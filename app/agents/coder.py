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
        "For generated Flask projects, project.json is NOT package.json. It is a strict data-only "
        "runtime manifest and may contain ONLY schema_version, framework, frontend, python, npm, "
        "and routes. Exact shape example: "
        "{\\\"schema_version\\\":1,\\\"framework\\\":\\\"flask\\\","
        "\\\"frontend\\\":\\\"none\\\",\\\"python\\\":{\\\"flask\\\":\\\"3.1.2\\\","
        "\\\"flask-sqlalchemy\\\":\\\"3.1.1\\\",\\\"pytest\\\":\\\"8.4.2\\\"},"
        "\\\"npm\\\":{},\\\"routes\\\":[\\\"/\\\"]}. "
        "Never add name, version, description, main, scripts, dependencies, devDependencies, "
        "repository, keywords, author, license, or other package.json fields. "
        "Use exact numeric dependency versions; export app from app.py and put tests in tests/. "
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
