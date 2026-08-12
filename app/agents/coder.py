"""Makes changes — inside the workspace, and nowhere else."""

from __future__ import annotations

from app.agents.base import WorkerAgent
from app.policy.permissions import AgentRole


class CoderAgent(WorkerAgent):
    name = "coder"
    role = AgentRole.CODER
    #: Write access is scoped to the configured workspace by the filesystem
    #: tool itself. There is no shell entry in this list because there is no
    #: shell capability in the runtime at all.
    allowed_tools = frozenset(
        {
            "filesystem.read",
            "filesystem.list",
            "filesystem.write",
            "tests.run",
            "system.request_privileged_action",
        }
    )
    mandate = (
        "You implement changes inside the workspace and validate them with the "
        "test suite. If a task genuinely needs a privileged system action, "
        "request it — a human decides, and you continue without it."
    )
