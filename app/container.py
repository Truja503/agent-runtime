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
from app.config import PrivilegedParserKind, Settings
from app.models.base import ModelProvider
from app.models.profiles import ModelPool, load_configuration
from app.observability.events import EventBus, EventSink, EventType
from app.observability.prompts import PrivacyFilter, PromptInspection
from app.observability.store import SQLiteEventStore
from app.policy.engine import PolicyEngine
from app.privileged_bridge import InProcessPrivilegedGateway, PrivilegedGateway
from app.tasks.evidence import evaluate_criteria, execution_evidence
from app.tasks.manager import TaskManager
from app.tasks.state import TERMINAL_STATUSES, TaskStatus, TaskStore
from app.tasks.store import SQLiteTaskStore
from app.tools.broker import ToolBroker
from app.tools.builtin import build_registry
from app.tools.filesystem import Workspace
from app.tools.registry import ToolRegistry
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
    _background: set[asyncio.Task[None]] = field(default_factory=set)
    _jobs: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_health: dict[str, str] = field(default_factory=dict)

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

            supervisor.on_worker_start = worker_start
            for agent in [supervisor, *workers.values()]:
                if task.options.max_steps:
                    agent.max_steps = task.options.max_steps
                if task.options.model_profile:
                    agent.model = self.pool.get(task.options.model_profile)
                    agent.profile = self.pool.configuration.profiles[task.options.model_profile]
            await self.tasks.transition(task_id, TaskStatus.PLANNING)
            if task.options.agent in {"auto", "supervisor"}:
                outcome = await supervisor.run_task(task_id=task_id, goal=task.goal)
            else:
                await worker_start(task.options.agent)
                worker = workers[task.options.agent]
                result = await worker.run(task_id=task_id, goal=task.goal)
                outcome = {
                    "summary": result.summary,
                    "workers": [worker.name],
                    "worker_results": [result.model_dump(mode="json")],
                    "pending_approvals": result.pending_approvals,
                }
            outcome = self.inspection.privacy.clean(outcome)
            outcome["evidence"] = execution_evidence(await self.tasks.events_for(task_id))
            outcome["acceptance_failures"] = evaluate_criteria(
                task.options.acceptance, outcome["evidence"]
            )
            outcome["models"] = {
                a.name: {"provider": a.model.name, "model": a.model.model}
                for a in [supervisor, *workers.values()]
                if a.name in outcome["workers"] or (
                    a.name == "supervisor" and task.options.agent in {"auto", "supervisor"})
            }
            await self.tasks.record_result(task_id, outcome)
        except Exception as exc:  # a failed task must never take down the process
            reason = self.inspection.privacy.text(f"{type(exc).__name__}: {exc}")
            logger.error("task %s failed: %s", task_id, reason)
            await self.tasks.fail(task_id, reason)
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

        if outcome["acceptance_failures"] or any(
            r["status"] != "completed" for r in outcome["worker_results"]
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
    provider = pool.for_agent("supervisor")
    common = {
        "broker": broker,
        "events": events,
        "registry": tool_registry,
        "max_steps": settings.default_max_steps,
        "inspection": inspection,
    }
    workers: dict[str, BaseAgent] = {
        role: cls(model=pool.for_agent(role), profile=pool.profile_for(role), **common)  # type: ignore[arg-type]
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
