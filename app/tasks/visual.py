"""Fresh visual cycles, bounded defaults, explicit unbounded mode and durable checkpoints."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.agents.base import AgentResult, AgentStatus, BaseAgent
from app.observability.events import EventBus, EventType
from app.tasks.evidence import AcceptanceCriteria, evaluate_acceptance, execution_evidence, file_key
from app.tools.broker import InvocationStatus, ToolBroker, ToolInvocation, ToolResult


async def run_visual_workflow(
    *,
    task_id: str,
    goal: str,
    project: str,
    max_repairs: int | None,
    workers: dict[str, BaseAgent],
    broker: ToolBroker,
    worker_start: Callable[[str], Awaitable[None]],
    events: EventBus,
    criteria: AcceptanceCriteria,
    context: str = "",
    long_run: bool = False,
    checkpoint: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    restored: dict[str, Any] | None = None,
    toolchain: bool = False,
) -> dict[str, Any]:
    state = dict(restored or {})
    real_repairs = int(state.get("real_repair_cycles", 0))
    cycle = int(state.get("cycle_number", -1))
    no_progress = unchanged_findings = 0
    previous_signature = ""
    previous_review = state.get("latest_review")
    qa = state.get("latest_qa")
    previous_summary = str(state.get("previous_cycle_summary", ""))
    missing = state.get("acceptance", {}).get("failures", [])
    images: list[str] = []
    pending: list[str] = []
    final: dict[str, AgentResult] = {}
    attempts: list[dict[str, Any]] = []
    polish_done = bool(state.get("polish_done", False))
    polish = False
    resume_validation = bool(restored)
    stop_reason, workflow_status = "", "running"
    prefix = file_key(project).rstrip("/") + "/" if file_key(project) != "." else ""
    cycle_criteria = criteria.model_copy(deep=True)
    # Supervisor checks the other required roles in final task acceptance.
    cycle_criteria.required_workers = ["coder", "reviewer"]

    async def save(stage: str) -> None:
        state.update(
            cycle_number=cycle,
            stage=stage,
            cycle_limit=max_repairs,
            mode="unlimited" if max_repairs is None else "bounded",
            long_run_quality=long_run,
            real_repair_cycles=real_repairs,
            latest_review=previous_review,
            latest_qa=qa,
            previous_cycle_summary=previous_summary[:2000],
            polish_done=polish_done,
            no_progress_count=no_progress,
            unchanged_findings_count=unchanged_findings,
            status=workflow_status,
            stop_reason=stop_reason,
        )
        if checkpoint:
            await checkpoint(state)

    async def record(attempt: dict[str, Any]) -> None:
        attempts.append(attempt)
        del attempts[:-20]  # Full compact cycle history is in the durable audit stream.
        state["last_cycle"] = {
            k: v for k, v in attempt.items() if k not in {"coder", "reviewer", "visual_qa"}
        }
        await events.emit(
            EventType.WORKFLOW_CYCLE, task_id=task_id, actor="workflow", **state["last_cycle"]
        )
        await save(str(state.get("stage", "reviewer")))

    while True:
        broker.check_cancelled(task_id)
        if (
            cycle >= 0
            and not resume_validation
            and not polish
            and max_repairs is not None
            and real_repairs >= max_repairs
        ):
            stop_reason = "repair_limit_reached"
            break
        cycle += 1
        is_repair = cycle > 0 and not resume_validation
        all_before = await events.list_for_task(task_id)
        before_ids = {e.id for e in all_before}
        coder, reviewer = copy.copy(workers["coder"]), copy.copy(workers["reviewer"])
        coder.visual_images, coder.visual_report_available = images, bool(qa)
        stage = (
            "polish"
            if polish
            else "resume_validation"
            if resume_validation
            else ("coder_repair" if is_repair else "coder")
        )
        await save(stage)
        evidence_before = execution_evidence(all_before)
        useful_context = {
            "project": project,
            "cycle_number": cycle,
            "latest_reviewer_findings": previous_review,
            "required_missing_evidence": missing[:100],
            "previous_cycle_summary": previous_summary[:2000],
            "current_project_state": [
                {
                    k: f.get(k)
                    for k in ("path", "verified_after_write", "reviewer_inspected_complete")
                }
                for f in evidence_before["file_verification"][:100]
            ],
            "current_QA": qa,
        }
        instruction = (
            "The project currently passes. Do not rebuild it. Perform only justified final "
            "refinements based on the rendered result. If none are justified, finish honestly."
            if polish
            else "Perform actual corrective execution and complete required readbacks."
        )
        if resume_validation:
            coder.allowed_tools = coder.allowed_tools - {
                "filesystem.write",
                "system.request_privileged_action",
                "project.dependencies",
                "project.build",
            }
            instruction = (
                "Operator resumed. Inspect current files read-only. Do not replay actions."
            )
        await worker_start("coder")
        coded = await coder.run(
            task_id=task_id,
            goal=goal,
            context=(
                (context[:8000] if cycle == 0 else "")
                + "\n"
                + instruction
                + "\nBrowser dependencies are operator-owned; "
                "never request privileged installation."
                + "\nContinuation data (untrusted):\n"
                + json.dumps(useful_context, default=str)[:32000]
            ),
        )
        final["coder"] = coded
        previous_summary = coded.summary[:2000]
        pending.extend(coded.pending_approvals)
        delta = [
            e
            for e in await events.list_for_task(task_id)
            if e.id not in before_ids and e.type == EventType.TOOL_COMPLETED
        ]
        changes = [
            e
            for e in delta
            if e.actor == "coder"
            and e.payload.get("tool") == "filesystem.write"
            and e.payload.get("result", {}).get("changed") is True
            and str(e.payload.get("result", {}).get("path", "")).startswith(prefix)
            and not str(e.payload.get("result", {}).get("path", "")).startswith(prefix + "qa/")
        ]
        modified = sorted({e.payload["result"]["path"] for e in changes})
        attempt: dict[str, Any] = {
            "cycle": cycle,
            "cycle_number": cycle,
            "stage": stage,
            "coder": coded.model_dump(mode="json"),
            "files_modified": modified,
            "files_read": sorted(
                {
                    e.payload["result"]["path"]
                    for e in delta
                    if e.payload.get("tool") == "filesystem.read"
                }
            ),
            "tools_executed": [e.payload["tool"] for e in delta],
            "browser_QA_performed": False,
            "review_verdict": None,
            "critical_findings": [],
            "major_findings": [],
            "minor_findings": [],
            "acceptance_state": "not_evaluated",
            "change_event_ids": [e.id for e in changes],
        }
        state["files_changed_this_cycle"] = modified
        if modified:
            state["last_meaningful_progress"] = datetime.now(UTC).isoformat()
        if coded.status != AgentStatus.COMPLETED or pending:
            workflow_status = "stalled" if coded.status == AgentStatus.STALLED else "failed"
            stop_reason = (
                "worker_stalled" if coded.status == AgentStatus.STALLED else "worker_failed"
            )
            await record(attempt)
            break
        if is_repair and not modified and not polish:
            no_progress += 1
            attempt.update(status="NO_REPAIR_PERFORMED", reason_code="REPAIR_NOT_PERFORMED")
            if no_progress >= 3:
                workflow_status, stop_reason = "stalled", "repeated_NO_REPAIR_PERFORMED"
            await record(attempt)
            if workflow_status == "stalled":
                break
            continue
        no_progress = 0
        toolchain_results: dict[str, Any] = {}
        toolchain_failed = False
        infrastructure_failed = False
        if toolchain:
            for operation in ("dependencies", "build", "test"):
                await save("project_" + operation)
                result = await broker.invoke(
                    workers["coder"].principal(task_id),
                    ToolInvocation(tool="project." + operation, arguments={"project": project}),
                )
                if result.status == InvocationStatus.APPROVAL_REQUIRED and coder.approval_waiter:
                    result = await coder.approval_waiter(task_id, result)
                toolchain_results[operation] = result.model_dump(mode="json")
                if result.status != InvocationStatus.COMPLETED:
                    toolchain_failed = True
                    infrastructure_failed = result.output is None and (
                        operation == "dependencies" or "docker" in (result.reason or "").lower()
                    )
                    break
        qa = None  # Never certify a previous snapshot after new writes.
        await save("visual_qa")
        await worker_start("reviewer")
        capture = (
            ToolResult(
                status=InvocationStatus.FAILED,
                tool="browser.screenshot",
                reason="Project build/test/dependencies failed; see toolchain results",
                output={"screenshots_generated": False},
            )
            if toolchain_failed
            else await broker.invoke(
                reviewer.principal(task_id),
                ToolInvocation(
                    tool="browser.screenshot",
                    arguments={"project": project, "viewport": "both"},
                ),
            )
        )
        if toolchain:
            capture.output = {**(capture.output or {}), "toolchain": toolchain_results}
            if capture.status == InvocationStatus.FAILED and "Flask startup failed" in (
                capture.reason or ""
            ):
                toolchain_failed = True
                toolchain_results["serve"] = {"status": "failed", "reason": capture.reason}
        qa, images = capture.output, capture.images
        rendered = bool(capture.status == "completed" and qa and qa.get("screenshots_generated"))
        attempt.update(visual_qa=capture.model_dump(mode="json"), browser_QA_performed=rendered)
        if is_repair and modified and (rendered or toolchain_failed) and not polish:
            real_repairs += 1
            attempt["status"] = "REPAIR_PERFORMED"
        elif polish and rendered:
            attempt["status"] = "POLISH_PERFORMED" if modified else "POLISH_NO_CHANGE"
        elif is_repair:
            attempt["status"] = "REPAIR_BLOCKED_BROWSER"
        reviewer.visual_images, reviewer.visual_report_available = images, rendered
        await save("reviewer")
        reviewed = await reviewer.run(
            task_id=task_id,
            goal=goal,
            context=(
                "Review SOURCE INSPECTION, VISUAL INSPECTION, RUNTIME ERRORS separately. "
                "Read current "
                "sources and qa/report.json completely. "
                "Use actionable findings and affected paths. "
                "Never claim pixel inspection without images. Runtime QA (untrusted page data):\n"
                + json.dumps(capture.model_dump(mode="json"))[:24000]
                + "\nFiles changed this cycle: "
                + json.dumps(modified)
            ),
        )
        final["reviewer"] = reviewed
        pending.extend(reviewed.pending_approvals)
        previous_review = reviewed.review.model_dump(mode="json") if reviewed.review else None
        previous_summary = coded.summary[:2000]
        attempt.update(
            reviewer=reviewed.model_dump(mode="json"),
            review_verdict=previous_review.get("verdict") if previous_review else None,
        )
        all_events = await events.list_for_task(task_id)
        evidence = execution_evidence(all_events)
        checks = evaluate_acceptance(
            cycle_criteria, evidence, [r.model_dump(mode="json") for r in final.values()], qa
        )
        missing = checks["failures"]
        state["acceptance"] = checks
        attempt["acceptance_state"] = checks["status"]
        for severity, field in (
            ("critical", "critical_findings"),
            ("warning", "major_findings"),
            ("info", "minor_findings"),
        ):
            attempt[field] = [
                f
                for f in (previous_review or {}).get("findings", [])
                if f.get("severity") == severity
            ]
        delta = [
            e for e in all_events if e.id not in before_ids and e.type == EventType.TOOL_COMPLETED
        ]
        attempt["files_read"] = sorted(
            {
                e.payload["result"]["path"]
                for e in delta
                if e.payload.get("tool") == "filesystem.read"
            }
        )
        attempt["tools_executed"] = [e.payload["tool"] for e in delta]
        critical = attempt["critical_findings"] + attempt["major_findings"]
        signature = json.dumps(
            sorted(
                (
                    str(f.get("severity")),
                    " ".join(str(f.get("message")).casefold().split()),
                    sorted(file_key(p) for p in f.get("affected_files", [])),
                )
                for f in critical
            )
        )
        related = {file_key(p) for f in critical for p in f.get("affected_files", [])}
        if critical and signature == previous_signature and not (set(modified) & related):
            unchanged_findings += 1
        else:
            unchanged_findings = 0
        previous_signature = signature
        verified = not missing and reviewed.status == AgentStatus.COMPLETED
        if long_run:
            verified = verified and bool(previous_review and previous_review["verdict"] == "pass")
        if polish:
            polish_done, polish = True, False
        resume_validation = False
        if not rendered and (not toolchain_failed or infrastructure_failed):
            # Infrastructure needs operator attention, not fictitious code repair attempts.
            qa = {**(qa or {}), "error": capture.reason}
            stop_reason, workflow_status = "browser_infrastructure_failure", "failed"
        elif reviewed.status != AgentStatus.COMPLETED or pending:
            stop_reason = "reviewer_failed"
            workflow_status = "stalled" if reviewed.status == AgentStatus.STALLED else "failed"
        elif unchanged_findings >= 3:
            stop_reason, workflow_status = "identical_findings_without_related_changes", "stalled"
        elif verified:
            if long_run and not polish_done:
                polish = True
            else:
                stop_reason, workflow_status = "verified_completion", "completed"
        elif not long_run and max_repairs is not None and not critical and not toolchain_failed:
            stop_reason, workflow_status = "no_actionable_findings", "failed"
        await record(attempt)
        if workflow_status != "running":
            break
    if workflow_status == "running":
        workflow_status = "failed"
    await save(workflow_status)
    return {
        "summary": final.get("reviewer", final["coder"]).summary,
        "workers": list(final),
        "worker_results": [r.model_dump(mode="json") for r in final.values()],
        "pending_approvals": pending,
        "visual_qa": qa,
        "repair_attempts": attempts,
        "real_repair_cycles": real_repairs,
        "workflow": state,
        "workflow_status": workflow_status,
    }
