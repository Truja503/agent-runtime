"""Deterministic privileged policy.

Two questions live here, and nothing else does:

1. Is this a permitted action against a permitted target?
2. Is this request in a state where an authenticated human may execute it?

No model is consulted for either. The parser upstream may propose anything at
all; this module decides whether it becomes executable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from privileged.schemas import (
    INTENT_ADAPTER,
    SERVICE_NAME,
    PrivilegedAction,
    PrivilegedIntent,
    PrivilegedRequest,
    RequestStatus,
)

#: The complete list of services this runtime will touch. Adding a name here is
#: a deliberate, reviewable act.
ALLOWED_SERVICES: frozenset[str] = frozenset({"nginx", "postgresql", "redis"})

#: The complete list of permitted actions. Anything outside it — package
#: installation, user management, firewall changes, arbitrary shell — is not
#: "denied at runtime"; it simply does not exist as a capability.
ALLOWED_ACTIONS: frozenset[PrivilegedAction] = frozenset(PrivilegedAction)


class IntentRejected(Exception):
    """The proposed intent is not something this system will ever do."""


class ExecutionRefused(Exception):
    """The action is permitted, but this request may not run right now."""


def validate_intent(candidate: dict[str, Any] | PrivilegedIntent) -> PrivilegedIntent:
    """Turn an untrusted proposal into a validated intent, or refuse it.

    ``candidate`` typically comes from the local intent parser, which is a
    model — so it is treated exactly like any other untrusted input.
    """
    if isinstance(candidate, dict):
        try:
            intent = INTENT_ADAPTER.validate_python(candidate)
        except ValidationError as exc:
            raise IntentRejected(
                f"not a permitted privileged action ({exc.error_count()} problem(s))"
            ) from None
    else:
        intent = candidate

    if intent.action not in ALLOWED_ACTIONS:  # pragma: no cover - closed enum
        raise IntentRejected(f"action {intent.action.value!r} is not allowed")

    service = intent.service
    if not SERVICE_NAME.match(service):
        raise IntentRejected(f"malformed service name: {service!r}")
    if service not in ALLOWED_SERVICES:
        raise IntentRejected(
            f"service {service!r} is not in the allowlist "
            f"({', '.join(sorted(ALLOWED_SERVICES))})"
        )
    return intent


def authorize_execution(
    request: PrivilegedRequest,
    *,
    operator_id: str,
    now: datetime | None = None,
) -> PrivilegedIntent:
    """Final gate before the executor. Returns the intent that may run."""
    moment = now or datetime.now(UTC)

    if request.status is not RequestStatus.AWAITING_APPROVAL:
        raise ExecutionRefused(
            f"request {request.request_id} is {request.status.value}, not awaiting approval"
        )
    if request.intent is None:
        raise ExecutionRefused("request has no validated intent")
    if request.is_expired(moment):
        raise ExecutionRefused("approval window has expired")

    # A request cannot approve itself, and the requester cannot be the
    # approver. Agent names and operator ids are different namespaces, so this
    # is a belt-and-braces check on top of separate credentials.
    if request.requested_by == operator_id:
        raise ExecutionRefused("the requester may not approve their own request")

    # Re-validate rather than trusting what was stored: the allowlist may have
    # been tightened between request and approval.
    return validate_intent(request.intent)
