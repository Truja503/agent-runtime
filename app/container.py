"""Composition root.

Everything is constructed here and passed in explicitly. There are no module
level singletons, which is what makes it possible to stand up a complete
runtime in a test with a scripted model and a temp directory.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.base import BaseAgent
from app.agents.coder import CoderAgent
from app.agents.researcher import ResearcherAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.supervisor import SupervisorAgent
from app.agents.web import WebAgent, register_web_request, register_web_tools
from app.config import PrivilegedParserKind, Settings
from app.models.base import ModelProvider
from app.models.profiles import ModelPool, load_configuration
from app.observability.events import EventBus, EventSink, EventType
from app.observability.prompts import PrivacyFilter, PromptInspection
from app.observability.store import SQLiteEventStore
from app.policy.engine import PolicyEngine
from app.privileged_bridge import InProcessPrivilegedGateway, PrivilegedGateway
from app.tasks.evidence import evaluate_acceptance, execution_evidence
from app.tasks.manager import TaskManager
from app.tasks.state import TERMINAL_STATUSES, TaskStatus, TaskStore
from app.tasks.store import SQLiteTaskStore
from app.tasks.visual import run_visual_workflow
from app.tools.broker import InvocationStatus, ToolBroker, ToolResult
from app.tools.browser import BrowserTools
from app.tools.builtin import build_registry
from app.tools.filesystem import Workspace
from app.tools.registry import ToolRegistry
from app.tools.web import WebBroker
from privileged.audit import AuditSink
from privileged.auth import OperatorAuthenticator
from privileged.executor import PrivilegedExecutor
from privileged.local_llm import IntentParser, LocalModelIntentParser, RuleBasedIntentParser
from privileged.service import PrivilegedRequestService
from privileged.store import SQLitePrivilegedRequestStore

logger = logging.getLogger(__name__)


class EventBusAuditSink:
    """Forwards privileged audit records into the application event bus.

    The privileged package knows nothing about this class — it only sees the
    ``AuditSink`` protocol — so replacing it with syslog in a standalone daemon
    changes nothing on the privileged side.
    """

    _MAPPING = {
        "privileged_action_requested": EventType.PRIVILEGED_ACTION_REQUESTED,
        "privileged_action_approved": EventType.PRIVILEGED_ACTION_APPROVED,
        "privileged_action_denied": EventType.PRIVILEGED_ACTION_DENIED,
        "privileged_action_executed": EventType.PRIVILEGED_ACTION_EXECUTED,
        "privileged_action_failed": EventType.PRIVILEGED_ACTION_FAILED,
    }

    def __init__(self, events: EventBus) -> None:
        self._events = events

    async def record(self, event_type: str, payload: dict[str, Any]) -> None:
        mapped = self._MAPPING.get(event_type)
        if mapped is None:  # pragma: no cover - defensive
            return
        await self._events.emit(
            mapped,
            task_id=payload.get("task_id"),
            actor=payload.get("operator") or payload.get("requested_by"),
            **{k: v for k, v in payload.items() if k not in {"task_id"}},
        )


@dataclass
class Runtime:
    """The assembled application."""

    settings: Settings
    events: EventBus
    tasks: TaskManager
    registry: ToolRegistry
    broker: ToolBroker
    supervisor: SupervisorAgent
    workers: dict[str, BaseAgent]
    model: ModelProvider
    privileged_service: PrivilegedRequestService
    privileged_gateway: PrivilegedGateway
    pool: ModelPool
    inspection: PromptInspection
    web: WebAgent
    web_broker: WebBroker
    _background: set[asyncio.Task[None]] = field(default_factory=set)
    _jobs: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_health: dict[str, str] = field(default_factory=dict)
    active_agents: dict[str, dict[str, BaseAgent]] = field(default_factory=dict)
    _started: bool = False
    browser_health: dict[str, Any] = field(default_factory=dict)
    _approval_waiters: set[str] = field(default_factory=set)

    async def browser_readiness(self) -> dict[str, Any]:
        self.browser_health = await BrowserTools(
            Workspace(self.settings.workspace_root)
        ).readiness()
        return self.inspection.privacy.clean(self.browser_health)

    async def wait_for_approval(self, task_id: str, result: ToolResult) -> ToolResult:
        assert result.request_id
        self._approval_waiters.add(task_id)
        task = await self.tasks.get(task_id)
        resume_status = task.status
        await self.tasks.record_result(
            task_id,
            {
                **(task.result or {}),
                "pending_approvals": [result.request_id],
                "approvals": [{"request_id": result.request_id, "status": "awaiting_approval"}],
            },
        )
        await self.tasks.transition(task_id, TaskStatus.WAITING_FOR_APPROVAL)
        try:
            while True:
                self.broker.check_cancelled(task_id)
                ticket = await self.privileged_gateway.status(result.request_id)
                if ticket is None or ticket.status != "awaiting_approval":
                    break
                await asyncio.sleep(0.25)
            status = ticket.status if ticket else "missing"
            reason = {
                "denied": "approval_rejected",
                "rejected": "approval_rejected",
                "expired": "approval_expired",
                "missing": "approval_missing",
            }.get(status, f"approval_{status}")
            output = ticket.result if ticket else None
            resolved = ToolResult(
                status=InvocationStatus.COMPLETED
                if status == "executed"
                else InvocationStatus.FAILED,
                tool=result.tool,
                request_id=result.request_id,
                output=output,
                reason=reason,
            )
            task = await self.tasks.get(task_id)
            history = list((task.result or {}).get("approval_history", []))
            history.append({"request_id": result.request_id, "status": status, "reason": reason})
            await self.tasks.record_result(
                task_id,
                {
                    **(task.result or {}),
                    "pending_approvals": [],
                    "approvals": history,
                    "approval_history": history,
                },
            )
            await self.events.emit(
                EventType.TOOL_COMPLETED if status == "executed" else EventType.TOOL_FAILED,
                task_id=task_id,
                actor="approval_lifecycle",
                tool=result.tool,
                request_id=result.request_id,
                reason=reason,
                result=output or {},
            )
            await self.tasks.transition(task_id, resume_status)
            return resolved
        finally:
            self._approval_waiters.discard(task_id)

    async def reconcile_approvals(self) -> None:
        for task in await self.tasks.waiting_tasks():
            if task.id in self._approval_waiters:
                continue
            ids = (task.result or {}).get("pending_approvals", [])
            if not ids:
                ids = list(
                    dict.fromkeys(
                        e.payload["request_id"]
                        for e in await self.tasks.events_for(task.id)
                        if e.type == EventType.PRIVILEGED_ACTION_REQUESTED
                        and e.payload.get("request_id")
                    )
                )
            records = []
            for request_id in ids:
                ticket = await self.privileged_gateway.status(request_id)
                records.append(
                    {"request_id": request_id, "status": ticket.status if ticket else "missing"}
                )
            await self.tasks.record_result(task.id, {**(task.result or {}), "approvals": records})
            if records and all(r["status"] == "awaiting_approval" for r in records):
                continue
            statuses = {r["status"] for r in records}
            reason = (
                "approval_missing"
                if not records or "missing" in statuses
                else "approval_expired"
                if "expired" in statuses
                else "approval_rejected"
                if statuses & {"denied", "rejected"}
                else "approval_failed"
                if "failed" in statuses
                else "approval_completed_workflow_interrupted"
            )
            await self.tasks.fail(task.id, reason)

    async def start(self) -> None:
        if not self._started:
            await self.tasks.reconcile_startup()
            await self.reconcile_approvals()
            await self.browser_readiness()
            self._started = True

            async def reconcile() -> None:
                while True:
                    await asyncio.sleep(1)
                    await self.reconcile_approvals()

            monitor = asyncio.create_task(reconcile())
            self._background.add(monitor)
            monitor.add_done_callback(self._background.discard)

    def schedule_task(self, task_id: str) -> asyncio.Task[None]:
        """Run a task in the background and keep a reference so it is not GC'd."""
        if task_id in self._jobs:
            return self._jobs[task_id]
        job = asyncio.create_task(self.run_task(task_id))
        self._jobs[task_id] = job
        self._background.add(job)
        job.add_done_callback(self._background.discard)
        job.add_done_callback(lambda _: self._jobs.pop(task_id, None))
        return job

    async def stop_execution(self, task_id: str) -> None:
        self.broker.cancelled_tasks.add(task_id)
        job = self._jobs.get(task_id)
        if job and job is not asyncio.current_task():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def run_task(self, task_id: str) -> None:
        # A single workspace has one writer at a time. Configuration is stable
        # while jobs are queued/running; per-task overrides use agent copies.
        current = asyncio.current_task()
        if current:
            self._jobs[task_id] = current
        try:
            async with self._execution_lock:
                await self._execute_task(task_id)
        finally:
            self._jobs.pop(task_id, None)
            self.active_agents.pop(task_id, None)

    async def _execute_task(self, task_id: str) -> None:
        task = await self.tasks.get(task_id)
        if task.status in TERMINAL_STATUSES:
            return
        try:
            self.broker.check_cancelled(task_id)
            workers = {name: copy.copy(worker) for name, worker in self.workers.items()}
            supervisor = copy.copy(self.supervisor)
            supervisor._workers = {name: workers[name] for name in self.supervisor._workers}

            async def worker_start(role: str) -> None:
                status = TaskStatus.REVIEWING if role == "reviewer" else TaskStatus.RUNNING
                current = await self.tasks.get(task_id)
                if current.status == TaskStatus.PLANNING:
                    await self.tasks.transition(task_id, TaskStatus.RUNNING)
                await self.tasks.transition(task_id, status)
                if task.options.recoverable_execution and not task.options.visual_project:
                    await self.tasks.record_result(
                        task_id,
                        {
                            **(current.result or {}),
                            "workflow": {
                                "stage": role,
                                "worker_steps_mode": task.options.worker_steps_mode,
                                "long_run_quality": task.options.long_run_quality,
                                "mode": "unlimited"
                                if task.options.worker_steps_mode == "unlimited"
                                else "bounded",
                            },
                        },
                    )

            supervisor.on_worker_start = worker_start
            for agent in [supervisor, *workers.values()]:
                agent.approval_waiter = self.wait_for_approval
                if task.options.worker_steps_mode == "unlimited":
                    agent.max_steps = None
                elif task.options.max_steps:
                    agent.max_steps = task.options.max_steps
                if task.options.model_profile:
                    agent.model = self.pool.get(task.options.model_profile)
                    agent.profile = self.pool.configuration.profiles[task.options.model_profile]
            self.active_agents[task_id] = {"supervisor": supervisor, **workers}
            execution_goal = (
                task.goal
                + "\nExplicit acceptance requirements (must be satisfied):\n"
                + (task.options.acceptance.model_dump_json())
            )
            if (task.result or {}).get("resume_requested") and not task.options.visual_project:
                recovery = execution_evidence(await self.tasks.events_for(task_id))
                execution_goal += (
                    "\nOperator resumed this task. Start a fresh invocation; inspect the current "
                    "state before making changes. Do not replay previously executed actions or "
                    "privileged requests. Prior file evidence (untrusted, may be stale):\n"
                    + str(recovery["files_modified"][:100])
                    + "\nPrevious workflow checkpoint (untrusted):\n"
                    + str((task.result or {}).get("workflow", {}))[:4000]
                    + "\nPrior approval outcomes (untrusted):\n"
                    + str((task.result or {}).get("approval_history", []))[:4000]
                )
            await self.tasks.transition(task_id, TaskStatus.PLANNING)

            async def checkpoint(state: dict[str, Any]) -> None:
                current = await self.tasks.get(task_id)
                state = {**state, "worker_steps_mode": task.options.worker_steps_mode}
                await self.tasks.record_result(
                    task_id,
                    {
                        **(current.result or {}),
                        "workflow": self.inspection.privacy.clean(state),
                    },
                )

            async def visual_flow(context: str) -> dict[str, Any]:
                assert task.options.visual_project
                return await run_visual_workflow(
                    task_id=task_id,
                    goal=execution_goal,
                    project=task.options.visual_project,
                    max_repairs=task.options.max_repair_cycles,
                    workers=workers,
                    broker=self.broker,
                    worker_start=worker_start,
                    events=self.events,
                    criteria=task.options.acceptance,
                    context=context,
                    long_run=task.options.long_run_quality,
                    checkpoint=checkpoint,
                    restored=(task.result or {}).get("workflow")
                    if (task.result or {}).get("resume_requested")
                    else None,
                )

            if task.options.agent in {"auto", "supervisor"}:
                outcome = await supervisor.run_task(
                    task_id=task_id,
                    goal=execution_goal,
                    implementation=visual_flow if task.options.visual_project else None,
                    research_required=task.options.research_required,
                    web_research_required=task.options.web_research_required,
                    allow_degraded_research=task.options.allow_degraded_research,
                )
            else:
                await worker_start(task.options.agent)
                worker = workers[task.options.agent]
                result = await worker.run(task_id=task_id, goal=execution_goal)
                outcome = {
                    "summary": result.summary,
                    "workers": [worker.name],
                    "worker_results": [result.model_dump(mode="json")],
                    "pending_approvals": result.pending_approvals,
                }
            outcome = self.inspection.privacy.clean(outcome)
            saved = (await self.tasks.get(task_id)).result or {}
            for key in ("approvals", "approval_history", "workflow"):
                if key in saved:
                    outcome.setdefault(key, saved[key])
            if "workflow" in outcome:
                outcome["workflow"]["worker_steps_mode"] = task.options.worker_steps_mode
            outcome["evidence"] = execution_evidence(await self.tasks.events_for(task_id))
            outcome["acceptance"] = evaluate_acceptance(
                task.options.acceptance,
                outcome["evidence"],
                outcome["worker_results"],
                outcome.get("visual_qa"),
            )
            outcome["acceptance_failures"] = outcome["acceptance"]["failures"]
            outcome["acceptance_failures"].extend(outcome.get("routing_failures", []))
            if outcome["acceptance_failures"]:
                outcome["acceptance"]["status"] = "rejected"
            if outcome.get("workflow_status") == "stalled" or any(
                r["status"] == "stalled" for r in outcome["worker_results"]
            ):
                outcome["acceptance"]["status"] = "stalled"
            outcome["review"] = outcome["acceptance"]["review"]
            outcome["models"] = {
                a.name: {"provider": a.model.name, "model": a.model.model}
                for a in [supervisor, *workers.values()]
                if a.name in outcome["workers"]
                or (a.name == "supervisor" and task.options.agent in {"auto", "supervisor"})
            }
            await self.tasks.record_result(task_id, outcome)
        except Exception as exc:  # a failed task must never take down the process
            reason = self.inspection.privacy.text(f"{type(exc).__name__}: {exc}")
            logger.error("task %s failed: %s", task_id, reason)
            await self.tasks.fail(task_id, reason)
            return

        if outcome.get("workflow_status") == "stalled" or any(
            r["status"] == "stalled" for r in outcome["worker_results"]
        ):
            await self.tasks.transition(task_id, TaskStatus.STALLED)
            return

        if outcome.get("pending_approvals"):
            # The work stops here on purpose: something in it needs a human,
            # and no agent can supply that.
            await self.tasks.transition(task_id, TaskStatus.WAITING_FOR_APPROVAL)
            await self.events.emit(
                EventType.TASK_STATUS_CHANGED,
                task_id=task_id,
                actor="supervisor",
                note="blocked on privileged approval",
                pending_approvals=outcome["pending_approvals"],
            )
            return

        if (
            not outcome["worker_results"]
            or outcome.get("workflow_status") == "failed"
            or outcome["acceptance_failures"]
            or any(r["status"] != "completed" for r in outcome["worker_results"])
        ):
            await self.tasks.fail(task_id, "worker or acceptance criteria failed; see task detail")
            return
        await self.tasks.transition(task_id, TaskStatus.REVIEWING)
        await self.tasks.complete(task_id, outcome)

    async def aclose(self) -> None:
        for job in list(self._background):
            job.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.pool.aclose()
        self.inspection.entries.clear()


def build_runtime(
    settings: Settings,
    *,
    model: ModelProvider | None = None,
    task_store: TaskStore | None = None,
    event_sinks: list[EventSink] | None = None,
    registry: ToolRegistry | None = None,
    privileged_service: PrivilegedRequestService | None = None,
    intent_parser: IntentParser | None = None,
    audit_sink: AuditSink | None = None,
) -> Runtime:
    events = EventBus(
        event_sinks if event_sinks is not None else [SQLiteEventStore(settings.database_path)]
    )
    tasks = TaskManager(
        task_store if task_store is not None else SQLiteTaskStore(settings.database_path),
        events,
    )

    privileged = privileged_service or _build_privileged_service(
        settings,
        parser=intent_parser,
        audit=audit_sink or EventBusAuditSink(events),
    )
    gateway = InProcessPrivilegedGateway(privileged)

    workspace = Workspace(settings.workspace_root)
    tool_registry = registry or build_registry(workspace=workspace)
    broker = ToolBroker(
        registry=tool_registry,
        policy=PolicyEngine(),
        events=events,
        privileged_gateway=gateway,
    )

    pool = ModelPool(settings, load_configuration(settings), model)
    inspection = PromptInspection(settings.dev_prompt_inspection, PrivacyFilter(settings))
    for profile in pool.configuration.profiles.values():
        key = profile.credential(settings)
        if key:
            inspection.privacy.secrets.append(key.get_secret_value())
    events.privacy = inspection.privacy.clean
    web_broker = WebBroker(
        workspace=workspace,
        enabled=lambda: settings.internet_access_enabled,
        privacy=inspection.privacy,
        events=events,
    )
    register_web_tools(tool_registry, web_broker)
    web = WebAgent(broker)
    register_web_request(tool_registry, web)
    provider = pool.for_agent("supervisor")
    workers: dict[str, BaseAgent] = {
        role: cls(
            model=pool.for_agent(role),
            profile=pool.profile_for(role),
            max_steps=pool.steps_for(role),
            broker=broker,
            events=events,
            registry=tool_registry,
            inspection=inspection,
        )
        for role, cls in {
            "researcher": ResearcherAgent,
            "coder": CoderAgent,
            "reviewer": ReviewerAgent,
        }.items()
    }
    supervisor = SupervisorAgent(
        model=provider,
        broker=broker,
        events=events,
        workers=workers,
        profile=pool.profile_for("supervisor"),
        inspection=inspection,
        max_steps=pool.steps_for("supervisor"),
        web=web,
    )
    for role, agent in {"supervisor": supervisor, **workers}.items():
        mandate = pool.configuration.agents[role].mandate
        if mandate is not None:
            agent.mandate = mandate

    runtime = Runtime(
        settings=settings,
        events=events,
        tasks=tasks,
        registry=tool_registry,
        broker=broker,
        supervisor=supervisor,
        workers=workers,
        model=provider,
        privileged_service=privileged,
        privileged_gateway=gateway,
        pool=pool,
        inspection=inspection,
        web=web,
        web_broker=web_broker,
    )
    tasks.stop_execution = runtime.stop_execution
    return runtime


def _build_privileged_service(
    settings: Settings,
    *,
    parser: IntentParser | None,
    audit: AuditSink,
) -> PrivilegedRequestService:
    if parser is None:
        if settings.privileged_parser is PrivilegedParserKind.LOCAL:
            parser = LocalModelIntentParser(
                base_url=settings.privileged_model_base_url,
                api_key=settings.privileged_model_api_key.get_secret_value(),
                model=settings.privileged_model_name,
            )
        else:
            parser = RuleBasedIntentParser()

    return PrivilegedRequestService(
        store=SQLitePrivilegedRequestStore(settings.privileged_database_path),
        authenticator=OperatorAuthenticator(
            {operator.operator_id: operator.secret_hash for operator in settings.operators}
        ),
        executor=PrivilegedExecutor(),
        parser=parser,
        audit=audit,
        approval_ttl_seconds=settings.privileged_approval_ttl_seconds,
    )
