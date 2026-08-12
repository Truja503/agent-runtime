"""Composition root.

Everything is constructed here and passed in explicitly. There are no module
level singletons, which is what makes it possible to stand up a complete
runtime in a test with a scripted model and a temp directory.
"""

from __future__ import annotations

import asyncio
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
from app.models.factory import ModelFactory
from app.observability.events import EventBus, EventSink, EventType
from app.observability.store import SQLiteEventStore
from app.policy.engine import PolicyEngine
from app.privileged_bridge import InProcessPrivilegedGateway, PrivilegedGateway
from app.tasks.manager import TaskManager
from app.tasks.state import TaskStatus, TaskStore
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
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    def schedule_task(self, task_id: str) -> asyncio.Task[None]:
        """Run a task in the background and keep a reference so it is not GC'd."""
        job = asyncio.create_task(self.run_task(task_id))
        self._background.add(job)
        job.add_done_callback(self._background.discard)
        return job

    async def run_task(self, task_id: str) -> None:
        task = await self.tasks.get(task_id)
        try:
            await self.tasks.transition(task_id, TaskStatus.PLANNING)
            await self.tasks.transition(task_id, TaskStatus.RUNNING)
            outcome = await self.supervisor.run_task(task_id=task_id, goal=task.goal)
        except Exception as exc:  # a failed task must never take down the process
            logger.exception("task %s failed", task_id)
            await self.tasks.fail(task_id, f"{type(exc).__name__}: {exc}")
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

        await self.tasks.transition(task_id, TaskStatus.REVIEWING)
        await self.tasks.complete(task_id, outcome)

    async def aclose(self) -> None:
        for job in list(self._background):
            job.cancel()
        await self.model.aclose()


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

    provider = model or ModelFactory.from_settings(settings)
    common = {
        "model": provider,
        "broker": broker,
        "events": events,
        "registry": tool_registry,
        "max_steps": settings.default_max_steps,
    }
    workers: dict[str, BaseAgent] = {
        "researcher": ResearcherAgent(**common),  # type: ignore[arg-type]
        "coder": CoderAgent(**common),  # type: ignore[arg-type]
        "reviewer": ReviewerAgent(**common),  # type: ignore[arg-type]
    }
    supervisor = SupervisorAgent(
        model=provider, broker=broker, events=events, workers=workers
    )

    return Runtime(
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
    )


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
