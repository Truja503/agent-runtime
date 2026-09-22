"""Acceptance rejects finished-but-incomplete work and requires ordered evidence."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents.reviewer import ReviewerAgent
from app.container import Runtime
from app.main import create_app
from app.models.scripted import ScriptedModelProvider
from app.observability.events import Event, EventType
from app.tasks.evidence import AcceptanceCriteria, evaluate_acceptance, execution_evidence
from app.tasks.state import Task, TaskOptions, TaskStatus
from app.tools.filesystem import FilesystemTools, ReadArgs, Workspace, WriteArgs
from tests.conftest import API_TOKEN


def tool(name: str, path: str, **args: Any) -> str:
    return json.dumps({"action": "use_tool", "tool": name, "arguments": {"path": path, **args}})


def finish(review: dict[str, Any] | None = None) -> str:
    return json.dumps({"action": "finish", "summary": "execution done", "review": review})


def verdict(value: str = "pass", critical: bool = False) -> dict[str, Any]:
    return {
        "verdict": value,
        "summary": "Reviewed implementation",
        "findings": [
            {
                "severity": "critical",
                "category": "implementation",
                "message": "Transaction logic missing",
                "affected_files": ["app.js"],
                "evidence_event_ids": [],
            }
        ]
        if critical
        else [],
        "acceptance_criteria": [],
    }


@pytest.mark.parametrize(
    "review_verdict,critical,readback,expected",
    [
        ("fail", True, True, TaskStatus.FAILED),
        ("pass", False, True, TaskStatus.COMPLETED),
        ("pass", False, False, TaskStatus.FAILED),
        ("pass", True, True, TaskStatus.FAILED),
        ("pass_with_warnings", False, True, TaskStatus.COMPLETED),
    ],
)
async def test_execution_completion_is_not_acceptance(
    runtime: Runtime,
    review_verdict: str,
    critical: bool,
    readback: bool,
    expected: TaskStatus,
) -> None:
    coder = [tool("filesystem.write", "app.js", content="const amount = 1;")]
    reviewer = [tool("filesystem.read", "app.js")] if readback else []
    model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["researcher", "coder", "reviewer"],
                        "plan": "Inspect, implement, review",
                    }
                )
            ],
            "researcher": [finish()],
            "coder": [*coder, finish()],
            "reviewer": [*reviewer, finish(verdict(review_verdict, critical))],
        }
    )
    runtime.supervisor.model = model
    for agent in runtime.workers.values():
        agent.model = model
    task = await runtime.tasks.create(
        "Build transaction simulator",
        created_by="test",
        options=TaskOptions(
            acceptance=AcceptanceCriteria(
                required_modified_files=["app.js"],
                required_read_after_write=["app.js"],
                required_reviewer_files=["app.js"],
                required_workers=["researcher", "coder", "reviewer"],
            )
        ),
    )
    await runtime.run_task(task.id)
    task = await runtime.tasks.get(task.id)
    assert task.status == expected
    assert all(w["status"] == "completed" for w in task.result["worker_results"])
    assert task.result["review"]["verdict"] == review_verdict
    assert task.result["acceptance"]["status"] == (
        "accepted" if expected == TaskStatus.COMPLETED else "rejected"
    )


@pytest.mark.parametrize(
    "sequence,verified",
    [
        (["write:A"], []),
        (["read:A", "write:A"], []),
        (["write:A", "read:A"], ["A"]),
        (["write:A", "write:A", "read:A"], ["A"]),
        (["write:A", "read:B"], []),
        (["write:A", "read:A", "write:A"], []),
    ],
)
def test_readback_requires_full_read_after_latest_write(
    sequence: list[str], verified: list[str]
) -> None:
    events = []
    for item in sequence:
        action, path = item.split(":")
        events.append(
            Event(
                type=EventType.TOOL_COMPLETED,
                actor="coder",
                payload={
                    "tool": "filesystem." + action,
                    "result": {
                        "path": path,
                        "offset": 0,
                        "bytes_returned": 5,
                        "total_bytes": 5,
                        "complete": True,
                        "version": "v1",
                    },
                },
            )
        )
    assert execution_evidence(events)["verified_after_write"] == verified


async def test_unicode_continuation_reassembles_large_file_and_never_escapes(
    workspace: Workspace,
) -> None:
    fs = FilesystemTools(workspace)
    content = "hello 世界 🚀\n" * 3000
    await fs.write(WriteArgs(path="large.txt", content=content))
    offset, chunks = 0, []
    while True:
        page = await fs.read(ReadArgs(path="large.txt", offset=offset, max_bytes=1003))
        chunks.append(page["content"])
        assert page["bytes_returned"] <= 1003
        if page["complete"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(chunks) == content
    assert page["total_bytes"] == len(content.encode())
    with pytest.raises(Exception, match="escapes"):
        await fs.read(ReadArgs(path="../outside", offset=100))


async def test_file_observation_delivers_more_than_1500_chars(runtime: Runtime) -> None:
    content = "a" * 6000 + "END_OF_FILE"
    (runtime.settings.workspace_root / "large.txt").write_text(content)
    model = ScriptedModelProvider(
        script={"reviewer": [tool("filesystem.read", "large.txt"), finish(verdict())]}
    )
    runtime.workers["reviewer"].model = model
    task = await runtime.tasks.create(
        "Read all", created_by="test", options=TaskOptions(agent="reviewer")
    )
    await runtime.run_task(task.id)
    second = model.calls[1]
    assert "END_OF_FILE" in second.messages[-1].content
    assert '"complete": true' in second.messages[-1].content
    # Full source is delivered, but not copied into durable result observations/events.
    assert content not in str((await runtime.tasks.get(task.id)).result)
    assert content not in str(await runtime.tasks.events_for(task.id))


def test_page_coverage_cannot_mix_versions_or_agents() -> None:
    def page(actor: str, offset: int, version: str) -> Event:
        return Event(
            type=EventType.TOOL_COMPLETED,
            actor=actor,
            payload={
                "tool": "filesystem.read",
                "result": {
                    "path": "x",
                    "offset": offset,
                    "bytes_returned": 5,
                    "total_bytes": 10,
                    "version": version,
                },
            },
        )

    assert not execution_evidence([page("reviewer", 0, "v1"), page("reviewer", 5, "v2")])[
        "reviewer_inspected_files"
    ]
    assert not execution_evidence([page("coder", 0, "v1"), page("reviewer", 5, "v1")])[
        "reviewer_inspected_files"
    ]
    assert execution_evidence([page("reviewer", 0, "v1"), page("reviewer", 5, "v1")])[
        "reviewer_inspected_files"
    ] == ["x"]


def test_reviewer_cannot_finish_without_verdict() -> None:
    assert ReviewerAgent.parse_decision(finish()) is None
    assert ReviewerAgent.parse_decision(finish(verdict())) is not None
    assert ReviewerAgent.parse_decision(finish(verdict("invented"))) is None


async def test_supervisor_sees_real_capabilities_and_passes_findings(runtime: Runtime) -> None:
    model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["researcher", "coder", "reviewer"],
                        "plan": "Inspect, write, verify",
                    }
                )
            ],
            "researcher": [
                tool("filesystem.list", "."),
                json.dumps({"action": "finish", "summary": "finding for coder"}),
            ],
            "coder": [tool("filesystem.write", "new.txt", content="x"), finish()],
            "reviewer": [tool("filesystem.read", "new.txt"), finish(verdict())],
        }
    )
    runtime.supervisor.model = model
    for a in runtime.workers.values():
        a.model = model
    task = await runtime.tasks.create("inspect/write/verify", created_by="test")
    await runtime.run_task(task.id)
    supervisor = model.calls[0].system
    assert "researcher: filesystem.list, filesystem.read, github.read" in supervisor
    assert (
        "Never assign implementation or writing to an agent lacking filesystem.write" in supervisor
    )
    coder_request = next(r for r in model.calls if r.metadata["agent"] == "coder")
    assert "finding for coder" in coder_request.messages[0].content
    assert "filesystem.write" not in runtime.workers["researcher"].allowed_tools
    assert "filesystem.write" not in runtime.workers["reviewer"].allowed_tools


@pytest.mark.parametrize(
    "status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.INTERRUPTED]
)
async def test_task_detail_endpoints_serialize_review_and_warnings(
    runtime: Runtime, status: TaskStatus
) -> None:
    task = Task(
        goal="legacy and structured task", status=status, result={"review": verdict("fail", True)}
    )
    await runtime.tasks._store.create(task)
    await runtime.events.emit(
        EventType.MODEL_TIMEOUT, task_id=task.id, actor="reviewer", attempt=1, timeout_seconds=120
    )
    await runtime.events.emit(EventType.MODEL_RETRY, task_id=task.id, actor="reviewer", attempt=2)
    await runtime.events.emit(
        EventType.MODEL_RESPONSE, task_id=task.id, actor="reviewer", attempt=2
    )
    # Historical/custom malformed metadata must not crash the entire evidence view.
    await runtime.events.emit(
        EventType.TOOL_COMPLETED,
        task_id=task.id,
        actor="coder",
        tool="filesystem.read",
        result="legacy",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        for path in [
            "/runtime",
            "/tasks",
            f"/tasks/{task.id}",
            f"/tasks/{task.id}/events",
            f"/tasks/{task.id}/evidence",
        ]:
            response = await client.get(path)
            assert response.status_code == 200, path
        facts = (await client.get(f"/tasks/{task.id}/evidence")).json()
        assert facts["warnings"]


def test_claimed_evidence_and_critical_findings_cannot_override_acceptance() -> None:
    evidence = execution_evidence([])
    review = verdict("pass", True)
    review["findings"][0]["evidence_event_ids"] = ["fabricated"]
    accepted = evaluate_acceptance(
        AcceptanceCriteria(),
        evidence,
        [{"agent": "reviewer", "status": "completed", "review": review}],
    )
    assert accepted["status"] == "not_evaluated"
    assert accepted["failures"]  # Execution still fails on a critical review.
    assert evidence["tool_calls"] == []


async def test_reasoning_effort_is_explicit_and_omitted_by_default() -> None:
    import openai
    from pydantic import SecretStr

    from app.models.base import Message, ModelRequest, Role
    from app.models.local import LocalModelProvider
    from tests.test_providers import FakeOpenAIClient

    fake = FakeOpenAIClient()
    provider = LocalModelProvider(
        base_url="http://localhost/v1", model="example", api_key=SecretStr("unused"), client=fake
    )
    await provider.generate(ModelRequest(messages=[Message(role=Role.USER, content="hello")]))
    assert fake.captured["reasoning_effort"] is openai.omit
    await provider.generate(
        ModelRequest(messages=[Message(role=Role.USER, content="hello")], reasoning_effort="none")
    )
    assert fake.captured["reasoning_effort"] == "none"


async def test_persisted_review_and_evidence_roundtrip(runtime: Runtime) -> None:
    from app.observability.store import SQLiteEventStore
    from app.tasks.store import SQLiteTaskStore

    runtime.tasks._store = SQLiteTaskStore(runtime.settings.database_path)
    runtime.events._sinks = [SQLiteEventStore(runtime.settings.database_path)]
    model = ScriptedModelProvider(
        script={"reviewer": [tool("filesystem.read", "README.md"), finish(verdict("fail", True))]}
    )
    runtime.workers["reviewer"].model = model
    task = await runtime.tasks.create(
        "review", created_by="test", options=TaskOptions(agent="reviewer")
    )
    await runtime.run_task(task.id)
    reloaded = await runtime.tasks.get(task.id)
    assert reloaded.status == TaskStatus.FAILED
    assert reloaded.result["review"]["findings"][0]["severity"] == "critical"
    assert execution_evidence(await runtime.tasks.events_for(task.id))[
        "reviewer_inspected_files"
    ] == ["README.md"]


def test_pass_with_warnings_cannot_satisfy_strict_pass_or_missing_worker() -> None:
    workers = [
        {"agent": "reviewer", "status": "completed", "review": verdict("pass_with_warnings")}
    ]
    criteria = AcceptanceCriteria(
        required_review_verdict="pass", required_workers=["coder", "reviewer"]
    )
    outcome = evaluate_acceptance(criteria, execution_evidence([]), workers)
    assert outcome["status"] == (
        "rejected" if outcome["explicit_requirements"] else "not_evaluated"
    )
    assert "reviewer verdict: pass_with_warnings" in outcome["failures"]
    assert "required worker did not complete: coder" in outcome["failures"]


def test_required_model_reviewed_criterion_must_pass() -> None:
    review = verdict()
    review["acceptance_criteria"] = [
        {"criterion": "BUILD TRANSACTION", "result": "not_verified", "required": True}
    ]
    outcome = evaluate_acceptance(
        AcceptanceCriteria(),
        execution_evidence([]),
        [{"agent": "reviewer", "status": "completed", "review": review}],
    )
    assert outcome["status"] == (
        "rejected" if outcome["explicit_requirements"] else "not_evaluated"
    )
    assert any("not passed" in failure for failure in outcome["failures"])
