"""Optional unbounded execution: real progress, fresh contexts and durable recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.container import Runtime
from app.main import create_app
from app.models.base import ModelRequest
from app.models.scripted import ScriptedModelProvider
from app.observability.events import EventType
from app.tasks.manager import TaskManager
from app.tasks.state import TaskOptions, TaskStatus
from app.tasks.store import SQLiteTaskStore
from tests.test_acceptance import finish, tool
from tests.test_api import AUTH, VIEWER_AUTH
from tests.test_browser import mock_qa, review


class Recording(ScriptedModelProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> Any:
        self.requests.append(request.model_copy(deep=True))
        return await super().generate(request)


def test_limits_are_explicit_and_nullable() -> None:
    defaults = TaskOptions()
    assert defaults.max_steps is None and defaults.worker_steps_mode == "profile"
    assert defaults.max_repair_cycles == 2 and not defaults.long_run_quality
    for value in (None, ""):
        assert TaskOptions.model_validate({"max_steps": value}).worker_steps_mode == "profile"
    for value in (0, "unlimited"):
        options = TaskOptions.model_validate({"max_steps": value})
        assert options.max_steps is None and options.worker_steps_mode == "unlimited"
        assert TaskOptions.model_validate_json(options.model_dump_json()) == options
    assert TaskOptions(max_steps=200).max_steps == 200
    assert TaskOptions(max_repair_cycles=12).max_repair_cycles == 12
    assert TaskOptions.model_validate({"max_repair_cycles": "unlimited"}).max_repair_cycles is None
    assert TaskOptions(long_run_quality=True).max_repair_cycles == 2


async def test_unlimited_worker_exceeds_fifty_with_bounded_context(runtime: Runtime) -> None:
    model = Recording(
        script={
            "coder": [
                *[tool("filesystem.write", "progress.txt", content=str(i)) for i in range(60)],
                finish(),
            ]
        }
    )
    runtime.workers["coder"].model = model
    task = await runtime.tasks.create(
        "Work",
        created_by="test",
        options=TaskOptions(
            agent="coder",
            max_steps=0,
        ),
    )
    await runtime.run_task(task.id)
    completed = await runtime.tasks.get(task.id)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.result and completed.result["worker_results"][0]["steps"] == 61
    assert max(len(r.messages) for r in model.requests) <= 13
    assert all(sum("Task: Work" in m.content for m in r.messages) == 1 for r in model.requests)


@pytest.mark.parametrize(
    "decision", ["invalid", '{"action":"use_tool"}', tool("filesystem.read", "README.md")]
)
async def test_unlimited_worker_stagnation(runtime: Runtime, decision: str) -> None:
    runtime.workers["coder"].model = ScriptedModelProvider(script={"coder": [decision] * 8})
    task = await runtime.tasks.create(
        "Work",
        created_by="test",
        options=TaskOptions(
            agent="coder",
            max_steps=0,
        ),
    )
    await runtime.run_task(task.id)
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.STALLED


def quality_fixture(
    runtime: Runtime,
    verdicts: list[str],
    *,
    no_change: set[int] | None = None,
    unrelated: bool = False,
) -> tuple[list[str], Recording]:
    calls = mock_qa(runtime)
    project = runtime.settings.workspace_root / "site"
    (project / "qa").mkdir(parents=True)
    (project / "qa/report.json").write_text("{}", encoding="utf-8")
    coder: list[str] = []
    reviewer: list[str] = []
    for index, verdict in enumerate(verdicts):
        path = "site/other.css" if unrelated and index > 0 else "site/index.html"
        if index not in (no_change or set()):
            coder.extend(
                [
                    tool("filesystem.write", path, content=f"revision {index}"),
                    tool("filesystem.read", path),
                ]
            )
        coder.append(finish())
        reviewer.extend(
            [
                tool("filesystem.read", "site/index.html"),
                tool("filesystem.read", "site/qa/report.json"),
                review(verdict),
            ]
        )
    model = Recording(script={"coder": coder, "reviewer": reviewer})
    for worker in runtime.workers.values():
        worker.model = model
    return calls, model


@pytest.mark.parametrize(
    "verdicts,repairs",
    [
        (["fail", "fail", "fail", "pass", "pass"], 3),
        (["pass", "fail", "pass"], 1),
    ],
)
async def test_unlimited_fresh_cycles_polish_and_final_pass(
    runtime: Runtime,
    verdicts: list[str],
    repairs: int,
) -> None:
    calls, model = quality_fixture(runtime, verdicts)
    task = await runtime.tasks.create(
        "Improve page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
            long_run_quality=True,
        ),
    )
    await runtime.events.emit(
        EventType.MODEL_RESPONSE,
        task_id=task.id,
        actor="history",
        historical_marker="DO_NOT_RESEND_OLD_HISTORY",
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.COMPLETED, result.result
    assert result.result and result.result["real_repair_cycles"] == repairs
    assert len(calls) == len(verdicts)
    assert result.result["review"]["verdict"] == "pass"
    assert result.result["acceptance"]["status"] == "accepted"
    assert result.result["workflow"]["polish_done"]
    openings = [
        r.messages[0].content
        for r in model.requests
        if r.metadata["agent"] == "coder" and r.metadata["step"] == "1"
    ]
    assert len(openings) == len(verdicts)
    assert sum("The project currently passes. Do not rebuild it." in s for s in openings) == 1
    assert all("DO_NOT_RESEND_OLD_HISTORY" not in s for s in openings)
    assert max(map(len, openings)) < 42000
    assert all(len(r.messages) == 1 for r in model.requests if r.metadata["step"] == "1")
    cycles = [
        e for e in await runtime.tasks.events_for(task.id) if e.type == EventType.WORKFLOW_CYCLE
    ]
    assert len(cycles) == len(verdicts)
    for event in cycles:
        assert {
            "cycle_number",
            "files_read",
            "files_modified",
            "tools_executed",
            "browser_QA_performed",
            "review_verdict",
            "critical_findings",
            "major_findings",
            "minor_findings",
            "acceptance_state",
        } <= event.payload.keys()


async def test_three_noops_stall_without_consuming_repairs(runtime: Runtime) -> None:
    calls, _ = quality_fixture(runtime, ["fail"] * 4, no_change={1, 2, 3})
    task = await runtime.tasks.create(
        "Improve page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
        ),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.STALLED
    assert result.result and result.result["real_repair_cycles"] == 0
    assert result.result["acceptance"]["status"] == "stalled"
    assert len(calls) == 1
    assert [a["reason_code"] for a in result.result["repair_attempts"][1:]] == [
        "REPAIR_NOT_PERFORMED"
    ] * 3


async def test_identical_findings_unrelated_changes_stall(runtime: Runtime) -> None:
    quality_fixture(runtime, ["fail"] * 5, unrelated=True)
    task = await runtime.tasks.create(
        "Improve page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
        ),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.STALLED
    assert result.result and result.result["workflow"]["stop_reason"] == (
        "identical_findings_without_related_changes"
    )


async def test_unlimited_cancellation_is_immediate(runtime: Runtime) -> None:
    started = asyncio.Event()

    class Waiting(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            started.set()
            await asyncio.Event().wait()

    runtime.workers["coder"].model = Waiting()
    task = await runtime.tasks.create(
        "Work",
        created_by="test",
        options=TaskOptions(
            agent="coder",
            max_steps=0,
        ),
    )
    runtime.schedule_task(task.id)
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(runtime.tasks.cancel(task.id, actor="operator"), 2)
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.CANCELLED


async def test_sqlite_checkpoint_recovered_paused(runtime: Runtime, tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "recovery.db")
    manager = TaskManager(store=store, events=runtime.events)
    task = await manager.create(
        "Quality",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
        ),
    )
    await manager.transition(task.id, TaskStatus.PLANNING)
    await manager.transition(task.id, TaskStatus.RUNNING)
    checkpoint = {
        "workflow": {
            "cycle_number": 7,
            "stage": "coder_repair",
            "mode": "unlimited",
            "latest_review": {"verdict": "fail"},
            "latest_qa": {"error": "overflow"},
            "acceptance": {"status": "rejected"},
        }
    }
    await manager.record_result(task.id, checkpoint)
    restarted = TaskManager(store=SQLiteTaskStore(tmp_path / "recovery.db"), events=runtime.events)
    assert await restarted.reconcile_startup() == 1
    recovered = await restarted.get(task.id)
    assert recovered.status == TaskStatus.PAUSED and recovered.result == checkpoint
    assert recovered.options.max_repair_cycles is None


async def test_operator_resume_validates_before_repair(runtime: Runtime) -> None:
    calls, _ = quality_fixture(runtime, ["pass"], no_change={0})
    (runtime.settings.workspace_root / "site/index.html").write_text("existing", encoding="utf-8")
    task = await runtime.tasks.create(
        "Improve page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
        ),
    )
    await runtime.tasks.transition(task.id, TaskStatus.PAUSED)
    await runtime.tasks.record_result(
        task.id, {"workflow": {"cycle_number": 7, "real_repair_cycles": 4, "polish_done": True}}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
    ) as client:
        assert (
            await client.post(f"/tasks/{task.id}/resume", headers=VIEWER_AUTH)
        ).status_code == 403
        response = await client.post(f"/tasks/{task.id}/resume", headers=AUTH)
        assert response.status_code == 200
        for _ in range(100):
            if task.id not in runtime._jobs:
                break
            await asyncio.sleep(0.01)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.COMPLETED, result.result
    assert result.result and result.result["real_repair_cycles"] == 4
    assert result.result["workflow"]["cycle_number"] == 8
    assert calls == ["qa"]
    assert not result.result["evidence"]["files_modified"]


async def test_polish_can_finish_without_unjustified_writes(runtime: Runtime) -> None:
    calls, _ = quality_fixture(runtime, ["pass", "pass"], no_change={1})
    task = await runtime.tasks.create(
        "Improve page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=None,
            long_run_quality=True,
        ),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert result.result and result.result["real_repair_cycles"] == 0
    assert len(calls) == 2
    assert result.result["repair_attempts"][-1]["files_modified"] == []


async def test_nonvisual_unlimited_can_resume_fresh(runtime: Runtime) -> None:
    model = Recording(script={"coder": [tool("filesystem.read", "README.md"), finish()]})
    runtime.workers["coder"].model = model
    task = await runtime.tasks.create(
        "Inspect",
        created_by="test",
        options=TaskOptions(
            agent="coder",
            max_steps=0,
        ),
    )
    await runtime.tasks.reconcile_startup()
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.PAUSED
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
    ) as client:
        assert (await client.post(f"/tasks/{task.id}/resume", headers=AUTH)).status_code == 200
        for _ in range(100):
            if task.id not in runtime._jobs:
                break
            await asyncio.sleep(0.01)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.COMPLETED and result.error is None
    assert "Operator resumed this task" in model.requests[0].messages[0].content
