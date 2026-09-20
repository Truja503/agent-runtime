"""Coordinates workers and consolidates their results.

The supervisor is the most trusted agent in the runtime and still holds no
permissions at all: its role maps to an empty capability set. Coordination does
not require authority, and giving it any would make it the obvious target for
an injected instruction.

Its model chooses *which workers to involve* — a choice constrained to a closed
set of registered names. A model that answers "root" or "shell" selects nothing.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

from app.agents.base import SECURITY_PREAMBLE, AgentResult, AgentStatus, BaseAgent
from app.errors import ModelError
from app.models.base import Message, ModelProvider, Role, closed_schema, extract_json_object
from app.models.profiles import ModelProfile
from app.observability.events import EventBus, EventType
from app.observability.prompts import PromptInspection
from app.policy.permissions import AgentRole
from app.tools.broker import ToolBroker


class SupervisorPlan(BaseModel):
    workers: list[str] = Field(default_factory=list)
    plan: str = ""


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
        self.on_worker_start: Callable[[str], Awaitable[None]] | None = None

    async def run(
        self, *, task_id: str, goal: str, context: str = ""
    ) -> AgentResult:  # pragma: no cover - the API path uses run_task
        outcome = await self.run_task(task_id=task_id, goal=goal)
        return AgentResult(
            agent=self.name,
            role=self.role.value,
            status=AgentStatus.COMPLETED,
            summary=str(outcome.get("summary", "")),
        )

    async def run_task(self, *, task_id: str, goal: str) -> dict[str, Any]:
        await self.events.emit(
            EventType.AGENT_STARTED, task_id=task_id, actor=self.name, goal_chars=len(goal)
        )

        plan = await self._plan(task_id=task_id, goal=goal)
        selected = list(dict.fromkeys(name for name in plan.workers if name in self._workers))
        if not selected:
            # A model that returns nothing usable must not stall the runtime.
            selected = [name for name in ("researcher", "reviewer") if name in self._workers]

        results: list[AgentResult] = []
        pending: list[str] = []
        for worker_name in selected:
            if self.on_worker_start:
                await self.on_worker_start(worker_name)
            worker = self._workers[worker_name]
            result = await worker.run(task_id=task_id, goal=goal, context=plan.plan)
            results.append(result)
            pending.extend(result.pending_approvals)

        summary = self._consolidate(goal, plan, results, pending)
        await self.events.emit(
            EventType.AGENT_COMPLETED,
            task_id=task_id,
            actor=self.name,
            workers=selected,
            pending_approvals=pending,
        )
        return {
            "summary": summary,
            "plan": plan.plan,
            "workers": selected,
            "pending_approvals": pending,
            "worker_results": [result.model_dump(mode="json") for result in results],
        }

    def response_schema(self) -> dict[str, Any]:
        schema = SupervisorPlan.model_json_schema()
        schema["properties"]["workers"]["items"] = {"type": "string", "enum": list(self._workers)}
        return closed_schema(schema)

    def system_prompt(self) -> str:
        roster = ", ".join(sorted(self._workers)) or "(none)"
        return (
            f"You are the supervisor of a small agent team. {self.mandate}\n\n"
            f"{SECURITY_PREAMBLE}\n\n"
            f"Available workers: {roster}. You may only name workers from that list.\n"
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
