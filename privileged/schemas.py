"""Structured privileged intent.

Everything that crosses into this domain must end up as one of the models
below. There is no free-text field that reaches a shell, and no ``command``
action — the closed set *is* the capability list.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

#: Service names are validated for shape here and for membership in
#: ``privileged.policy.ALLOWED_SERVICES``. Both checks must pass.
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")


class PrivilegedAction(StrEnum):
    READ_SERVICE_STATUS = "read_service_status"
    READ_SERVICE_LOGS = "read_service_logs"
    RESTART_SERVICE = "restart_service"


class _ServiceIntent(BaseModel):
    service: str = Field(max_length=64)


class ReadServiceStatusIntent(_ServiceIntent):
    action: Literal[PrivilegedAction.READ_SERVICE_STATUS] = (
        PrivilegedAction.READ_SERVICE_STATUS
    )


class ReadServiceLogsIntent(_ServiceIntent):
    action: Literal[PrivilegedAction.READ_SERVICE_LOGS] = (
        PrivilegedAction.READ_SERVICE_LOGS
    )
    lines: int = Field(default=100, ge=1, le=1000)


class RestartServiceIntent(_ServiceIntent):
    action: Literal[PrivilegedAction.RESTART_SERVICE] = PrivilegedAction.RESTART_SERVICE


PrivilegedIntent = Annotated[
    ReadServiceStatusIntent | ReadServiceLogsIntent | RestartServiceIntent,
    Field(discriminator="action"),
]

INTENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(PrivilegedIntent)


class RequestStatus(StrEnum):
    #: Parsed and valid; a human must decide.
    AWAITING_APPROVAL = "awaiting_approval"
    #: Could not be parsed into a permitted action. Terminal.
    REJECTED = "rejected"
    #: A human declined it. Terminal.
    DENIED = "denied"
    #: Ran successfully. Terminal.
    EXECUTED = "executed"
    #: Approved and attempted, but the command failed. Terminal.
    FAILED = "failed"
    #: Nobody approved it in time. Terminal.
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {
        RequestStatus.REJECTED,
        RequestStatus.DENIED,
        RequestStatus.EXECUTED,
        RequestStatus.FAILED,
        RequestStatus.EXPIRED,
    }
)


class ExecutionResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    argv: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class PrivilegedRequest(BaseModel):
    """One request, from creation to outcome. This is the audit record."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    requested_by: str
    task_id: str | None = None
    #: The original natural-language ask, kept for the human who approves.
    request_text: str
    #: ``None`` when the text could not be mapped to a permitted action.
    intent: PrivilegedIntent | None = None
    status: RequestStatus = RequestStatus.AWAITING_APPROVAL
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    #: The human. Never an agent, never a model.
    approved_by: str | None = None
    result: ExecutionResult | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) > self.expires_at
