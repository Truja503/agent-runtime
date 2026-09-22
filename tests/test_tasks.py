"""Task state machine, manager, and the end-to-end supervisor flow."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.container import Runtime
from app.errors import InvalidTaskTransitionError, TaskNotFoundError
from app.models.scripted import ScriptedModelProvider
from app.observability.events import EventBus, EventType
from app.observability.store import InMemoryEventStore
from app.tasks.manager import TaskManager
from app.tasks.state import TERMINAL_STATUSES, TaskStatus, can_transition
from app.tasks.store import InMemoryTaskStore


@pytest.fixture
def manager() -> TaskManager:
    return TaskManager(InMemoryTaskStore(), EventBus([InMemoryEventStore()]))


def test_transition_table_is_closed_over_terminal_states() -> None:
    for status in TERMINAL_STATUSES:
        for target in TaskStatus:
            resumable = status in {TaskStatus.PAUSED, TaskStatus.STALLED}
            assert can_transition(status, target) == (
                resumable and target in {TaskStatus.CREATED, TaskStatus.CANCELLED}
            )


def test_legal_and_illegal_transitions() -> None:
    assert can_transition(TaskStatus.CREATED, TaskStatus.PLANNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_FOR_APPROVAL)
    assert not can_transition(TaskStatus.CREATED, TaskStatus.COMPLETED)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)


async def test_create_get_and_list(manager: TaskManager) -> None:
    task = await manager.create("do the thing", created_by="tester")
    assert task.status is TaskStatus.CREATED
    assert (await manager.get(task.id)).goal == "do the thing"
    assert [t.id for t in await manager.list_tasks()] == [task.id]


async def test_unknown_task_raises(manager: TaskManager) -> None:
    with pytest.raises(TaskNotFoundError):
        await manager.get("nope")


async def test_illegal_transition_is_refused(manager: TaskManager) -> None:
    task = await manager.create("goal", created_by="tester")
    with pytest.raises(InvalidTaskTransitionError):
        await manager.transition(task.id, TaskStatus.COMPLETED)


async def test_completion_records_the_result(manager: TaskManager) -> None:
    task = await manager.create("goal", created_by="tester")
    await manager.transition(task.id, TaskStatus.PLANNING)
    await manager.transition(task.id, TaskStatus.RUNNING)
    completed = await manager.complete(task.id, {"summary": "done"})
    assert completed.status is TaskStatus.COMPLETED
    assert completed.result == {"summary": "done"}


async def test_failure_records_the_reason(manager: TaskManager) -> None:
    task = await manager.create("goal", created_by="tester")
    failed = await manager.fail(task.id, "provider unreachable")
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "provider unreachable"


async def test_events_are_attached_to_the_task(manager: TaskManager) -> None:
    task = await manager.create("goal", created_by="tester")
    await manager.transition(task.id, TaskStatus.PLANNING)
    events = await manager.events_for(task.id)
    assert [event.type for event in events] == [
        EventType.TASK_CREATED,
        EventType.TASK_STATUS_CHANGED,
    ]


# --- full runtime flow ----------------------------------------------------


async def test_task_runs_end_to_end_and_completes(runtime: Runtime) -> None:
    task = await runtime.tasks.create("Review this project", created_by="tester")
    await runtime.run_task(task.id)

    finished = await runtime.tasks.get(task.id)
    assert finished.status is TaskStatus.COMPLETED
    assert finished.result is not None
    assert finished.result["workers"]

    types = {event.type for event in await runtime.tasks.events_for(task.id)}
    assert {
        EventType.TASK_CREATED,
        EventType.AGENT_STARTED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_ALLOWED,
        EventType.TOOL_COMPLETED,
        EventType.TASK_COMPLETED,
    } <= types


async def test_task_parks_in_waiting_for_approval(runtime: Runtime) -> None:
    """A privileged request stops the task; only a human can move it on."""
    runtime.supervisor._workers = {"coder": runtime.workers["coder"]}  # noqa: SLF001
    runtime.workers["coder"].model = ScriptedModelProvider(  # type: ignore[attr-defined]
        script={
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "system.request_privileged_action",
                        "arguments": {"request": "restart nginx"},
                    }
                ),
                json.dumps({"action": "finish", "summary": "asked for approval"}),
            ]
        }
    )
    runtime.supervisor.model = ScriptedModelProvider(
        script={"supervisor": [json.dumps({"workers": ["coder"], "plan": "ask a human"})]}
    )

    task = await runtime.tasks.create("restart nginx on the box", created_by="tester")
    job = runtime.schedule_task(task.id)
    for _ in range(100):
        if (await runtime.tasks.get(task.id)).status is TaskStatus.WAITING_FOR_APPROVAL:
            break
        await asyncio.sleep(0.01)

    parked = await runtime.tasks.get(task.id)
    assert parked.status is TaskStatus.WAITING_FOR_APPROVAL

    types = {event.type for event in await runtime.tasks.events_for(task.id)}
    assert EventType.PRIVILEGED_ACTION_REQUESTED in types
    assert EventType.PRIVILEGED_ACTION_EXECUTED not in types
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)


async def test_model_failure_fails_the_task_cleanly(runtime: Runtime) -> None:
    class BrokenModel(ScriptedModelProvider):
        async def generate(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider exploded")

    runtime.supervisor.model = BrokenModel()
    task = await runtime.tasks.create("anything", created_by="tester")
    await runtime.run_task(task.id)

    failed = await runtime.tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "provider exploded" in (failed.error or "")
