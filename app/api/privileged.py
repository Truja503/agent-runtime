"""Privileged approval endpoints — off by default.

Read this router as the *optional* alternative to the CLI. When
``PRIVILEGED_API_ENABLED`` is false (the default) it is not mounted at all, so
``POST /privileged/requests/{id}/approve`` returns 404 rather than existing and
refusing.

When it is mounted, note what authenticates it: operator headers, checked by
:mod:`privileged.auth` against scrypt hashes. A bearer token from the normal API
is not accepted and is not even consulted. Someone holding an API session
cannot escalate by calling this endpoint; they need a credential the
application never issues.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_runtime
from app.container import Runtime
from privileged.policy import ExecutionRefused, IntentRejected
from privileged.schemas import PrivilegedRequest
from privileged.service import NotAuthenticated

router = APIRouter(prefix="/privileged", tags=["privileged"])


class OperatorCredentials(BaseModel):
    operator_id: str
    secret: str


class DenyBody(BaseModel):
    reason: str = Field(default="", max_length=500)


def operator_credentials(
    x_operator_id: str = Header(..., alias="X-Operator-Id"),
    x_operator_secret: str = Header(..., alias="X-Operator-Secret"),
) -> OperatorCredentials:
    """Second authority. Deliberately not the API bearer token."""
    return OperatorCredentials(operator_id=x_operator_id, secret=x_operator_secret)


def _view(request: PrivilegedRequest) -> dict[str, object]:
    return {
        "request_id": request.request_id,
        "status": request.status.value,
        "requested_by": request.requested_by,
        "task_id": request.task_id,
        "request_text": request.request_text,
        "action": request.intent.action.value if request.intent else None,
        "service": request.intent.service if request.intent else None,
        "reason": request.reason,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "approved_by": request.approved_by,
        "exit_code": request.result.exit_code if request.result else None,
    }


@router.get("/requests")
async def list_pending(
    runtime: Runtime = Depends(get_runtime),
    credentials: OperatorCredentials = Depends(operator_credentials),
) -> list[dict[str, object]]:
    # Listing is itself operator-only: pending requests describe intent about
    # production systems.
    _authenticate(runtime, credentials)
    pending = await runtime.privileged_service.list_pending()
    return [_view(request) for request in pending]


@router.post("/requests/{request_id}/approve")
async def approve(
    request_id: str,
    runtime: Runtime = Depends(get_runtime),
    credentials: OperatorCredentials = Depends(operator_credentials),
) -> dict[str, object]:
    try:
        record = await runtime.privileged_service.approve_and_execute(
            request_id=request_id,
            operator_id=credentials.operator_id,
            secret=credentials.secret,
        )
    except NotAuthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid operator credentials"
        ) from None
    except (ExecutionRefused, IntentRejected) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _view(record)


@router.post("/requests/{request_id}/deny")
async def deny(
    request_id: str,
    body: DenyBody,
    runtime: Runtime = Depends(get_runtime),
    credentials: OperatorCredentials = Depends(operator_credentials),
) -> dict[str, object]:
    try:
        record = await runtime.privileged_service.deny(
            request_id=request_id,
            operator_id=credentials.operator_id,
            secret=credentials.secret,
            reason=body.reason,
        )
    except NotAuthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid operator credentials"
        ) from None
    except ExecutionRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _view(record)


def _authenticate(runtime: Runtime, credentials: OperatorCredentials) -> None:
    service = runtime.privileged_service
    try:
        service.authenticate_operator(credentials.operator_id, credentials.secret)
    except NotAuthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid operator credentials"
        ) from None
