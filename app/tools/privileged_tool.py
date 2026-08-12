"""The privileged *request* capability.

This is the only privileged entry in the registry, and it has no execution
path. The broker routes it to the gateway before a handler is ever reached; the
handler below exists solely as a tripwire, so that if some future refactor
manages to dispatch a privileged tool normally, the process raises instead of
doing something privileged.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.errors import PermissionDeniedError


class RequestPrivilegedActionArgs(BaseModel):
    """Natural language in, structured intent out — parsed on the other side.

    The text is never turned into a command here. It crosses the boundary as
    data, and the privileged domain parses it with a *local* model and then
    validates the result against a closed set of actions.
    """

    request: str = Field(min_length=1, max_length=500)


async def refuse(_: BaseModel) -> dict[str, Any]:
    raise PermissionDeniedError(
        "privileged actions are never executed in the application process; "
        "this tool only creates a request for a human operator"
    )
