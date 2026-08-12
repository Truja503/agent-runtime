"""MCP adapter.

    Agent → ToolBroker → PolicyEngine → MCP Adapter → MCP Server

MCP is a way to *add* capabilities, never a way to bypass the policy engine. An
MCP tool becomes usable only when a human writes an :class:`MCPBinding` for it,
declaring its risk level, its required permissions, and the argument schema the
runtime will validate against. The server's own advertised schema is treated as
a hint, not as authorisation: a server that renames itself ``filesystem.read``
or claims to need no permissions gains nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ToolExecutionError
from app.policy.permissions import Capability, RiskLevel
from app.tools.registry import ToolRegistry, ToolSpec


class MCPToolDescriptor(BaseModel):
    """What a server says it offers. Untrusted."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPClient(Protocol):
    async def list_tools(self) -> list[MCPToolDescriptor]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MCPBinding(BaseModel):
    """A human decision to expose one remote tool, under stated terms."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    remote_name: str
    #: Name inside this runtime. Namespaced so a remote tool can never shadow
    #: a built-in capability.
    local_name: str
    description: str
    risk: RiskLevel
    required_permissions: frozenset[Capability]
    args_model: type[BaseModel]

    def namespaced(self) -> str:
        return self.local_name if self.local_name.startswith("mcp.") else f"mcp.{self.local_name}"


class StaticMCPClient:
    """An in-process MCP client backed by local callables.

    A real deployment swaps this for a stdio or HTTP client speaking the MCP
    protocol; everything downstream — binding, policy, broker — is unchanged.
    """

    def __init__(
        self,
        tools: dict[
            str,
            tuple[MCPToolDescriptor, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
        ],
    ) -> None:
        self._tools = tools

    async def list_tools(self) -> list[MCPToolDescriptor]:
        return [descriptor for descriptor, _ in self._tools.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolExecutionError(f"MCP server does not expose {name!r}")
        _, handler = entry
        return await handler(arguments)


def register_mcp_tools(
    registry: ToolRegistry,
    client: MCPClient,
    bindings: list[MCPBinding],
) -> list[ToolSpec]:
    """Register each explicitly bound MCP tool as an ordinary runtime tool.

    Because the result is an ordinary :class:`ToolSpec`, MCP calls travel the
    same broker → policy → execute path as everything else.
    """
    specs: list[ToolSpec] = []
    for binding in bindings:
        specs.append(
            registry.register(
                name=binding.namespaced(),
                description=f"[mcp] {binding.description}",
                risk=binding.risk,
                required_permissions=binding.required_permissions,
                args_model=binding.args_model,
                handler=_make_handler(client, binding),
            )
        )
    return specs


def _make_handler(
    client: MCPClient, binding: MCPBinding
) -> Callable[[BaseModel], Awaitable[dict[str, Any]]]:
    async def handler(args: BaseModel) -> dict[str, Any]:
        result = await client.call_tool(binding.remote_name, args.model_dump())
        # Server output is untrusted content, flagged as such for the agent.
        return {"result": result, "source": "mcp", "untrusted": True}

    return handler
