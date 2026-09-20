"""The privileged request lifecycle.

    request (agent)  →  parse (local model)  →  validate (deterministic)
                     →  human authentication  →  human approval
                     →  policy re-check       →  executor

``create_request`` is the only method the application side can reach, via
``app.privileged_bridge``. Everything below it requires an operator credential
that the application process does not have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from privileged.audit import AuditSink, NullAuditSink
from privileged.auth import Operator, OperatorAuthenticator
from privileged.executor import CommandNotAvailable, PrivilegedExecutor
from privileged.local_llm import IntentParser, RuleBasedIntentParser
from privileged.policy import (
    ExecutionRefused,
    IntentRejected,
    authorize_execution,
    validate_intent,
)
from privileged.schemas import PrivilegedRequest, RequestStatus
from privileged.store import PrivilegedRequestStore


class NotAuthenticated(Exception):
    """Bad or missing operator credentials."""


class PrivilegedRequestService:
    def __init__(
        self,
        *,
        store: PrivilegedRequestStore,
        authenticator: OperatorAuthenticator,
        executor: PrivilegedExecutor,
        parser: IntentParser | None = None,
        audit: AuditSink | None = None,
        approval_ttl_seconds: int = 900,
    ) -> None:
        self._store = store
        self._authenticator = authenticator
        self._executor = executor
        self._parser = parser or RuleBasedIntentParser()
        self._audit = audit or NullAuditSink()
        self._ttl = timedelta(seconds=approval_ttl_seconds)

    # -- agent-reachable ---------------------------------------------------

    async def create_request(
        self,
        *,
        requested_by: str,
        request_text: str,
        task_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PrivilegedRequest:
        """Record an intent to act. Executes nothing, ever."""
        now = datetime.now(UTC)
        request = PrivilegedRequest(
            requested_by=requested_by,
            task_id=task_id,
            request_text=request_text[:500],
            context=context or {},
            created_at=now,
            expires_at=now + self._ttl,
        )

        proposal = await self._parser.parse(request.request_text)
        if proposal is None:
            request.status = RequestStatus.REJECTED
            request.reason = "could not map the request onto a permitted action"
        else:
            try:
                request.intent = validate_intent(proposal)
            except IntentRejected as exc:
                request.status = RequestStatus.REJECTED
                request.reason = str(exc)

        await self._store.save(request)
        await self._audit.record(
            "privileged_action_requested",
            {
                "request_id": request.request_id,
                "requested_by": requested_by,
                "task_id": task_id,
                "status": request.status.value,
                "action": request.intent.action.value if request.intent else None,
                "service": request.intent.service if request.intent else None,
                "reason": request.reason,
            },
        )
        return request

    async def get_request(self, request_id: str) -> PrivilegedRequest | None:
        return await self._store.get(request_id)

    async def list_pending(self, limit: int = 50) -> list[PrivilegedRequest]:
        return await self._store.list_by_status(RequestStatus.AWAITING_APPROVAL, limit)

    # -- operator-only -----------------------------------------------------

    def authenticate_operator(self, operator_id: str, secret: str) -> Operator:
        operator = self._authenticator.authenticate(operator_id, secret)
        if operator is None:
            raise NotAuthenticated("invalid operator credentials")
        return operator

    async def approve_and_execute(
        self, *, request_id: str, operator_id: str, secret: str
    ) -> PrivilegedRequest:
        """Authenticate a human, re-check policy, then run the command."""
        operator = self.authenticate_operator(operator_id, secret)

        request = await self._store.get(request_id)
        if request is None:
            raise ExecutionRefused(f"no such request: {request_id}")

        if request.is_expired() and request.status is RequestStatus.AWAITING_APPROVAL:
            request.status = RequestStatus.EXPIRED
            request.reason = "approval window elapsed"
            request.decided_at = datetime.now(UTC)
            await self._store.save(request)

        try:
            intent = authorize_execution(request, operator_id=operator.operator_id)
        except (ExecutionRefused, IntentRejected) as exc:
            await self._audit.record(
                "privileged_action_denied",
                {
                    "request_id": request_id, "task_id": request.task_id,
                    "operator": operator.operator_id,
                    "reason": str(exc),
                },
            )
            raise

        await self._audit.record(
            "privileged_action_approved",
            {
                "request_id": request_id, "task_id": request.task_id,
                "operator": operator.operator_id,
                "action": intent.action.value,
                "service": intent.service,
            },
        )

        try:
            result = await self._executor.execute(intent)
        except CommandNotAvailable as exc:
            request.status = RequestStatus.FAILED
            request.reason = str(exc)
            request.approved_by = operator.operator_id
            request.decided_at = datetime.now(UTC)
            await self._store.save(request)
            await self._audit.record(
                "privileged_action_failed",
                {"request_id": request_id, "task_id": request.task_id, "reason": str(exc)},
            )
            return request

        request.result = result
        request.approved_by = operator.operator_id
        request.decided_at = datetime.now(UTC)
        request.status = RequestStatus.EXECUTED if result.succeeded else RequestStatus.FAILED
        await self._store.save(request)
        await self._audit.record(
            "privileged_action_executed"
            if result.succeeded
            else "privileged_action_failed",
            {
                "request_id": request_id, "task_id": request.task_id,
                "operator": operator.operator_id,
                "action": intent.action.value,
                "service": intent.service,
                "argv": result.argv,
                "exit_code": result.exit_code,
            },
        )
        return request

    async def deny(
        self, *, request_id: str, operator_id: str, secret: str, reason: str = ""
    ) -> PrivilegedRequest:
        operator = self.authenticate_operator(operator_id, secret)
        request = await self._store.get(request_id)
        if request is None:
            raise ExecutionRefused(f"no such request: {request_id}")
        if request.status is not RequestStatus.AWAITING_APPROVAL:
            raise ExecutionRefused(
                f"request {request_id} is {request.status.value}, not awaiting approval"
            )
        request.status = RequestStatus.DENIED
        request.reason = reason or "denied by operator"
        request.approved_by = operator.operator_id
        request.decided_at = datetime.now(UTC)
        await self._store.save(request)
        await self._audit.record(
            "privileged_action_denied",
            {
                "request_id": request_id, "task_id": request.task_id,
                "operator": operator.operator_id,
                "reason": request.reason,
            },
        )
        return request
