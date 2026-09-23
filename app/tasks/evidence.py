"""Ordered broker facts and deterministic acceptance; model reviews stay labelled."""

from __future__ import annotations

import posixpath
from pathlib import PureWindowsPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.observability.events import Event, EventType
from app.tasks.review import ReviewResult


def file_key(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


class AcceptanceCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_files: list[str] = Field(default_factory=list, max_length=100)
    required_modified_files: list[str] = Field(default_factory=list, max_length=100)
    required_read_after_write: list[str] = Field(default_factory=list, max_length=100)
    require_readback_all_modified: bool = False
    required_reviewer_files: list[str] = Field(default_factory=list, max_length=100)
    required_workers: list[Literal["researcher", "coder", "reviewer"]] = Field(
        default_factory=list, max_length=3
    )
    required_review_verdict: Literal["pass", "pass_or_warnings"] | None = None
    required_tool_calls: list[str] = Field(default_factory=list, max_length=100)
    required_tests: list[str] = Field(default_factory=list, max_length=100)
    required_visual_qa: bool = False
    required_project_toolchain: bool = False

    @field_validator(
        "required_files",
        "required_modified_files",
        "required_read_after_write",
        "required_reviewer_files",
    )
    @classmethod
    def relative_paths(cls, paths: list[str]) -> list[str]:
        result = []
        for path in paths:
            normalized = file_key(path)
            if (
                not path
                or len(path) > 1024
                or "\x00" in path
                or path.startswith(("/", "\\", "~"))
                or PureWindowsPath(path).drive
                or normalized in {".", ".."}
                or normalized.startswith("../")
            ):
                raise ValueError("acceptance file paths must be confined relative paths")
            result.append(normalized)
        return list(dict.fromkeys(result))


def execution_evidence(events: list[Event]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    # Input order is durable audit order, not model-provided timestamps or claims.
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        payload = event.payload
        tool = payload.get("tool")
        result = payload.get("result", {})
        if not isinstance(tool, str) or not isinstance(result, dict):
            warnings.append(f"Unusable legacy tool metadata: {event.id}")
            continue
        call = {**payload, "event_id": event.id, "agent": event.actor}
        calls.append(call)
        path = result.get("path")
        if tool not in {"filesystem.write", "filesystem.read"} or not isinstance(path, str):
            continue
        path = file_key(path)
        state = states.setdefault(
            path,
            {
                "path": path,
                "last_write_event_id": None,
                "verified_after_write": False,
                "reviewer_inspected_complete": False,
                "read_event_ids": [],
                "coverage": {},
            },
        )
        if tool == "filesystem.write":
            state.update(
                last_write_event_id=event.id,
                verified_after_write=False,
                reviewer_inspected_complete=False,
                read_event_ids=[],
                coverage={},
            )
            continue
        # Old logs lack delivery/continuation metadata; never retroactively claim full reads.
        offset, count, total = (result.get(k) for k in ("offset", "bytes_returned", "total_bytes"))
        version = result.get("version")
        if not all(type(n) is int and n >= 0 for n in (offset, count, total)) or not isinstance(
            version, str
        ):
            continue
        offset, count, total = cast(int, offset), cast(int, count), cast(int, total)
        if offset + count > total:
            continue
        if state.get("read_version") not in {None, version}:
            state.update(
                verified_after_write=False,
                reviewer_inspected_complete=False,
                read_event_ids=[],
                coverage={},
            )
        state["read_version"] = version
        actor = event.actor or "unknown"
        coverage = state["coverage"].get(actor)
        if coverage is None or coverage["version"] != version or coverage["total"] != total:
            coverage = {"version": version, "total": total, "ranges": [], "events": []}
            state["coverage"][actor] = coverage
        coverage["ranges"].append((offset, offset + count))
        coverage["events"].append(event.id)
        covered = 0
        for start, end in sorted(coverage["ranges"]):
            if start > covered:
                break
            covered = max(covered, end)
        full = covered == total and any(start == 0 for start, _ in coverage["ranges"])
        if full:
            state["read_event_ids"] = coverage["events"]
            if state["last_write_event_id"]:
                state["verified_after_write"] = True
            if actor == "reviewer":
                state["reviewer_inspected_complete"] = True
    files = [{k: v for k, v in state.items() if k != "coverage"} for state in states.values()]
    tests = [c["result"] for c in calls if c["tool"] == "tests.run"]
    return {
        "tool_calls": calls,
        "files_modified": sorted(f["path"] for f in files if f["last_write_event_id"]),
        "file_verification": files,
        "verified_after_write": sorted(f["path"] for f in files if f["verified_after_write"]),
        "reviewer_inspected_files": sorted(
            f["path"] for f in files if f["reviewer_inspected_complete"]
        ),
        "tests_executed": tests,
        "verification_actions": [c for c in calls if c["tool"] in {"filesystem.read", "tests.run"}],
        "warnings": warnings,
        "scope": (
            "Recorded execution and read coverage only; "
            "not proof of correctness or current disk contents."
        ),
    }


def evaluate_criteria(criteria: AcceptanceCriteria, evidence: dict[str, Any]) -> list[str]:
    files = {
        file_key(c["result"]["path"])
        for c in evidence["tool_calls"]
        if c["tool"] in {"filesystem.read", "filesystem.write"}
        and isinstance(c.get("result", {}).get("path"), str)
    }
    tools = {c["tool"] for c in evidence["tool_calls"]}
    tests = {t.get("suite") for t in evidence["tests_executed"] if t.get("passed") is True}
    modified = set(evidence["files_modified"])
    required_readback = set(criteria.required_read_after_write)
    if criteria.require_readback_all_modified:
        required_readback |= modified
    return (
        [f"missing file evidence: {p}" for p in criteria.required_files if p not in files]
        + [
            f"missing modification: {p}"
            for p in criteria.required_modified_files
            if p not in modified
        ]
        + [
            f"missing complete read after latest write: {p}"
            for p in sorted(required_readback)
            if p not in evidence["verified_after_write"]
        ]
        + [
            f"missing complete reviewer inspection: {p}"
            for p in criteria.required_reviewer_files
            if p not in evidence["reviewer_inspected_files"]
        ]
        + [f"missing tool execution: {t}" for t in criteria.required_tool_calls if t not in tools]
        + [f"missing passing test: {t}" for t in criteria.required_tests if t not in tests]
    )


def evaluate_acceptance(
    criteria: AcceptanceCriteria,
    evidence: dict[str, Any],
    workers: list[dict[str, Any]],
    visual_qa: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures = evaluate_criteria(criteria, evidence)
    if criteria.required_project_toolchain:
        toolchain = (visual_qa or {}).get("toolchain", {})
        for operation in ("build", "test"):
            if not (toolchain.get(operation, {}).get("output") or {}).get("passed"):
                failures.append(f"missing passing project {operation}")
    if criteria.required_visual_qa:
        if not visual_qa or not visual_qa.get("screenshots_generated"):
            failures.append("missing final visual QA screenshots")
        else:
            previews = visual_qa.get("previews", [])
            if {p.get("device") for p in previews} != {"desktop", "mobile"}:
                failures.append("missing desktop/mobile visual QA")
            for route in visual_qa.get("routes", ["/"]):
                pairs = [p for p in previews if p.get("route", "/") == route]
                if {p.get("device") for p in pairs} != {"desktop", "mobile"}:
                    failures.append(f"missing desktop/mobile route QA: {route}")
            if any(
                not p.get("dom_loaded")
                or p.get("render_success") is False
                or (p.get("http_status") is not None and p["http_status"] >= 400)
                or p.get("console_errors")
                or p.get("failed_resources")
                or p.get("horizontal_overflow")
                for p in previews
            ):
                failures.append("final visual QA contains runtime, resource or overflow errors")
    checks: list[dict[str, Any]] = [
        {"source": "runtime-verified", "status": "fail", "message": f} for f in failures
    ]
    by_role = {w["agent"]: w for w in workers}
    for role in set(criteria.required_workers) | set(by_role):
        ok = by_role.get(role, {}).get("status") == "completed"
        checks.append(
            {
                "source": "runtime-verified",
                "status": "pass" if ok else "fail",
                "message": f"{role} execution completed",
            }
        )
        if not ok:
            failures.append(f"required worker did not complete: {role}")
    if not workers:
        failures.append("no workers executed")
    reviewer = by_role.get("reviewer")
    require_review = (
        reviewer is not None
        or criteria.required_review_verdict is not None
        or bool(criteria.required_reviewer_files)
    )
    review = reviewer.get("review") if reviewer else None
    if require_review:
        if not review:
            failures.append("missing structured reviewer verdict")
        else:
            parsed = ReviewResult.model_validate(review)
            rejects = parsed.verdict == "fail" or (
                criteria.required_review_verdict == "pass" and parsed.verdict != "pass"
            )
            if rejects:
                failures.append(f"reviewer verdict: {parsed.verdict}")
            for finding in parsed.findings:
                if finding.severity == "critical":
                    failures.append(f"critical review finding: {finding.message}")
            for criterion in parsed.acceptance_criteria:
                if criterion.required and criterion.result != "pass":
                    failures.append(
                        f"required model-reviewed criterion not passed: {criterion.criterion}"
                    )
            checks.append(
                {"source": "model-reviewed", "status": parsed.verdict, "message": parsed.summary}
            )
    checks.append(
        {
            "source": "runtime-verified",
            "status": (
                "not_evaluated"
                if not any(criteria.model_dump().values())
                else "pass"
                if not evaluate_criteria(criteria, evidence)
                else "fail"
            ),
            "message": "Explicit tool evidence requirements",
        }
    )
    return {
        "status": (
            "not_evaluated"
            if not any(criteria.model_dump().values())
            else "rejected"
            if failures
            else "accepted"
        ),
        "explicit_requirements": any(criteria.model_dump().values()),
        "review_inspection": "evidence_present"
        if evidence["reviewer_inspected_files"]
        else "unverified",
        "failures": failures,
        "checks": checks,
        "review": review,
        "scope": (
            "Acceptance of declared criteria; model-reviewed judgments are not runtime proof."
        ),
    }
