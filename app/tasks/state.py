"""Task state machine.

Transitions are data, not scattered ``if`` statements, so an illegal move is
impossible to make by accident and trivial to test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from app.tasks.evidence import AcceptanceCriteria, file_key


class TaskOptions(BaseModel):
    agent: Literal["auto", "supervisor", "researcher", "coder", "reviewer"] = "auto"
    project: str = "default"
    max_steps: int | None = Field(default=None, ge=1)
    worker_steps_mode: Literal["profile", "custom", "unlimited"] = "profile"
    model_profile: str | None = None
    workspace: str = ""
    acceptance: AcceptanceCriteria = Field(default_factory=AcceptanceCriteria)
    visual_project: str | None = Field(default=None, min_length=1, max_length=1024)
    project_framework: Literal["static", "flask"] = "static"
    max_repair_cycles: int | None = Field(default=2, ge=0)
    long_run_quality: bool = False
    research_required: bool = False
    web_research_required: bool = False
    allow_degraded_research: bool = False

    @model_validator(mode="before")
    @classmethod
    def execution_limits(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        steps = value.get("max_steps")
        if steps in (0, "unlimited") or value.get("worker_steps_mode") == "unlimited":
            value.update(max_steps=None, worker_steps_mode="unlimited")
        elif steps in (None, ""):
            value.update(max_steps=None, worker_steps_mode="profile")
        else:
            value["worker_steps_mode"] = "custom"
        if value.get("max_repair_cycles") == "unlimited":
            value["max_repair_cycles"] = None
        return value

    @property
    def recoverable_execution(self) -> bool:
        return (
            self.long_run_quality
            or self.worker_steps_mode == "unlimited"
            or self.max_repair_cycles is None
        )

    @model_validator(mode="after")
    def visual_requirements(self) -> TaskOptions:
        if self.project_framework == "flask" and not self.visual_project:
            raise ValueError("Flask tasks require a confined visual_project")
        if self.visual_project:
            if self.agent not in {"auto", "supervisor"}:
                raise ValueError("visual QA workflow requires auto/supervisor routing")
            self.acceptance.required_visual_qa = True
            if self.project_framework == "flask":
                self.acceptance.required_project_toolchain = True
            self.acceptance.required_workers = list(
                dict.fromkeys([*self.acceptance.required_workers, "coder", "reviewer"])
            )
            self.acceptance.required_review_verdict = (
                self.acceptance.required_review_verdict or "pass_or_warnings"
            )
            if (
                self.long_run_quality
                or self.max_repair_cycles is None
                or self.project_framework == "flask"
            ):
                self.acceptance.required_review_verdict = "pass"
                self.acceptance.require_readback_all_modified = True
                prefix = self.visual_project.rstrip("/\\")
                entry = "app.py" if self.project_framework == "flask" else "index.html"
                self.acceptance.required_files = list(
                    dict.fromkeys(
                        [
                            *self.acceptance.required_files,
                            file_key(f"{prefix}/{entry}"),
                            file_key(f"{prefix}/qa/report.json"),
                        ]
                    )
                )
        return self


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    STALLED = "stalled"
    PAUSED = "paused"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
        TaskStatus.STALLED,
        TaskStatus.PAUSED,
    }
)

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.REVIEWING,
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.REVIEWING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.REVIEWING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.INTERRUPTED: frozenset(),
    TaskStatus.STALLED: frozenset({TaskStatus.CREATED, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.CREATED, TaskStatus.CANCELLED}),
}

RECOVERABLE_STATUSES = frozenset(
    {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.REVIEWING}
)
for _status in RECOVERABLE_STATUSES:
    ALLOWED_TRANSITIONS[_status] = ALLOWED_TRANSITIONS[_status] | {
        TaskStatus.INTERRUPTED,
        TaskStatus.PAUSED,
        TaskStatus.STALLED,
    }
ALLOWED_TRANSITIONS[TaskStatus.WAITING_FOR_APPROVAL] |= {TaskStatus.PAUSED, TaskStatus.STALLED}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = Field(min_length=1)
    status: TaskStatus = TaskStatus.CREATED
    created_by: str = "unknown"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result: dict[str, Any] | None = None
    error: str | None = None
    options: TaskOptions = Field(default_factory=TaskOptions)


class TaskStore(Protocol):
    """Storage seam: swap SQLite for Postgres later without touching the manager."""

    async def create(self, task: Task) -> Task: ...

    async def get(self, task_id: str) -> Task | None: ...

    async def update(self, task: Task) -> Task: ...

    async def list_tasks(self, limit: int = 50) -> list[Task]: ...

    async def list_by_status(
        self, statuses: frozenset[TaskStatus], limit: int = 100
    ) -> list[Task]: ...
