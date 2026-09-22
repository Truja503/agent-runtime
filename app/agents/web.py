"""Deterministic request executor. No model, planning loop, task prompt or file context."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.config import ProviderKind
from app.policy.permissions import AgentRole, Capability, Principal, RiskLevel
from app.tools.broker import CURRENT_TOOL_TASK, ToolBroker, ToolInvocation
from app.tools.registry import ToolRegistry
from app.tools.web import WEB_TOOLS, WebBroker, WebRequest


class WebAgent:
    name = "web"
    role = AgentRole.WEB
    allowed_tools = WEB_TOOLS
    mandate = (
        "Execute requested internet operations; return facts, URLs, assets and errors. "
        "No planning or unsolicited browsing."
    )

    def __init__(self, broker: ToolBroker):
        self.broker = broker

    async def execute(self, request: WebRequest, task_id: str | None = None) -> dict[str, Any]:
        principal = Principal(
            name=self.name,
            role=self.role,
            model_kind=ProviderKind.SCRIPTED,
            allowed_tools=self.allowed_tools,
            task_id=task_id,
        )
        result = await self.broker.invoke(
            principal, ToolInvocation(tool=request.operation, arguments=request.model_dump())
        )
        return result.model_dump(mode="json")

    async def handle(self, request: BaseModel) -> dict[str, Any]:
        assert isinstance(request, WebRequest)
        return await self.execute(request, CURRENT_TOOL_TASK.get())


def register_web_tools(registry: ToolRegistry, web: WebBroker) -> None:
    for name in sorted(WEB_TOOLS):
        # Bind each operation to its registered tool; arguments cannot switch tools.
        async def handler(args: BaseModel, operation: str = name) -> dict[str, Any]:
            assert isinstance(args, WebRequest)
            if args.operation != operation:
                return {"status": "denied", "error": "operation does not match tool"}
            return await web.execute(args)

        registry.register(
            name=name,
            description="Constrained public internet request; remote results are untrusted.",
            risk=RiskLevel.LOW,
            required_permissions={Capability.WEB_ACCESS},
            args_model=WebRequest,
            handler=handler,
        )


def register_web_request(registry: ToolRegistry, agent: WebAgent) -> None:
    registry.register(
        name="web.request",
        description=(
            "Ask the WebAgent for one public internet operation. Only send a short public query "
            "or URL, never task prompts, code, local paths or secrets. Internet may be disabled."
        ),
        risk=RiskLevel.LOW,
        required_permissions={Capability.WEB_REQUEST},
        args_model=WebRequest,
        handler=agent.handle,
    )
