"""Proposes a bounded project plan. The runtime alone executes and advances it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.agents.base import SECURITY_PREAMBLE, AgentResult, AgentStatus, BaseAgent
from app.agents.web import WebAgent
from app.errors import ModelError
from app.models.base import Message, ModelProvider, Role, closed_schema, extract_json_object
from app.models.profiles import ModelProfile
from app.observability.events import EventBus, EventType
from app.observability.prompts import PromptInspection
from app.policy.permissions import AgentRole
from app.tasks.phases import ProjectPlan
from app.tools.broker import ToolBroker


class SupervisorAgent(BaseAgent):
    name = "supervisor"
    role = AgentRole.SUPERVISOR
    allowed_tools: frozenset[str] = frozenset()
    mandate = "Propose a sequential project plan: usually 3-8 phases, one for simple tasks."
    legacy_plan = False

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

    async def run(self, *, task_id: str, goal: str, context: str = "") -> AgentResult:
        plan = await self.plan(task_id=task_id, goal=goal)
        return AgentResult(
            agent=self.name,
            role=self.role.value,
            status=AgentStatus.COMPLETED,
            summary=plan.summary,
            claims={"project_plan": plan.model_dump()},
        )

    def response_schema(self) -> dict[str, Any]:
        schema = ProjectPlan.model_json_schema()
        schema["$defs"]["PhasePlan"]["properties"]["workers"]["items"] = {
            "type": "string",
            "enum": list(self._workers),
        }
        return closed_schema(schema)

    def system_prompt(self) -> str:
        roster = "\n".join(
            f"{name}: {', '.join(sorted(worker.allowed_tools))}"
            for name, worker in self._workers.items()
        )
        return (
            f"You are the project planner. {self.mandate}\n{SECURITY_PREAMBLE}\n"
            f"Registered workers and capabilities:\n{roster}\n"
            "Supervisor has ZERO tools. Proposing requirements grants no authority. "
            "Return a ProjectPlan JSON with summary, phases and optional web_requests. "
            "Each phase has id (safe lowercase), title, a small exact goal, workers, depends_on "
            "(earlier IDs only), requirements (AcceptanceCriteria object), verification "
            "(environment/build/test/visual_qa), workflow (workers or visual). "
            "Use only registered workers, in researcher/coder/reviewer order, only when useful. "
            "Never assign implementation or writing to an agent lacking filesystem.write. "
            "For Flask: environment, backend, frontend, verification, final visual QA. "
            "Use workflow=visual for browser QA/review/repair. A reviewer-only visual phase "
            "starts with QA; Coder is called only if repair is needed. "
            "requirements supports required_files, required_read_after_write, "
            "required_reviewer_files, required_workers, required_review_verdict and "
            "require_readback_all_modified. File paths are workspace-relative. "
            "Keep phases bounded (at most 12). Workers receive only the current phase. "
            "Do not restate the entire task in every phase. The concise summary must retain "
            "the global objective. Do not put arbitrary prose into deterministic check fields. "
            "Operator-required research and final acceptance cannot be removed by your plan. "
            "Optional web_requests are public-information requests only, never local data/secrets."
        )

    async def plan(self, *, task_id: str, goal: str) -> ProjectPlan:
        await self.events.emit(EventType.AGENT_STARTED, task_id=task_id, actor=self.name)
        raw = await self._ask_model(
            system=self.system_prompt(),
            messages=[Message(role=Role.USER, content=f"Task: {goal}")],
            task_id=task_id,
            step=1,
            goal=goal,
        )
        payload = extract_json_object(raw)
        try:
            if payload is None:
                raise ValueError("missing plan")
            self.legacy_plan = "phases" not in payload
            if self.legacy_plan:
                # Old scripted responses migrate into the same phase engine.
                payload = {
                    "summary": str(payload.get("plan") or "Complete the task")[:1500],
                    "phases": [
                        {
                            "id": "implementation",
                            "title": "Implementation",
                            "goal": str(payload.get("plan") or goal)[:2000],
                            "workers": payload.get("workers", []),
                        }
                    ],
                    "web_requests": payload.get("web_requests", []),
                }
            plan = ProjectPlan.model_validate(payload)
            if any(name not in self._workers for phase in plan.phases for name in phase.workers):
                raise ValueError("worker is not registered")
        except (ValueError, ValidationError) as exc:
            await self.events.emit(
                EventType.MODEL_INVALID_RESPONSE,
                task_id=task_id,
                actor=self.name,
                reason="invalid project plan",
            )
            raise ModelError("invalid project plan; no phases executed") from exc
        return plan
