"""Registry discipline, the test runner, GitHub reads, and the MCP adapter."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.errors import ToolExecutionError, ToolNotFoundError
from app.observability.events import EventBus
from app.observability.store import InMemoryEventStore
from app.policy.engine import PolicyEngine
from app.policy.permissions import AgentRole, Capability, RiskLevel
from app.tools.broker import InvocationStatus, ToolBroker, ToolInvocation
from app.tools.github import GitHubTools, ReadRepoArgs
from app.tools.mcp import (
    MCPBinding,
    MCPToolDescriptor,
    StaticMCPClient,
    register_mcp_tools,
)
from app.tools.registry import ToolRegistry
from app.tools.testing import ProjectTestTools, RunTestsArgs
from tests.conftest import RecordingCommandRunner, principal_for


class EmptyArgs(BaseModel):
    pass


async def noop(_: BaseModel) -> dict[str, Any]:
    return {}


# --- registry -------------------------------------------------------------


def test_registry_requires_explicit_metadata() -> None:
    registry = ToolRegistry()
    spec = registry.register(
        name="demo.tool",
        description="A demo.",
        risk=RiskLevel.LOW,
        required_permissions={Capability.FILESYSTEM_READ},
        args_model=EmptyArgs,
        handler=noop,
    )
    assert spec.facts().required_permissions == frozenset({Capability.FILESYSTEM_READ})
    assert registry.has("demo.tool")
    assert registry.get("demo.tool") is spec


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry()
    registry.register(
        name="demo.tool",
        description="A demo.",
        risk=RiskLevel.LOW,
        args_model=EmptyArgs,
        handler=noop,
    )
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            name="demo.tool",
            description="Another.",
            risk=RiskLevel.LOW,
            args_model=EmptyArgs,
            handler=noop,
        )


def test_privileged_tools_must_declare_privileged_risk() -> None:
    """A mislabelled privileged tool fails at registration, not at runtime."""
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="marked privileged"):
        registry.register(
            name="system.sneaky",
            description="Pretends to be low risk.",
            risk=RiskLevel.LOW,
            privileged=True,
            args_model=EmptyArgs,
            handler=noop,
        )


def test_unknown_tool_lookup_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().get("nope")


def test_no_shell_capability_is_registered(registry: ToolRegistry) -> None:
    names = registry.names()
    for forbidden in ("shell.exec", "system.run", "bash", "run_any_command"):
        assert forbidden not in names


# --- tests.run ------------------------------------------------------------


async def test_tests_run_uses_a_predefined_command(tmp_path: Any) -> None:
    runner = RecordingCommandRunner(stdout="2 passed")
    tools = ProjectTestTools(
        workspace_root=tmp_path,
        runner=runner,
        suites={"default": ["/usr/bin/true"], "lint": ["/usr/bin/false"]},
    )
    result = await tools.run(RunTestsArgs(suite="default"))
    assert result["passed"] is True
    assert runner.calls == [["/usr/bin/true"]]


async def test_tests_run_rejects_unknown_suites(tmp_path: Any) -> None:
    tools = ProjectTestTools(
        workspace_root=tmp_path,
        runner=RecordingCommandRunner(),
        suites={"default": ["/usr/bin/true"]},
    )
    with pytest.raises(ToolExecutionError, match="unknown suite"):
        await tools.run(RunTestsArgs(suite="; rm -rf /"))


async def test_tests_run_takes_no_command_from_the_caller(tmp_path: Any) -> None:
    """The args model has exactly one field, and it is a suite name."""
    assert set(RunTestsArgs.model_fields) == {"suite"}


# --- github.read ----------------------------------------------------------


async def test_github_read_projects_a_fixed_subset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/anthropics/anthropic-sdk-python"
        return httpx.Response(
            200,
            json={
                "full_name": "anthropics/anthropic-sdk-python",
                "description": "SDK",
                "default_branch": "main",
                "stargazers_count": 100,
                "language": "Python",
                "archived": False,
                "owner": {"email": "should-not-be-returned@example.com"},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    tools = GitHubTools(client)
    result = await tools.read_repo(
        ReadRepoArgs(owner="anthropics", repo="anthropic-sdk-python")
    )
    assert result["full_name"] == "anthropics/anthropic-sdk-python"
    assert result["untrusted"] is True
    assert "owner" not in result
    await tools.aclose()


async def test_github_read_validates_names() -> None:
    tools = GitHubTools(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    )
    with pytest.raises(ToolExecutionError, match="invalid owner"):
        await tools.read_repo(ReadRepoArgs(owner="../../etc", repo="passwd"))
    await tools.aclose()


# --- MCP ------------------------------------------------------------------


class SearchArgs(BaseModel):
    query: str


async def test_mcp_tools_are_registered_only_via_explicit_bindings() -> None:
    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"hits": [arguments["query"]]}

    client = StaticMCPClient(
        {
            "search": (MCPToolDescriptor(name="search", description="Search docs."), search),
            # The server also advertises this one. Without a binding it simply
            # does not exist inside the runtime.
            "delete_everything": (
                MCPToolDescriptor(name="delete_everything", description="Danger."),
                search,
            ),
        }
    )
    registry = ToolRegistry()
    register_mcp_tools(
        registry,
        client,
        [
            MCPBinding(
                remote_name="search",
                local_name="docs.search",
                description="Search the docs.",
                risk=RiskLevel.LOW,
                required_permissions=frozenset({Capability.GITHUB_READ}),
                args_model=SearchArgs,
            )
        ],
    )

    assert registry.names() == ["mcp.docs.search"]
    assert not registry.has("delete_everything")
    assert not registry.has("mcp.delete_everything")


async def test_mcp_calls_still_go_through_the_policy_engine() -> None:
    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"hits": [arguments["query"]]}

    client = StaticMCPClient(
        {"search": (MCPToolDescriptor(name="search", description="Search."), search)}
    )
    registry = ToolRegistry()
    register_mcp_tools(
        registry,
        client,
        [
            MCPBinding(
                remote_name="search",
                local_name="docs.search",
                description="Search the docs.",
                risk=RiskLevel.LOW,
                required_permissions=frozenset({Capability.GITHUB_READ}),
                args_model=SearchArgs,
            )
        ],
    )
    broker = ToolBroker(
        registry=registry, policy=PolicyEngine(), events=EventBus([InMemoryEventStore()])
    )

    # The researcher holds github.read, so this is allowed.
    allowed = await broker.invoke(
        principal_for(AgentRole.RESEARCHER, tools={"mcp.docs.search"}),
        ToolInvocation(tool="mcp.docs.search", arguments={"query": "policy"}),
    )
    assert allowed.status is InvocationStatus.COMPLETED
    assert allowed.output is not None
    assert allowed.output["untrusted"] is True

    # The coder does not, so the same MCP tool is denied. MCP is not a bypass.
    denied = await broker.invoke(
        principal_for(AgentRole.CODER, tools={"mcp.docs.search"}),
        ToolInvocation(tool="mcp.docs.search", arguments={"query": "policy"}),
    )
    assert denied.status is InvocationStatus.DENIED
    assert denied.rule_id == "R2-missing-permission"
