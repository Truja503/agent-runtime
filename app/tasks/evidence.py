"""Projections of broker facts, never interpretations of agent summaries."""

from typing import Any

from pydantic import BaseModel, Field

from app.observability.events import Event, EventType


class AcceptanceCriteria(BaseModel):
    required_files: list[str] = Field(default_factory=list, max_length=100)
    required_tool_calls: list[str] = Field(default_factory=list, max_length=100)
    required_tests: list[str] = Field(default_factory=list, max_length=100)


def execution_evidence(events: list[Event]) -> dict[str, Any]:
    calls = [
        {"event_id": e.id, "agent": e.actor, **e.payload}
        for e in events
        if e.type is EventType.TOOL_COMPLETED
    ]
    files = [
        c["result"]["path"]
        for c in calls
        if c["tool"] == "filesystem.write" and "path" in c.get("result", {})
    ]
    tests = [c["result"] for c in calls if c["tool"] == "tests.run" and "result" in c]
    return {
        "tool_calls": calls,
        "files_modified": sorted(set(files)),
        "tests_executed": tests,
        "verification_actions": [c for c in calls if c["tool"] in {"filesystem.read", "tests.run"}],
        "scope": "Recorded tool execution; not proof of correctness or current file contents.",
    }


def evaluate_criteria(criteria: AcceptanceCriteria, evidence: dict[str, Any]) -> list[str]:
    files = {
        c.get("result", {}).get("path")
        for c in evidence["tool_calls"]
        if c["tool"] in {"filesystem.read", "filesystem.write"}
    }
    tools = {c["tool"] for c in evidence["tool_calls"]}
    tests = {t.get("suite") for t in evidence["tests_executed"] if t.get("passed") is True}
    return (
        [f"missing file evidence: {p}" for p in criteria.required_files if p not in files]
        + [f"missing tool execution: {t}" for t in criteria.required_tool_calls if t not in tools]
        + [f"missing passing test: {t}" for t in criteria.required_tests if t not in tests]
    )
