"""Task lifecycle: create, transition, record results, attach events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.errors import InvalidTaskTransitionError, TaskNotFoundError
from app.observability.events import Event, EventBus, EventType
from app.tasks.state import (
    RECOVERABLE_STATUSES,
    Task,
    TaskOptions,
    TaskStatus,
    TaskStore,
    can_transition,
)


class TaskManager:
    def __init__(self, store: TaskStore, events: EventBus) -> None:
        self._store = store
        self._events = events
        self.stop_execution: Callable[[str], Awaitable[None]] | None = None

    async def create(
        self, goal: str, *, created_by: str, options: TaskOptions | None = None
    ) -> Task:
        task = Task(goal=goal, created_by=created_by, options=options or TaskOptions())
        await self._store.create(task)
        await self._events.emit(
            EventType.TASK_CREATED,
            task_id=task.id,
            actor=created_by,
            goal=goal,
        )
        return task

    async def get(self, task_id: str) -> Task:
        task = await self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"no task with id {task_id}")
        return task

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        return await self._store.list_tasks(limit)

    async def waiting_tasks(self) -> list[Task]:
        return await self._store.list_by_status(frozenset({TaskStatus.WAITING_FOR_APPROVAL}))

    async def transition(self, task_id: str, target: TaskStatus) -> Task:
        task = await self.get(task_id)
        if task.status is target:
            return task
        if target == TaskStatus.COMPLETED:
            self.check_completion(task.result or {})
        if not can_transition(task.status, target):
            raise InvalidTaskTransitionError(
                f"cannot move task {task_id} from {task.status} to {target}"
            )
        previous = task.status
        task.status = target
        if target == TaskStatus.CREATED:
            task.error = None
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)
        await self._events.emit(
            EventType.TASK_STATUS_CHANGED,
            task_id=task_id,
            actor="task_manager",
            previous=previous.value,
            current=target.value,
        )
        return task

    async def complete(self, task_id: str, result: dict[str, Any]) -> Task:
        task = await self.get(task_id)
        self.check_completion(result)
        if not can_transition(task.status, TaskStatus.COMPLETED):
            raise InvalidTaskTransitionError(f"cannot complete task {task_id} from {task.status}")
        task.status = TaskStatus.COMPLETED
        task.result = result
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)
        await self._events.emit(
            EventType.TASK_COMPLETED, task_id=task_id, actor="task_manager", result=result
        )
        return task

    @staticmethod
    def check_completion(result: dict[str, Any]) -> None:
        if (
            result.get("acceptance", {}).get("status") in {"rejected", "stalled"}
            or result.get("acceptance", {}).get("failures")
            or result.get("pending_approvals")
            or (
                result.get("phase_execution")
                and result.get("acceptance", {}).get("status") != "accepted"
            )
            or any(
                p["status"] != "passed" or p.get("pending_approvals")
                for p in result.get("phase_execution", {}).get("phases", [])
            )
        ):
            raise InvalidTaskTransitionError(
                "cannot complete task with unmet evidence or approvals"
            )

    async def resume_failed_phase(self, task_id: str) -> Task:
        task = await self.get(task_id)
        if task.status != TaskStatus.FAILED or not (task.result or {}).get("phase_execution"):
            raise InvalidTaskTransitionError("only persisted phased failures are recoverable")
        task.status, task.error = TaskStatus.CREATED, None
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)
        await self._events.emit(
            EventType.TASK_STATUS_CHANGED,
            task_id=task_id,
            actor="task_manager",
            previous="failed",
            current="created",
            reason="operator phase resume",
        )
        return task

    async def fail(self, task_id: str, reason: str) -> Task:
        task = await self.get(task_id)
        # Failing is always permitted from a non-terminal state; a task that is
        # already finished keeps its original outcome.
        if not can_transition(task.status, TaskStatus.FAILED):
            return task
        task.status = TaskStatus.FAILED
        task.error = reason
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)
        await self._events.emit(
            EventType.TASK_FAILED, task_id=task_id, actor="task_manager", reason=reason
        )
        return task

    async def record_result(self, task_id: str, result: dict[str, Any]) -> None:
        task = await self.get(task_id)
        task.result = result
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)

    async def cancel(self, task_id: str, *, actor: str) -> Task:
        task = await self.get(task_id)
        if not can_transition(task.status, TaskStatus.CANCELLED):
            raise InvalidTaskTransitionError(f"cannot cancel task {task_id} from {task.status}")
        if self.stop_execution:
            await self.stop_execution(task_id)
        task = await self.get(task_id)
        if not can_transition(task.status, TaskStatus.CANCELLED):
            raise InvalidTaskTransitionError(f"cannot cancel task from {task.status}")
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now(UTC)
        await self._store.update(task)
        await self._events.emit(EventType.TASK_CANCELLED, task_id=task_id, actor=actor)
        return task

    async def events_for(self, task_id: str) -> list[Event]:
        await self.get(task_id)  # 404 rather than an empty list for an unknown id
        return await self._events.list_for_task(task_id)

    async def reconcile_startup(self) -> int:
        """A new process owns no previous asyncio jobs. Never pretend to resume."""
        count = 0
        while batch := await self._store.list_by_status(RECOVERABLE_STATUSES):
            for task in batch:
                previous = task.status
                task.status = (
                    TaskStatus.PAUSED
                    if task.options.recoverable_execution
                    or (task.result or {}).get("phase_execution")
                    else TaskStatus.INTERRUPTED
                )
                task.error = "Runtime restarted before task completion."
                task.updated_at = datetime.now(UTC)
                execution = (task.result or {}).get("phase_execution")
                if execution:
                    execution["status"] = "paused"
                    phase = execution["phases"][execution["current_phase_index"]]
                    if phase["status"] != "passed":
                        phase.update(status="paused", stop_reason="runtime_restart")
                    plan = execution["plan"]["phases"][execution["current_phase_index"]]
                    await self._events.emit(
                        EventType.PHASE_PAUSED,
                        task_id=task.id,
                        actor="task_manager",
                        phase_id=plan["id"],
                        phase_title=plan["title"],
                        phase_index=execution["current_phase_index"] + 1,
                        phase_attempt=phase["attempt"],
                        reason="runtime_restart",
                    )
                await self._store.update(task)
                await self._events.emit(
                    EventType.TASK_INTERRUPTED,
                    task_id=task.id,
                    actor="task_manager",
                    previous=previous.value,
                    current=task.status.value,
                    reason=task.error,
                )
                count += 1
        for task in await self.waiting_tasks():
            if task.options.recoverable_execution or (task.result or {}).get("phase_execution"):
                execution = (task.result or {}).get("phase_execution")
                if execution:
                    execution["status"] = "paused"
                    execution["phases"][execution["current_phase_index"]]["status"] = "paused"
                    await self._store.update(task)
                await self.transition(task.id, TaskStatus.PAUSED)
                count += 1
        return count
