"""Read-only investigator."""

from __future__ import annotations

from app.agents.base import WorkerAgent
from app.policy.permissions import AgentRole


class ResearcherAgent(WorkerAgent):
    name = "researcher"
    role = AgentRole.RESEARCHER
    #: Read-only by construction. There is no write tool in this set, and the
    #: researcher role holds no write capability either — two independent
    #: reasons a researcher cannot modify anything.
    allowed_tools = frozenset({"filesystem.read", "filesystem.list", "github.read", "web.request"})
    mandate = (
        "Inspect only what is needed. Request only declared tools; never request writes. "
        "Use web.request for current public information; send only a short public query or URL. "
        "If a directory is missing, inspect its parent or workspace root (.). "
        "Return focused findings/context for the supervisor and coder. "
        "You gather and summarise information. You never modify anything; "
        "if a change is needed, say so in your summary and let a coder do it."
    )
