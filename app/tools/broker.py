"""The single choke point for tool execution.

    Agent → ToolBroker → PolicyEngine → Tool

Agents hold a reference to a broker, never to a handler. There is no other path
from an agent to a capability, which is what makes the policy engine
unavoidable rather than merely advisory.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.errors import ToolError, ToolNotFoundError
from app.observability.events import EventBus, EventType
from app.policy.engine import Effect, PolicyEngine
from app.policy.permissions import Principal
from app.privileged_bridge import PrivilegedGateway
from app.tasks.evidence import file_key
from app.tools.registry import ToolRegistry

CURRENT_TOOL_TASK: ContextVar[str | None] = ContextVar("current_tool_task", default=None)


class InvocationStatus(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    FAILED = "failed"


class ToolInvocation(BaseModel):
    """What the agent asked for. Arguments are untrusted until validated."""

    tool: str
    arguments: dict[str, Any] = {}


class ToolResult(BaseModel):
    """What the agent gets back. Never a callable, never a raw exception."""

    status: InvocationStatus
    tool: str
    output: dict[str, Any] | None = None
    reason: str | None = None
    rule_id: str | None = None
    #: Present only when status is APPROVAL_REQUIRED.
    request_id: str | None = None
    images: list[str] = Field(default_factory=list, exclude=True)

    def as_observation(self) -> str:
        """A compact, model-facing rendering of the outcome."""
        if self.status is InvocationStatus.COMPLETED:
            return f"{self.tool} -> {json.dumps(self.output, ensure_ascii=False)}"
        if self.status is InvocationStatus.APPROVAL_REQUIRED:
            return (
                f"{self.tool} -> approval_required (request {self.request_id}). "
                "A human operator must approve this; you cannot proceed with it."
            )
        return f"{self.tool} -> {self.status.value}: {self.reason}"


class ToolBroker:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        events: EventBus,
        privileged_gateway: PrivilegedGateway | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._events = events
        self._privileged_gateway = privileged_gateway
        self.cancelled_tasks: set[str] = set()
        self.project_scopes: dict[str, str] = {}

    def check_cancelled(self, task_id: str | None) -> None:
        if task_id in self.cancelled_tasks:
            raise asyncio.CancelledError

    async def invoke(self, principal: Principal, invocation: ToolInvocation) -> ToolResult:
        task_id = principal.task_id
        self.check_cancelled(task_id)
        scope = self.project_scopes.get(task_id or "")
        if scope and invocation.tool == "tests.run":
            return await self._deny(
                principal,
                invocation.tool,
                "generated projects must use isolated project.test",
                rule_id="project-execution-boundary",
            )
        if scope and invocation.tool.startswith(("filesystem.", "project.", "browser.")):
            path = invocation.arguments.get("path", invocation.arguments.get("project", "."))
            if isinstance(path, str):
                normalized = file_key(path)
                if normalized != scope and not normalized.startswith(scope + "/"):
                    return await self._deny(
                        principal, invocation.tool, "outside task project", rule_id="project-scope"
                    )
                if invocation.tool == "filesystem.write" and any(
                    p in {".venv", "node_modules"} for p in normalized.split("/")
                ):
                    return await self._deny(
                        principal,
                        invocation.tool,
                        "environment is runtime-owned",
                        rule_id="project-environment",
                    )
        await self._events.emit(
            EventType.TOOL_REQUESTED,
            task_id=task_id,
            actor=principal.name,
            tool=invocation.tool,
            role=principal.role.value,
            model_kind=principal.model_kind.value,
            arguments={
                k: v
                for k, v in invocation.arguments.items()
                if k in {"path", "offset", "max_bytes", "suite", "owner", "repo"}
            },
            argument_names=sorted(invocation.arguments),
        )

        try:
            spec = self._registry.get(invocation.tool)
        except ToolNotFoundError as exc:
            return await self._deny(principal, invocation.tool, str(exc), rule_id="R0-unknown-tool")

        decision = self._policy.evaluate(principal, spec.facts())

        if decision.effect is Effect.DENY:
            return await self._deny(principal, spec.name, decision.reason, rule_id=decision.rule_id)

        # Belt and braces: the engine must never allow a privileged tool. If this
        # ever fires, the policy table has been edited into an unsafe state and
        # we fail closed rather than execute.
        if spec.privileged and decision.effect is Effect.ALLOW:
            return await self._deny(
                principal,
                spec.name,
                "internal invariant violated: privileged tool was allowed directly",
                rule_id="R3-invariant",
            )

        try:
            arguments = spec.args_model.model_validate(invocation.arguments)
        except ValidationError as exc:
            reason = f"invalid arguments for {spec.name}: {exc.error_count()} problem(s)"
            await self._events.emit(
                EventType.TOOL_FAILED,
                task_id=task_id,
                actor=principal.name,
                tool=spec.name,
                reason=reason,
            )
            return ToolResult(status=InvocationStatus.FAILED, tool=spec.name, reason=reason)

        if decision.effect is Effect.REQUIRE_APPROVAL:
            self.check_cancelled(task_id)
            return await self._request_approval(principal, spec.name, arguments)

        await self._events.emit(
            EventType.TOOL_ALLOWED,
            task_id=task_id,
            actor=principal.name,
            tool=spec.name,
            rule_id=decision.rule_id,
            reason=decision.reason,
        )

        try:
            self.check_cancelled(task_id)
            context_token = CURRENT_TOOL_TASK.set(task_id)
            try:
                output = await spec.handler(arguments)
            finally:
                CURRENT_TOOL_TASK.reset(context_token)
        except ToolError as exc:
            await self._events.emit(
                EventType.TOOL_FAILED,
                task_id=task_id,
                actor=principal.name,
                tool=spec.name,
                reason=str(exc),
            )
            return ToolResult(status=InvocationStatus.FAILED, tool=spec.name, reason=str(exc))

        if spec.name == "project.dependencies" and output.get("status") == "approval_required":
            await self._events.emit(
                EventType.PRIVILEGED_ACTION_REQUESTED,
                task_id=task_id,
                actor=principal.name,
                tool=spec.name,
                request_id=output["request_id"],
                project=output["project"],
            )
            return ToolResult(
                status=InvocationStatus.APPROVAL_REQUIRED,
                tool=spec.name,
                request_id=output["request_id"],
                output=output,
            )

        if (
            spec.name
            in {
                "web.search",
                "web.request",
                "web.fetch",
                "docs.fetch",
                "assets.search_images",
                "assets.import_image",
            }
            and output.get("status") == "denied"
        ):
            return await self._deny(
                principal,
                spec.name,
                str(output.get("reason") or output.get("error") or "egress denied"),
                rule_id="web-egress",
            )

        if spec.name.startswith(
            ("web.", "docs.", "assets.", "browser.", "project.")
        ) and output.get("status") in {
            "not_configured",
            "failed",
        }:
            reason = str(
                output.get("reason")
                or output.get("error")
                or output.get("output")
                or "tool operation failed"
            )[-8000:]
            if output.get("error_code") == "browser_unavailable":
                reason = "browser_unavailable: " + reason
            await self._events.emit(
                EventType.TOOL_FAILED,
                task_id=task_id,
                actor=principal.name,
                tool=spec.name,
                reason=reason,
            )
            return ToolResult(
                status=InvocationStatus.FAILED, tool=spec.name, output=output, reason=reason
            )

        images = output.pop("_images", []) if spec.name.startswith("browser.") else []
        await self._events.emit(
            EventType.TOOL_COMPLETED,
            task_id=task_id,
            actor=principal.name,
            tool=spec.name,
            result={
                k: v
                for k, v in output.items()
                if k
                in {
                    "path",
                    "bytes",
                    "bytes_written",
                    "changed",
                    "truncated",
                    "suite",
                    "exit_code",
                    "passed",
                    "complete",
                    "offset",
                    "bytes_returned",
                    "total_bytes",
                    "next_offset",
                    "version",
                    "project",
                    "screenshots_generated",
                    "screenshots",
                    "report_path",
                    "previews",
                    "routes",
                    "status",
                    "environment",
                    "output",
                    "url",
                }
            },
        )
        return ToolResult(
            status=InvocationStatus.COMPLETED,
            images=images,
            tool=spec.name,
            output=output,
            rule_id=decision.rule_id,
        )

    async def _deny(
        self, principal: Principal, tool: str, reason: str, *, rule_id: str
    ) -> ToolResult:
        await self._events.emit(
            EventType.TOOL_DENIED,
            task_id=principal.task_id,
            actor=principal.name,
            tool=tool,
            reason=reason,
            rule_id=rule_id,
        )
        return ToolResult(status=InvocationStatus.DENIED, tool=tool, reason=reason, rule_id=rule_id)

    async def _request_approval(
        self, principal: Principal, tool: str, arguments: BaseModel
    ) -> ToolResult:
        if self._privileged_gateway is None:
            return await self._deny(
                principal,
                tool,
                "privileged requests are not enabled in this runtime",
                rule_id="R3-no-gateway",
            )

        payload = arguments.model_dump()
        request_text = str(payload.get("request", ""))
        if any(word in request_text.lower() for word in ("playwright", "chromium")):
            return ToolResult(
                status=InvocationStatus.FAILED,
                tool=tool,
                reason="browser_unavailable: browser dependency setup is operator-owned; "
                "run python -m playwright install chromium outside the agent workflow",
            )
        ticket = await self._privileged_gateway.submit(
            requested_by=principal.name,
            request_text=request_text,
            task_id=principal.task_id,
            context={"tool": tool, "role": principal.role.value},
        )
        await self._events.emit(
            EventType.PRIVILEGED_ACTION_REQUESTED,
            task_id=principal.task_id,
            actor=principal.name,
            tool=tool,
            request_id=ticket.request_id,
            status=ticket.status,
            parsed_action=ticket.action,
        )
        if ticket.status != "awaiting_approval":
            return ToolResult(
                status=InvocationStatus.FAILED,
                tool=tool,
                request_id=ticket.request_id,
                reason=f"approval_{ticket.status}",
            )
        return ToolResult(
            status=InvocationStatus.APPROVAL_REQUIRED,
            tool=tool,
            request_id=ticket.request_id,
            reason="approval_required",
            rule_id="R3-privileged-requires-human",
        )
