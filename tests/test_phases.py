"""Real runtime gates: models propose finish, evidence alone advances phases."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.coder import CoderAgent
from app.container import Runtime
from app.errors import InvalidTaskTransitionError
from app.models.base import ModelRequest
from app.models.scripted import ScriptedModelProvider
from app.observability.context import CURRENT_PHASE, PhaseContext
from app.observability.events import EventType
from app.observability.logging import JsonFormatter
from app.tasks.manager import TaskManager
from app.tasks.phases import ProjectPlan
from app.tasks.state import TaskOptions, TaskStatus
from app.tasks.store import SQLiteTaskStore
from tests.conftest import OPERATOR_ID, OPERATOR_SECRET
from tests.test_acceptance import finish, tool
from tests.test_browser import mock_qa, review


def phase(ident: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "title": ident.title(),
        "goal": f"Only implement {ident}",
        "workers": ["coder"],
        **extra,
    }


def configure(
    runtime: Runtime, phases: list[dict[str, Any]], script: dict[str, list[str]]
) -> ScriptedModelProvider:
    model = ScriptedModelProvider(
        script={
            "supervisor": [json.dumps({"summary": "Build Pocket Ledger", "phases": phases})],
            **script,
        }
    )
    runtime.supervisor.model = model
    for worker in runtime.workers.values():
        worker.model = model
    return model


async def test_three_phases_persist_compact_context_and_reject_early_finish(
    runtime: Runtime,
) -> None:
    requests: list[ModelRequest] = []
    phases = [
        phase("environment"),
        phase(
            "backend",
            depends_on=["environment"],
            requirements={"required_files": ["app.py"], "required_read_after_write": ["app.py"]},
        ),
        phase("frontend", depends_on=["backend"]),
    ]
    model = configure(
        runtime,
        phases,
        {
            "coder": [
                finish(),
                finish(),
                tool("filesystem.write", "app.py", content="app = 1"),
                tool("filesystem.read", "app.py"),
                finish(),
                finish(),
            ]
        },
    )
    original = model.generate

    async def record(request: ModelRequest) -> Any:
        requests.append(request)
        return await original(request)

    model.generate = record  # type: ignore[method-assign]
    task = await runtime.tasks.create("Huge original instruction " * 1000, created_by="test")
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status == "completed", stored.error or stored.result
    execution = stored.result["phase_execution"]
    assert [p["status"] for p in execution["phases"]] == ["passed"] * 3
    assert execution["phases"][1]["attempt"] == 2
    backend = [
        r.messages[0].content
        for r in requests
        if r.metadata["agent"] == "coder" and r.metadata["goal"] == "Only implement backend"
    ]
    assert "environment" in backend[0] and "Huge original instruction" not in backend[0]
    assert "missing file evidence: app.py" in backend[-1]
    events = await runtime.tasks.events_for(task.id)
    nested = [e for e in events if e.actor == "coder" and e.type != EventType.TASK_CREATED]
    assert nested and all(
        all(key in e.payload for key in ("phase_id", "phase_index", "phase_title", "phase_attempt"))
        for e in nested
    )
    assert any(e.type == EventType.PHASE_COMPLETION_REJECTED for e in events)
    assert CURRENT_PHASE.get() is None


async def test_project_test_repairs_inside_same_phase(runtime: Runtime) -> None:
    runs: list[int] = []

    async def backend_test(args: Any) -> dict[str, Any]:
        runs.append(1)
        return {
            "status": "completed" if len(runs) > 1 else "failed",
            "passed": len(runs) > 1,
            "output": "validation assertion failed",
        }

    spec = runtime.registry.get("project.test")
    runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": backend_test})
    configure(
        runtime,
        [phase("verification", verification=["test"])],
        {
            "coder": [
                finish(),
                tool("filesystem.write", "app.py", content="fixed validation"),
                finish(),
            ]
        },
    )
    task = await runtime.tasks.create("verify", created_by="test")
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == "completed", result.result
    assert len(runs) == 2
    assert result.result["phase_execution"]["phases"][0]["attempt"] == 2


@pytest.mark.parametrize("changes", ["unchanged", "operator", "later-phase", "environment"])
async def test_restart_resumes_current_phase_and_revalidates_prior_files(
    runtime: Runtime, changes: str
) -> None:
    environment_ready = True

    async def inspect_environment(args: Any) -> dict[str, Any]:
        return {"environment": "READY" if environment_ready else "NOT READY"}

    spec = runtime.registry.get("project.inspect")
    runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": inspect_environment})
    configure(
        runtime,
        [
            phase(
                "one",
                requirements={"required_files": ["one.txt"]},
                verification=["environment"] if changes == "environment" else [],
            ),
            phase("two"),
            phase("three"),
        ],
        {
            "coder": [
                tool("filesystem.write", "one.txt", content="verified"),
                finish(),
                *(
                    [tool("filesystem.write", "one.txt", content="verified by phase two")]
                    if changes == "later-phase"
                    else []
                ),
                finish(),
                finish(),
            ]
        },
    )
    task = await runtime.tasks.create("three phases", created_by="test")
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    execution = stored.result["phase_execution"]
    execution["current_phase_index"] = 2
    execution["phases"][2]["status"] = "running"
    stored.status = TaskStatus.RUNNING
    await runtime.tasks._store.update(stored)
    await runtime.tasks.reconcile_startup()
    paused = await runtime.tasks.get(task.id)
    assert paused.status == "paused"
    assert paused.result["phase_execution"]["phases"][0]["status"] == "passed"
    if changes == "operator":
        (runtime.settings.workspace_root / "one.txt").write_text("operator edit")
    environment_ready = changes != "environment"
    await runtime.tasks.record_result(task.id, {**paused.result, "resume_requested": True})
    await runtime.tasks.transition(task.id, TaskStatus.CREATED)
    before = len(await runtime.tasks.events_for(task.id))
    await runtime.run_task(task.id)
    events = (await runtime.tasks.events_for(task.id))[before:]
    resumed = next(e for e in events if e.type == EventType.PHASE_RESUMED)
    assert resumed.payload["phase_id"] == (
        "one" if changes in {"operator", "environment"} else "three"
    )
    assert not any(e.actor == "supervisor" and e.type == EventType.MODEL_REQUEST for e in events)


async def test_approval_wait_keeps_phase_and_operator_event_metadata(runtime: Runtime) -> None:
    configure(
        runtime,
        [phase("environment")],
        {
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "system.request_privileged_action",
                        "arguments": {"request": "restart nginx"},
                    }
                ),
                finish(),
            ]
        },
    )
    task = await runtime.tasks.create("operator request", created_by="test")
    job = runtime.schedule_task(task.id)
    for _ in range(200):
        stored = await runtime.tasks.get(task.id)
        if stored.status == "waiting_for_approval":
            break
        await asyncio.sleep(0.01)
    state = stored.result["phase_execution"]["phases"][0]
    assert state["status"] == "waiting_for_approval" and state["pending_approvals"]
    event = await runtime.events.emit(
        EventType.PRIVILEGED_ACTION_DENIED,
        task_id=task.id,
        actor="operator",
        request_id=state["pending_approvals"][0],
    )
    assert event.payload["phase_id"] == "environment"
    await runtime.privileged_service.approve_and_execute(
        request_id=state["pending_approvals"][0], operator_id=OPERATOR_ID, secret=OPERATOR_SECRET
    )
    await asyncio.wait_for(job, 5)
    completed = await runtime.tasks.get(task.id)
    assert completed.status == "completed"
    phase_state = completed.result["phase_execution"]["phases"][0]
    assert phase_state["status"] == "passed" and phase_state["attempt"] == 1
    assert phase_state["pending_approvals"] == []


async def test_context_does_not_leak_between_tasks(runtime: Runtime) -> None:
    token = CURRENT_PHASE.set(PhaseContext("one", "backend", 2, "Backend", 1, 3))
    try:
        event = await runtime.events.emit(EventType.TOOL_REQUESTED, task_id="other", actor="coder")
        assert "phase_id" not in event.payload
    finally:
        CURRENT_PHASE.reset(token)


@pytest.mark.parametrize(
    "bad",
    [
        [phase("bad/id")],
        [phase("one", workers=["shell"])],
        [phase("one", depends_on=["two"]), phase("two")],
        [phase("one"), phase("one")],
        [phase(f"p{i}") for i in range(13)],
    ],
)
def test_plan_cannot_grant_authority_or_invalid_dependencies(bad: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ProjectPlan(summary="plan", phases=bad)


async def test_task_manager_cannot_complete_rejected_or_unpassed_phase(runtime: Runtime) -> None:
    task = await runtime.tasks.create("goal", created_by="test")
    await runtime.tasks.transition(task.id, TaskStatus.PLANNING)
    await runtime.tasks.transition(task.id, TaskStatus.RUNNING)
    for result in (
        {"acceptance": {"status": "rejected"}},
        {"phase_execution": {"phases": [{"status": "running"}]}},
        {"pending_approvals": ["pending"]},
    ):
        with pytest.raises(InvalidTaskTransitionError):
            await runtime.tasks.complete(task.id, result)


def test_tool_schema_binds_name_to_arguments_and_accepts_legacy(runtime: Runtime) -> None:
    schema = runtime.workers["coder"].response_schema()
    branches = schema["properties"]["decision"]["anyOf"]
    test = next(b for b in branches if b["properties"]["tool"].get("enum") == ["project.test"])
    assert set(test["properties"]["arguments"]["properties"]) == {"project"}
    assert test["properties"]["arguments"]["additionalProperties"] is False
    decision = {"action": "use_tool", "tool": "project.test", "arguments": {"project": "site"}}
    assert CoderAgent.parse_decision(json.dumps({"decision": decision}))
    assert CoderAgent.parse_decision(json.dumps(decision))


async def test_sqlite_checkpoint_survives_new_manager(runtime: Runtime) -> None:
    database = runtime.settings.database_path
    runtime.tasks = TaskManager(SQLiteTaskStore(database), runtime.events)
    configure(
        runtime,
        [phase("one"), phase("two"), phase("three")],
        {"coder": [finish(), finish(), finish()]},
    )
    task = await runtime.tasks.create("persist plan", created_by="test")
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    stored.status = TaskStatus.RUNNING
    stored.result["phase_execution"]["phases"][2]["status"] = "running"
    await runtime.tasks._store.update(stored)
    runtime.tasks = TaskManager(SQLiteTaskStore(database), runtime.events)
    assert await runtime.tasks.reconcile_startup() == 1
    restored = await runtime.tasks.get(task.id)
    assert restored.status == "paused"
    assert [p["status"] for p in restored.result["phase_execution"]["phases"]] == [
        "passed",
        "passed",
        "paused",
    ]


async def test_failed_phase_can_resume_without_replanning(runtime: Runtime) -> None:
    configure(
        runtime,
        [phase("implementation", requirements={"required_files": ["missing.py"]})],
        {"coder": [finish()] * 3},
    )
    task = await runtime.tasks.create("resume failed phase", created_by="test")
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status == "failed"
    await runtime.tasks.record_result(task.id, {**stored.result, "resume_requested": True})
    await runtime.tasks.resume_failed_phase(task.id)
    runtime.workers["coder"].model = ScriptedModelProvider(
        script={"coder": [tool("filesystem.write", "missing.py", content="corrected"), finish()]}
    )
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status == "completed", stored.result
    assert stored.result["phase_execution"]["phases"][0]["attempt"] == 4


@pytest.mark.parametrize("research", [False, True])
async def test_reviewer_only_visual_phase_does_not_run_coder(
    runtime: Runtime, research: bool
) -> None:
    calls = mock_qa(runtime)
    configure(
        runtime,
        [
            phase(
                "qa",
                workers=["researcher", "reviewer"] if research else ["reviewer"],
                workflow="visual",
            )
        ],
        {"reviewer": [review("pass")], "researcher": [finish()]},
    )
    task = await runtime.tasks.create(
        "inspect rendered project", created_by="test", options=TaskOptions(visual_project="site")
    )
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status == "completed", stored.result
    events = await runtime.tasks.events_for(task.id)
    assert calls == ["qa"]
    assert not any(e.type == EventType.MODEL_REQUEST and e.actor == "coder" for e in events)
    assert (
        any(e.type == EventType.MODEL_REQUEST and e.actor == "researcher" for e in events)
        == research
    )
    assert stored.result["phase_execution"]["phases"][0]["status"] == "passed"


async def test_unlimited_phase_stalls_repeated_missing_evidence(runtime: Runtime) -> None:
    configure(
        runtime,
        [phase("backend", requirements={"required_files": ["missing.py"]})],
        {"coder": [finish()] * 5},
    )
    task = await runtime.tasks.create(
        "bounded despite unlimited", created_by="test", options=TaskOptions(max_repair_cycles=None)
    )
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status == "stalled"
    assert stored.result["phase_execution"]["phases"][0]["attempt"] == 3


def test_phase_json_logs_inherit_context_without_secret() -> None:
    token = CURRENT_PHASE.set(PhaseContext("task", "backend", 2, "token=secret-value", 3, 5))
    try:
        record = logging.LogRecord("runtime", logging.INFO, __file__, 1, "repair", (), None)
        entry = json.loads(JsonFormatter().format(record))
        assert entry["phase_id"] == "backend" and entry["phase_attempt"] == 3
        assert entry["phase_title"] == "[redacted]"
    finally:
        CURRENT_PHASE.reset(token)
