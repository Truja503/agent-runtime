"""The broker: the only path from an agent to a capability."""

from __future__ import annotations

import pytest

from app.observability.events import EventBus, EventType
from app.observability.store import InMemoryEventStore
from app.policy.engine import PolicyEngine
from app.policy.permissions import AgentRole
from app.privileged_bridge import InProcessPrivilegedGateway
from app.tools.broker import InvocationStatus, ToolBroker, ToolInvocation
from app.tools.registry import ToolRegistry
from privileged.schemas import RequestStatus
from privileged.service import PrivilegedRequestService
from tests.conftest import principal_for


@pytest.fixture
def sink() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def broker(
    registry: ToolRegistry,
    sink: InMemoryEventStore,
    privileged_service: PrivilegedRequestService,
) -> ToolBroker:
    return ToolBroker(
        registry=registry,
        policy=PolicyEngine(),
        events=EventBus([sink]),
        privileged_gateway=InProcessPrivilegedGateway(privileged_service),
    )


def event_types(sink: InMemoryEventStore) -> list[EventType]:
    return [event.type for event in sink.events]


async def test_allowed_tool_executes(broker: ToolBroker, sink: InMemoryEventStore) -> None:
    coder = principal_for(AgentRole.CODER, tools={"filesystem.write"})
    result = await broker.invoke(
        coder, ToolInvocation(tool="filesystem.write", arguments={"path": "n.md", "content": "x"})
    )
    assert result.status is InvocationStatus.COMPLETED
    assert result.output is not None
    assert result.output["path"] == "n.md"
    assert EventType.TOOL_ALLOWED in event_types(sink)
    assert EventType.TOOL_COMPLETED in event_types(sink)


async def test_denied_tool_fails_and_is_audited(
    broker: ToolBroker, sink: InMemoryEventStore
) -> None:
    researcher = principal_for(AgentRole.RESEARCHER, tools={"filesystem.write"})
    result = await broker.invoke(
        researcher,
        ToolInvocation(tool="filesystem.write", arguments={"path": "n.md", "content": "x"}),
    )
    assert result.status is InvocationStatus.DENIED
    assert result.rule_id == "R2-missing-permission"

    denials = [e for e in sink.events if e.type is EventType.TOOL_DENIED]
    assert len(denials) == 1
    assert denials[0].actor == "researcher"
    assert denials[0].payload["tool"] == "filesystem.write"
    assert denials[0].payload["rule_id"] == "R2-missing-permission"


async def test_denied_tool_never_runs_the_handler(
    broker: ToolBroker, workspace: object
) -> None:
    researcher = principal_for(AgentRole.RESEARCHER, tools={"filesystem.write"})
    await broker.invoke(
        researcher,
        ToolInvocation(tool="filesystem.write", arguments={"path": "leak.txt", "content": "x"}),
    )
    assert not (workspace.root / "leak.txt").exists()  # type: ignore[attr-defined]


async def test_unknown_tool_is_denied(broker: ToolBroker) -> None:
    coder = principal_for(AgentRole.CODER, tools={"shell.exec"})
    result = await broker.invoke(coder, ToolInvocation(tool="shell.exec", arguments={}))
    assert result.status is InvocationStatus.DENIED
    assert result.rule_id == "R0-unknown-tool"


async def test_invalid_arguments_are_rejected_before_the_handler(
    broker: ToolBroker,
) -> None:
    coder = principal_for(AgentRole.CODER, tools={"filesystem.write"})
    result = await broker.invoke(
        coder, ToolInvocation(tool="filesystem.write", arguments={"path": "x.md"})
    )
    assert result.status is InvocationStatus.FAILED
    assert "invalid arguments" in (result.reason or "")


async def test_privileged_request_becomes_pending_approval(
    broker: ToolBroker,
    sink: InMemoryEventStore,
    privileged_service: PrivilegedRequestService,
    privileged_runner: object,
) -> None:
    coder = principal_for(AgentRole.CODER, tools={"system.request_privileged_action"})
    result = await broker.invoke(
        coder,
        ToolInvocation(
            tool="system.request_privileged_action",
            arguments={"request": "please restart nginx"},
        ),
    )

    assert result.status is InvocationStatus.APPROVAL_REQUIRED
    assert result.request_id is not None
    assert result.output is None  # nothing was executed, so there is no output

    record = await privileged_service.get_request(result.request_id)
    assert record is not None
    assert record.status is RequestStatus.AWAITING_APPROVAL
    assert record.intent is not None
    assert record.intent.service == "nginx"

    # Nothing ran.
    assert privileged_runner.calls == []  # type: ignore[attr-defined]
    assert EventType.PRIVILEGED_ACTION_REQUESTED in event_types(sink)
    assert EventType.TOOL_COMPLETED not in event_types(sink)


async def test_role_without_privileged_capability_cannot_even_request(
    broker: ToolBroker,
) -> None:
    reviewer = principal_for(AgentRole.REVIEWER, tools={"system.request_privileged_action"})
    result = await broker.invoke(
        reviewer,
        ToolInvocation(
            tool="system.request_privileged_action", arguments={"request": "restart nginx"}
        ),
    )
    assert result.status is InvocationStatus.DENIED


async def test_broker_without_a_gateway_denies_privileged_requests(
    registry: ToolRegistry, sink: InMemoryEventStore
) -> None:
    broker = ToolBroker(
        registry=registry,
        policy=PolicyEngine(),
        events=EventBus([sink]),
        privileged_gateway=None,
    )
    coder = principal_for(AgentRole.CODER, tools={"system.request_privileged_action"})
    result = await broker.invoke(
        coder,
        ToolInvocation(
            tool="system.request_privileged_action", arguments={"request": "restart nginx"}
        ),
    )
    assert result.status is InvocationStatus.DENIED
