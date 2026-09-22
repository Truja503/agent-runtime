"""The only doorway from the agent side into the privileged domain.

Note what this interface does **not** have: no ``approve``, no ``execute``, no
handle on the executor. The application process can create a request and read
its status. That is the entire surface. Approval and execution live behind a
separate credential and, in a real deployment, a separate process and OS user.

``tests/test_boundaries.py`` walks the import graph and fails the build if
anything under ``app/`` reaches past this module into the privileged internals.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from privileged.service import PrivilegedRequestService


class PrivilegedTicket(BaseModel):
    """What an agent is allowed to learn about its request: an id and a status."""

    request_id: str
    status: str
    action: str | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None


class PrivilegedGateway(Protocol):
    """Submit-and-poll only.

    An out-of-process implementation (Unix socket, HTTP over loopback to a
    daemon running as another user) satisfies the same protocol; nothing on the
    agent side would need to change.
    """

    async def submit(
        self,
        *,
        requested_by: str,
        request_text: str,
        task_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PrivilegedTicket: ...

    async def status(self, request_id: str) -> PrivilegedTicket | None: ...


class InProcessPrivilegedGateway:
    """In-process adapter used by the MVP.

    It holds a reference to the privileged service but re-exports only the two
    safe operations, so the narrow surface is enforced by this class rather
    than by convention.
    """

    def __init__(self, service: PrivilegedRequestService) -> None:
        self._service = service

    async def submit(
        self,
        *,
        requested_by: str,
        request_text: str,
        task_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PrivilegedTicket:
        record = await self._service.create_request(
            requested_by=requested_by,
            request_text=request_text,
            task_id=task_id,
            context=context or {},
        )
        return PrivilegedTicket(
            request_id=record.request_id,
            status=record.status.value,
            action=record.intent.action.value if record.intent else None,
        )

    async def status(self, request_id: str) -> PrivilegedTicket | None:
        record = await self._service.get_request(request_id)
        if record is None:
            return None
        return PrivilegedTicket(
            request_id=record.request_id,
            status=record.status.value,
            action=record.intent.action.value if record.intent else None,
            reason=record.reason,
            result=record.result.model_dump(mode="json") if record.result else None,
        )
