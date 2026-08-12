"""Request and response bodies.

Kept separate from the domain models so the wire format can change without
touching the runtime, and so nothing internal leaks into a response by
accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.observability.events import Event
from app.tasks.state import Task


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def of(cls, task: Task) -> TaskResponse:
        return cls(
            id=task.id,
            goal=task.goal,
            status=task.status.value,
            created_at=task.created_at,
            updated_at=task.updated_at,
            result=task.result,
            error=task.error,
        )


class EventResponse(BaseModel):
    id: str
    type: str
    timestamp: datetime
    actor: str | None
    payload: dict[str, Any]

    @classmethod
    def of(cls, event: Event) -> EventResponse:
        return cls(
            id=event.id,
            type=event.type.value,
            timestamp=event.timestamp,
            actor=event.actor,
            payload=event.payload,
        )


class HealthResponse(BaseModel):
    status: str
    provider: str
    model: str
    privileged_api_enabled: bool
    tools: list[str]


class PrivilegedRequestResponse(BaseModel):
    request_id: str
    status: str
    requested_by: str
    task_id: str | None
    request_text: str
    action: str | None
    service: str | None
    reason: str | None
    created_at: datetime
    expires_at: datetime | None
    approved_by: str | None
    exit_code: int | None = None
