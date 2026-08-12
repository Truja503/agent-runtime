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
    allowed_tools = frozenset({"filesystem.read", "filesystem.list", "github.read"})
    mandate = (
        "You gather and summarise information. You never modify anything; "
        "if a change is needed, say so in your summary and let a coder do it."
    )
