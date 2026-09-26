"""The audit trail.

Every decision the runtime makes about authority leaves a record here: what was
requested, by whom, whether it was allowed, and what happened. If you can't
reconstruct a privileged action from these events, the design has failed.

Redaction is applied at construction time, not at read time — a secret that
reaches the store is already leaked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.observability.context import CURRENT_PHASE

#: Substrings that mark a value as unloggable. Matched case-insensitively
#: against payload keys.
_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
    "private_key",
    "cookie",
)

#: Prompts and model output can contain anything a user pasted, so they are
#: never stored verbatim. Only length and a short excerpt survive.
_TRUNCATE_AT = 240


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_STATUS_CHANGED = "task_status_changed"
    WORKFLOW_CYCLE = "workflow_cycle"
    PHASE_PLANNED = "phase_planned"
    PHASE_STARTED = "phase_started"
    PHASE_WORKER_STARTED = "phase_worker_started"
    PHASE_VERIFICATION_STARTED = "phase_verification_started"
    PHASE_COMPLETION_REJECTED = "phase_completion_rejected"
    PHASE_REPAIR_STARTED = "phase_repair_started"
    PHASE_PASSED = "phase_passed"
    PHASE_FAILED = "phase_failed"
    PHASE_STALLED = "phase_stalled"
    PHASE_PAUSED = "phase_paused"
    PHASE_RESUMED = "phase_resumed"

    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"

    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    MODEL_ERROR = "model_error"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_RETRY = "model_retry"
    MODEL_FAILED = "model_failed"
    MODEL_INVALID_RESPONSE = "model_invalid_response"
    CONFIGURATION_CHANGED = "configuration_changed"
    WEB_EGRESS = "web_egress"

    TOOL_REQUESTED = "tool_requested"
    TOOL_ALLOWED = "tool_allowed"
    TOOL_DENIED = "tool_denied"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"

    PRIVILEGED_ACTION_REQUESTED = "privileged_action_requested"
    PRIVILEGED_ACTION_APPROVED = "privileged_action_approved"
    PRIVILEGED_ACTION_DENIED = "privileged_action_denied"
    PRIVILEGED_ACTION_EXECUTED = "privileged_action_executed"
    PRIVILEGED_ACTION_FAILED = "privileged_action_failed"


def redact(value: Any, *, key: str = "") -> Any:
    """Return a version of ``value`` that is safe to persist."""
    lowered = key.lower()
    if lowered == "external_data_sent" and isinstance(value, str):
        return value[:2048]
    if lowered in {"input_tokens", "output_tokens"} and type(value) is int:
        return value
    if lowered in {"input_tokens", "output_tokens"} and type(value) is int:
        return value
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key) for item in value]
    if isinstance(value, str) and len(value) > _TRUNCATE_AT:
        return f"{value[:_TRUNCATE_AT]}… [+{len(value) - _TRUNCATE_AT} chars]"
    return value


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    """Anything that can durably accept events."""

    async def append(self, event: Event) -> None: ...

    async def list_for_task(self, task_id: str) -> list[Event]: ...


class EventBus:
    """Fan-out to one or more sinks, with redaction applied once, up front."""

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = sinks
        self.privacy: Callable[[Any], Any] = lambda value: value

    async def emit(
        self,
        event_type: EventType,
        *,
        task_id: str | None = None,
        actor: str | None = None,
        **payload: Any,
    ) -> Event:
        phase = CURRENT_PHASE.get()
        if phase and phase.task_id == task_id:
            payload.update(phase.metadata())
        elif task_id and payload.get("request_id"):
            # Operator approval happens in a different request/context, possibly
            # after restart. Recover its association from the durable request event.
            for previous in reversed(await self.list_for_task(task_id)):
                if previous.payload.get("request_id") == payload[
                    "request_id"
                ] and previous.payload.get("phase_id"):
                    payload.update(
                        {k: v for k, v in previous.payload.items() if k.startswith("phase_")}
                    )
                    break
        event = Event(
            type=event_type,
            task_id=task_id,
            actor=actor,
            payload={key: redact(value, key=key) for key, value in self.privacy(payload).items()},
        )
        for sink in self._sinks:
            await sink.append(event)
        if phase and phase.task_id == task_id and phase.observe:
            await phase.observe(event)
        return event

    async def list_for_task(self, task_id: str) -> list[Event]:
        """Read back from the first sink; sinks are ordered most-durable-first."""
        if not self._sinks:
            return []
        return await self._sinks[0].list_for_task(task_id)
