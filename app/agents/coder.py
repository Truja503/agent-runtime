"""Makes changes — inside the workspace, and nowhere else."""

from __future__ import annotations

from pydantic import Field

from app.agents.base import AgentDecision, WorkerAgent
from app.policy.permissions import AgentRole


class CoderDecision(AgentDecision):
    files_changed: list[str] = Field(default_factory=list, max_length=100)
    verification_requested: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)


class CoderAgent(WorkerAgent):
    name = "coder"
    decision_type = CoderDecision
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
            "project.inspect",
            "project.build",
            "project.serve",
            "project.stop",
            "project.test",
            "project.dependencies",
            "browser.preview",
            "browser.screenshot",
            "browser.console_errors",
            "system.request_privileged_action",
        }
    )
    mandate = (
        "For Flask projects declare project.json with exact versions of all dependencies, "
        "including transitive packages; export app from app.py and put tests in tests/. "
        "Use project.dependencies to request operator approval, project.build/project.test "
        "for isolated execution, and browser.screenshot for route QA. Never use tests.run "
        "for generated Flask code. No shell commands or executable selection are available. "
        "Use relative paths and . for the workspace root, never /. "
        "After the LAST write to each changed file, read all its pages back. "
        "Reserve time for required verification and a finish decision. "
        "Finish may include files_changed, verification_requested, and limitations; "
        "these are claims, not execution evidence. "
        "You implement changes inside the workspace and validate them with the "
        "test suite. If a task genuinely needs a privileged system action, "
        "request it — a human decides, and you continue without it."
    )
