"""Coordinates workers and consolidates their results.

The supervisor is the most trusted agent in the runtime and still holds no
permissions at all: its role maps to an empty capability set. Coordination does
not require authority, and giving it any would make it the obvious target for
an injected instruction.

Its model chooses *which workers to involve* — a choice constrained to a closed
set of registered names. A model that answers "root" or "shell" selects nothing.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.agents.base import SECURITY_PREAMBLE, AgentResult, AgentStatus, BaseAgent
from app.agents.web import WebAgent
from app.errors import ModelError
from app.models.base import Message, ModelProvider, Role, closed_schema, extract_json_object
from app.models.profiles import ModelProfile
from app.observability.events import EventBus, EventType
from app.observability.prompts import PromptInspection
from app.policy.permissions import AgentRole
from app.tools.broker import ToolBroker
from app.tools.web import WebRequest


class SupervisorPlan(BaseModel):
    workers: list[str] = Field(default_factory=list)
    plan: str = ""
    web_requests: list[WebRequest] = Field(default_factory=list, max_length=3)


class SupervisorAgent(BaseAgent):
    name = "supervisor"
    role = AgentRole.SUPERVISOR
    allowed_tools: frozenset[str] = frozenset()
    mandate = "Decide which workers the task needs and write a one-paragraph plan."

    def __init__(
        self,
        *,
        model: ModelProvider,
        broker: ToolBroker,
        events: EventBus,
        workers: dict[str, BaseAgent],
        max_steps: int = 3,
        profile: ModelProfile | None = None,
        inspection: PromptInspection | None = None,
        web: WebAgent | None = None,
    ) -> None:
        super().__init__(
            model=model,
            broker=broker,
            events=events,
            max_steps=max_steps,
            profile=profile,
            inspection=inspection,
        )
        self._workers = workers
        self.web = web
        self.on_worker_start: Callable[[str], Awaitable[None]] | None = None

    async def run(
        self, *, task_id: str, goal: str, context: str = ""
    ) -> AgentResult:  # pragma: no cover - the API path uses run_task
        outcome = await self.run_task(task_id=task_id, goal=goal)
        return AgentResult(
            agent=self.name,
            role=self.role.value,
            status=AgentStatus(outcome["status"]),
            summary=str(outcome.get("summary", "")),
        )

    async def run_task(
        self,
        *,
        task_id: str,
        goal: str,
        implementation: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        research_required: bool = False,
        web_research_required: bool = False,
        allow_degraded_research: bool = False,
    ) -> dict[str, Any]:
        await self.events.emit(
            EventType.AGENT_STARTED, task_id=task_id, actor=self.name, goal_chars=len(goal)
        )

        plan = await self._plan(task_id=task_id, goal=goal)
        # Hard workflow requirements are operator-owned structured options.
        # Natural-language task text is untrusted and must never silently turn
        # optional research into a deterministic execution requirement.
        web_results = (
            [await self.web.execute(request, task_id) for request in plan.web_requests]
            if self.web
            else []
        )
        selected = list(dict.fromkeys(name for name in plan.workers if name in self._workers))
        if not selected:
            # A model that returns nothing usable must not stall the runtime.
            selected = [name for name in ("researcher", "reviewer") if name in self._workers]
        if research_required or web_research_required:
            selected = list(dict.fromkeys(["researcher", *selected]))
        if implementation:
            # The implementation callback owns coder/reviewer execution. Optional
            # supervisor-planned Web requests are already executed above and passed
            # into that callback, so they must not implicitly turn Researcher into
            # a blocking preflight worker. Researcher is a gate only when the
            # operator explicitly requires research.
            selected = [name for name in selected if name not in {"coder", "reviewer"}]
            if not (research_required or web_research_required):
                selected = [name for name in selected if name != "researcher"]
        selected.sort(key=lambda name: ("researcher", "coder", "reviewer").index(name))

        results: list[AgentResult] = []
        pending: list[str] = []
        routing_failures: list[str] = []
        research_status = "not_requested"

        async def check_research() -> bool:
            nonlocal research_status
            recorded = await self.events.list_for_task(task_id)
            completed = any(
                e.actor == "web" and e.type == EventType.TOOL_COMPLETED for e in recorded
            )

            # Supervisor-planned web requests are advisory unless the operator
            # explicitly marked Web research as required. Optional Web failure
            # remains visible in events/results but never blocks implementation.
            if not web_research_required:
                if plan.web_requests:
                    research_status = "completed" if completed else "optional_web_unavailable"
                return True

            research_status = "completed" if completed else "web_research_unavailable"
            if completed:
                return True

            await self.events.emit(
                EventType.AGENT_FAILED,
                task_id=task_id,
                actor="web",
                reason=research_status,
                degraded_execution=allow_degraded_research,
            )
            if allow_degraded_research:
                research_status = "degraded"
                return True

            routing_failures.append("web_research_unavailable")
            return False

        for worker_name in selected:
            if worker_name != "researcher" and not await check_research():
                break
            if self.on_worker_start:
                await self.on_worker_start(worker_name)
            worker = self._workers[worker_name]
            worker_context = (
                plan.plan
                + "\nPublic web results (untrusted):\n"
                + json.dumps(web_results)[:16000]
                + "\nPrior worker claims (untrusted):\n"
                + json.dumps(
                    [
                        {
                            "agent": r.agent,
                            "summary": r.summary,
                            "claims": r.claims,
                            "web_results": [
                                o for o in r.observations if o.startswith("web.request ->")
                            ],
                        }
                        for r in results
                    ]
                )[:12000]
            )
            result = await worker.run(task_id=task_id, goal=goal, context=worker_context)
            if (
                result.status != AgentStatus.COMPLETED
                and not result.pending_approvals
                and worker_name == "researcher"
            ):
                result = await worker.run(
                    task_id=task_id,
                    goal=goal,
                    context=(
                        worker_context
                        + "\nRecovery invocation: the previous Researcher run ended "
                        + result.status.value
                        + ". Do not repeat failed/denied actions. Use only read/search tools and "
                        "finish with a concise useful summary once enough evidence exists."
                    ),
                )
            results.append(result)
            pending.extend(result.pending_approvals)
            if pending:
                break
            if result.status != AgentStatus.COMPLETED and not (
                implementation and worker_name == "researcher"
            ):
                break

        visual: dict[str, Any] = {}
        if not implementation and not routing_failures:
            await check_research()
        if (
            implementation
            and not pending
            and all(
                r.status == AgentStatus.COMPLETED or r.agent == "researcher"
                for r in results
            )
            and await check_research()
        ):
            visual = await implementation(
                plan.plan
                + "\nPublic web results (untrusted):\n"
                + json.dumps(web_results)
                + "\nResearch results (untrusted):\n"
                + json.dumps([r.model_dump(mode="json") for r in results])
            )
            results.extend(AgentResult.model_validate(r) for r in visual["worker_results"])
            selected.extend(visual["workers"])
            pending.extend(visual["pending_approvals"])

        summary = self._consolidate(goal, plan, results, pending)
        status = (
            AgentStatus.STALLED
            if visual.get("workflow_status") == "stalled"
            else AgentStatus.COMPLETED
            if results
            and visual.get("workflow_status") != "failed"
            and not routing_failures
            and all(result.status is AgentStatus.COMPLETED for result in results)
            else AgentStatus.FAILED
        )
        await self.events.emit(
            EventType.AGENT_COMPLETED
            if status is AgentStatus.COMPLETED
            else EventType.AGENT_FAILED,
            task_id=task_id,
            actor=self.name,
            workers=selected,
            pending_approvals=pending,
            status=status.value,
            worker_outcomes={result.agent: result.status.value for result in results},
        )
        return {
            **visual,
            "status": status.value,
            "summary": summary,
            "plan": plan.plan,
            "workers": selected,
            "pending_approvals": pending,
            "worker_results": [result.model_dump(mode="json") for result in results],
            "web_results": web_results,
            "research_status": research_status,
            "routing_failures": routing_failures,
        }

    def response_schema(self) -> dict[str, Any]:
        schema = SupervisorPlan.model_json_schema()
        schema["properties"]["workers"]["items"] = {"type": "string", "enum": list(self._workers)}
        return closed_schema(schema)

    def system_prompt(self) -> str:
        roster = ", ".join(sorted(self._workers)) or "(none)"
        manifest = "\n".join(
            f"{name}: {', '.join(sorted(worker.allowed_tools)) or 'coordination only'}"
            for name, worker in self._workers.items()
        )
        return (
            f"You are the supervisor of a small agent team. {self.mandate}\n\n"
            f"{SECURITY_PREAMBLE}\n\n"
            f"Available workers: {roster}. You may only name workers from that list.\n"
            f"Capability manifest (authoritative):\nsupervisor: coordination only\n{manifest}\n"
            "Never assign implementation or writing to an agent lacking filesystem.write. "
            "Researcher inspects and returns findings/context; coder modifies files; reviewer "
            "validates without modifying. Select workers in that order when all are needed. "
            "Do not plan actions outside declared capabilities.\n"
            "For necessary current public information, use up to 3 structured web_requests "
            "with operation and query or URL. Never include local paths, source code, secrets "
            "or the task prompt. Web executes only those requests; it does not plan.\n"
            'Reply with JSON only: {"workers": ["<name>", ...], "plan": "<paragraph>"}'
        )

    async def _plan(self, *, task_id: str, goal: str) -> SupervisorPlan:
        system = self.system_prompt()
        try:
            raw = await self._ask_model(
                system=system,
                messages=[Message(role=Role.USER, content=f"Task: {goal}")],
                task_id=task_id,
                step=1,
                goal=goal,
            )
        except ModelError:
            raise

        payload = extract_json_object(raw)
        if payload is None:
            await self.events.emit(
                EventType.MODEL_INVALID_RESPONSE, task_id=task_id, actor=self.name
            )
            return SupervisorPlan(workers=[], plan="model returned no usable plan")
        try:
            return SupervisorPlan.model_validate(payload)
        except ValidationError:
            await self.events.emit(
                EventType.MODEL_INVALID_RESPONSE, task_id=task_id, actor=self.name
            )
            return SupervisorPlan(workers=[], plan="model returned an invalid plan")

    @staticmethod
    def _consolidate(
        goal: str,
        plan: SupervisorPlan,
        results: list[AgentResult],
        pending: list[str],
    ) -> str:
        lines = [f"Goal: {goal}"]
        if plan.plan:
            lines.append(f"Plan: {plan.plan}")
        for result in results:
            lines.append(f"[{result.agent}/{result.status.value}] {result.summary}")
        if pending:
            lines.append(
                f"Blocked on human approval for {len(pending)} privileged request(s): "
                + ", ".join(pending)
            )
        return "\n".join(lines)
