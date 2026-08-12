"""Inspects and validates. Cannot change what it is reviewing."""

from __future__ import annotations

from app.agents.base import WorkerAgent
from app.policy.permissions import AgentRole


class ReviewerAgent(WorkerAgent):
    name = "reviewer"
    role = AgentRole.REVIEWER
    #: Read and run tests. No write: a reviewer that can edit the code it is
    #: reviewing is not a reviewer.
    allowed_tools = frozenset({"filesystem.read", "filesystem.list", "tests.run"})
    mandate = (
        "You verify work: read what changed, run the tests, and report whether "
        "the result holds up. State problems plainly instead of fixing them."
    )
