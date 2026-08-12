"""Authorisation rules, checked directly against the engine."""

from __future__ import annotations

import pytest

from app.config import ProviderKind
from app.policy.engine import Effect, PolicyEngine, ToolFacts
from app.policy.permissions import (
    ROLE_PERMISSIONS,
    AgentRole,
    Capability,
    Principal,
    RiskLevel,
)
from tests.conftest import principal_for

ALL_TOOLS = {
    "filesystem.read",
    "filesystem.write",
    "filesystem.list",
    "tests.run",
    "github.read",
    "system.request_privileged_action",
}

READ = ToolFacts(
    name="filesystem.read",
    risk=RiskLevel.LOW,
    privileged=False,
    required_permissions=frozenset({Capability.FILESYSTEM_READ}),
)
WRITE = ToolFacts(
    name="filesystem.write",
    risk=RiskLevel.MEDIUM,
    privileged=False,
    required_permissions=frozenset({Capability.FILESYSTEM_WRITE}),
)
RUN_TESTS = ToolFacts(
    name="tests.run",
    risk=RiskLevel.MEDIUM,
    privileged=False,
    required_permissions=frozenset({Capability.TESTS_RUN}),
)
PRIVILEGED = ToolFacts(
    name="system.request_privileged_action",
    risk=RiskLevel.PRIVILEGED,
    privileged=True,
    required_permissions=frozenset({Capability.PRIVILEGED_REQUEST}),
)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


def test_researcher_can_read(engine: PolicyEngine) -> None:
    decision = engine.evaluate(principal_for(AgentRole.RESEARCHER, tools=ALL_TOOLS), READ)
    assert decision.effect is Effect.ALLOW


def test_researcher_cannot_write(engine: PolicyEngine) -> None:
    decision = engine.evaluate(principal_for(AgentRole.RESEARCHER, tools=ALL_TOOLS), WRITE)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == "R2-missing-permission"
    assert "filesystem.write" in decision.reason


def test_reviewer_cannot_write(engine: PolicyEngine) -> None:
    decision = engine.evaluate(principal_for(AgentRole.REVIEWER, tools=ALL_TOOLS), WRITE)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == "R2-missing-permission"


def test_reviewer_can_run_tests(engine: PolicyEngine) -> None:
    decision = engine.evaluate(principal_for(AgentRole.REVIEWER, tools=ALL_TOOLS), RUN_TESTS)
    assert decision.effect is Effect.ALLOW


def test_coder_can_write(engine: PolicyEngine) -> None:
    decision = engine.evaluate(principal_for(AgentRole.CODER, tools=ALL_TOOLS), WRITE)
    assert decision.effect is Effect.ALLOW


def test_supervisor_holds_no_permissions(engine: PolicyEngine) -> None:
    assert ROLE_PERMISSIONS[AgentRole.SUPERVISOR] == frozenset()
    decision = engine.evaluate(principal_for(AgentRole.SUPERVISOR, tools=ALL_TOOLS), READ)
    assert decision.effect is Effect.DENY


def test_undeclared_tool_is_denied_even_when_the_role_permits_it(
    engine: PolicyEngine,
) -> None:
    """Least privilege at the agent level, enforced rather than trusted."""
    coder = principal_for(AgentRole.CODER, tools={"filesystem.read"})
    decision = engine.evaluate(coder, WRITE)
    assert decision.effect is Effect.DENY
    assert decision.rule_id == "R1-not-declared"


def test_risk_ceiling_is_an_independent_bound(engine: PolicyEngine) -> None:
    """Even with the capability granted, a role cannot exceed its risk ceiling."""
    high_risk_read = ToolFacts(
        name="filesystem.read",
        risk=RiskLevel.HIGH,
        privileged=False,
        required_permissions=frozenset({Capability.FILESYSTEM_READ}),
    )
    decision = engine.evaluate(
        principal_for(AgentRole.RESEARCHER, tools=ALL_TOOLS), high_risk_read
    )
    assert decision.effect is Effect.DENY
    assert decision.rule_id == "R4-risk-ceiling"


@pytest.mark.parametrize(
    "kind", [ProviderKind.ANTHROPIC, ProviderKind.OPENAI, ProviderKind.LOCAL]
)
def test_no_agent_ever_gets_direct_privileged_execution(
    engine: PolicyEngine, kind: ProviderKind
) -> None:
    """The core invariant, across every role and every model backend."""
    for role in AgentRole:
        decision = engine.evaluate(
            principal_for(role, tools=ALL_TOOLS, kind=kind), PRIVILEGED
        )
        assert decision.effect is not Effect.ALLOW
        if role is AgentRole.CODER:
            # A coder may ask; asking is not doing.
            assert decision.effect is Effect.REQUIRE_APPROVAL
        else:
            assert decision.effect is Effect.DENY


def test_privileged_is_never_allowed_even_if_permissions_were_misconfigured(
    engine: PolicyEngine,
) -> None:
    """A privileged tool requiring nothing at all still cannot be executed."""
    misconfigured = ToolFacts(
        name="system.oops",
        risk=RiskLevel.PRIVILEGED,
        privileged=True,
        required_permissions=frozenset(),
    )
    reckless = Principal(
        name="reckless",
        role=AgentRole.CODER,
        model_kind=ProviderKind.ANTHROPIC,
        allowed_tools=frozenset({"system.oops"}),
    )
    # R3 catches it before the risk ceiling is even consulted.
    assert engine.evaluate(reckless, misconfigured).effect is not Effect.ALLOW
