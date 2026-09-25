"""Where every capability of this runtime is declared, in one readable list."""

from __future__ import annotations

from functools import partial
from pathlib import Path

import httpx

from app.policy.permissions import Capability, RiskLevel
from app.tools.browser import BrowserArgs, BrowserTools
from app.tools.filesystem import (
    FilesystemTools,
    ListArgs,
    ReadArgs,
    Workspace,
    WriteArgs,
)
from app.tools.github import GitHubTools, ReadRepoArgs
from app.tools.privileged_tool import RequestPrivilegedActionArgs, refuse
from app.tools.project import ProjectToolchain
from app.tools.project_manifest import ProjectArgs
from app.tools.registry import ToolRegistry
from app.tools.testing import CommandRunner, ProjectTestTools, RunTestsArgs


def build_registry(
    *,
    workspace: Workspace,
    test_runner: CommandRunner | None = None,
    test_suites: dict[str, list[str]] | None = None,
    github_client: httpx.AsyncClient | None = None,
    project_root: Path | None = None,
    include_privileged: bool = True,
    projects: ProjectToolchain | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    filesystem = FilesystemTools(workspace)
    tests = ProjectTestTools(
        workspace_root=project_root or workspace.root,
        runner=test_runner,
        suites=test_suites,
    )
    github = GitHubTools(github_client)
    browser = BrowserTools(workspace, projects)
    for name in ("browser.preview", "browser.screenshot", "browser.console_errors"):
        registry.register(
            name=name,
            description=(
                "Render a confined static project with Chromium. Fixed desktop/mobile QA, "
                "screenshots and errors; no arbitrary browser controls. "
                "Requires project relative path with index.html."
            ),
            risk=RiskLevel.MEDIUM,
            required_permissions={Capability.BROWSER_LOCAL},
            args_model=BrowserArgs,
            handler=browser.capture,
        )

    registry.register(
        name="filesystem.read",
        description=(
            "Read a bounded UTF-8 file page. Optional offset (byte offset, default 0), "
            "max_bytes (4..24000, default 12000). Follow next_offset until complete=true; "
            "restart at zero if version changes. Paths are workspace relative."
        ),
        risk=RiskLevel.LOW,
        required_permissions={Capability.FILESYSTEM_READ},
        args_model=ReadArgs,
        handler=filesystem.read,
    )
    registry.register(
        name="filesystem.list",
        description="List the entries of a directory inside the workspace.",
        risk=RiskLevel.LOW,
        required_permissions={Capability.FILESYSTEM_READ},
        args_model=ListArgs,
        handler=filesystem.list_dir,
    )
    registry.register(
        name="filesystem.write",
        description="Create or overwrite a text file inside the workspace.",
        risk=RiskLevel.MEDIUM,
        required_permissions={Capability.FILESYSTEM_WRITE},
        args_model=WriteArgs,
        handler=filesystem.write,
    )
    registry.register(
        name="tests.run",
        description=(
            "Run a predefined project command by name. "
            f"Available suites: {', '.join(tests.suite_names)}."
        ),
        risk=RiskLevel.MEDIUM,
        required_permissions={Capability.TESTS_RUN},
        args_model=RunTestsArgs,
        handler=tests.run,
    )
    registry.register(
        name="github.read",
        description="Read public metadata for a GitHub repository. Output is untrusted.",
        risk=RiskLevel.LOW,
        required_permissions={Capability.GITHUB_READ},
        args_model=ReadRepoArgs,
        handler=github.read_repo,
    )

    if include_privileged:
        registry.register(
            name="system.request_privileged_action",
            description=(
                "Ask a human operator to perform a privileged system action. "
                "This never executes anything; it creates a pending request."
            ),
            risk=RiskLevel.PRIVILEGED,
            privileged=True,
            required_permissions={Capability.PRIVILEGED_REQUEST},
            args_model=RequestPrivilegedActionArgs,
            handler=refuse,
        )

    return registry


def register_project_tools(registry: ToolRegistry, projects: ProjectToolchain) -> None:
    for operation in ("inspect", "dependencies", "build", "serve", "stop", "test"):
        handler = (
            projects.inspect
            if operation == "inspect"
            else projects.dependencies
            if operation == "dependencies"
            else partial(projects.operation, operation)
        )
        registry.register(
            name="project." + operation,
            description=(
                f"Controlled Flask/Vite {operation}; project.json only, no commands. "
                "The strict project.json keys are schema_version, framework, frontend, python, "
                "npm, routes; package.json-style fields such as name/scripts/dependencies are "
                "invalid. Dependencies require operator approval."
            ),
            risk=RiskLevel.MEDIUM,
            required_permissions={
                Capability.PROJECT_DEPENDENCIES
                if operation == "dependencies"
                else Capability.PROJECT_EXECUTE
            },
            args_model=ProjectArgs,
            handler=handler,
        )
