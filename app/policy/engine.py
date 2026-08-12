"""Deterministic authorisation.

The engine takes structured facts — a principal, a tool spec — and returns a
decision. It never reads free text, never calls a model, and has no I/O. Given
the same inputs it always returns the same answer, which is what makes it
testable and what makes prompt injection irrelevant to it.

An LLM may classify intent. An LLM never decides authorisation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.policy.permissions import Capability, Principal, RiskLevel, risk_exceeds


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(BaseModel):
    effect: Effect
    reason: str
    rule_id: str

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


class ToolFacts(BaseModel):
    """The subset of a tool's metadata that authorisation depends on."""

    name: str
    risk: RiskLevel
    privileged: bool
    required_permissions: frozenset[Capability]


class PolicyEngine:
    """Ordered rules, first match wins. Default is deny."""

    def evaluate(self, principal: Principal, tool: ToolFacts) -> PolicyDecision:
        # R1 — the agent must have declared the tool in its own allow-list.
        # This is least privilege at the agent level, checked by the broker
        # rather than trusted to the agent.
        if tool.name not in principal.allowed_tools:
            return PolicyDecision(
                effect=Effect.DENY,
                rule_id="R1-not-declared",
                reason=(
                    f"agent {principal.name!r} did not declare {tool.name!r} "
                    "in its allowed tools"
                ),
            )

        # R2 — the role must hold every capability the tool requires.
        missing = tool.required_permissions - principal.permissions
        if missing:
            return PolicyDecision(
                effect=Effect.DENY,
                rule_id="R2-missing-permission",
                reason=(
                    f"role {principal.role.value!r} lacks "
                    f"{', '.join(sorted(capability.value for capability in missing))}"
                ),
            )

        # R3 — the core invariant, checked before the risk ceiling because a
        # privileged tool is never executed here at all: the ceiling governs
        # what may *run*, and this path only ever produces a request. No model
        # and no role changes the outcome; the best an agent can get is a
        # pending request for a human.
        if tool.privileged:
            return PolicyDecision(
                effect=Effect.REQUIRE_APPROVAL,
                rule_id="R3-privileged-requires-human",
                reason=(
                    f"{tool.name!r} is privileged; it requires an authenticated "
                    "human operator and cannot be executed by an agent "
                    f"(requesting model is {principal.model_kind.value})"
                ),
            )

        # R4 — independent bound on directly-executable tools, so a mis-granted
        # permission is not sufficient on its own to run something dangerous.
        if risk_exceeds(tool.risk, principal.risk_ceiling):
            return PolicyDecision(
                effect=Effect.DENY,
                rule_id="R4-risk-ceiling",
                reason=(
                    f"tool risk {tool.risk.value!r} exceeds ceiling "
                    f"{principal.risk_ceiling.value!r} for role {principal.role.value!r}"
                ),
            )

        return PolicyDecision(
            effect=Effect.ALLOW,
            rule_id="R5-allow",
            reason=f"role {principal.role.value!r} may use {tool.name!r}",
        )
