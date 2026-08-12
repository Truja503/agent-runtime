"""Capabilities, roles, and risk levels.

This module is plain data on purpose. No model, no heuristic, no network call
decides what a role may do — you can read the entire authorisation surface of
the runtime by reading this file.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from app.config import ProviderKind


class Capability(StrEnum):
    """Named permissions. Tools declare which ones they need."""

    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    TESTS_RUN = "tests.run"
    GITHUB_READ = "github.read"
    #: The right to *ask* for a privileged action. Not the right to perform one.
    PRIVILEGED_REQUEST = "privileged.request"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PRIVILEGED = "privileged"


_RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.PRIVILEGED,
)


def risk_exceeds(level: RiskLevel, ceiling: RiskLevel) -> bool:
    return _RISK_ORDER.index(level) > _RISK_ORDER.index(ceiling)


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"


#: The authorisation table. A role gets exactly what its job needs.
ROLE_PERMISSIONS: dict[AgentRole, frozenset[Capability]] = {
    # The supervisor coordinates. It plans and delegates; it touches nothing.
    AgentRole.SUPERVISOR: frozenset(),
    AgentRole.RESEARCHER: frozenset(
        {Capability.FILESYSTEM_READ, Capability.GITHUB_READ}
    ),
    AgentRole.CODER: frozenset(
        {
            Capability.FILESYSTEM_READ,
            Capability.FILESYSTEM_WRITE,
            Capability.TESTS_RUN,
            # A coder may *request* a privileged action (e.g. "restart nginx").
            # Requesting creates a pending record; it never executes anything.
            Capability.PRIVILEGED_REQUEST,
        }
    ),
    # The reviewer inspects and validates. Read plus tests, no write.
    AgentRole.REVIEWER: frozenset({Capability.FILESYSTEM_READ, Capability.TESTS_RUN}),
}

#: A second, independent bound. Even if a permission were mistakenly granted,
#: a role cannot run a tool above its risk ceiling.
ROLE_RISK_CEILING: dict[AgentRole, RiskLevel] = {
    AgentRole.SUPERVISOR: RiskLevel.LOW,
    AgentRole.RESEARCHER: RiskLevel.LOW,
    AgentRole.CODER: RiskLevel.HIGH,
    AgentRole.REVIEWER: RiskLevel.MEDIUM,
}


class Principal(BaseModel):
    """Who is asking. Constructed by the runtime, never by a model."""

    name: str
    role: AgentRole
    #: Which trust domain the agent's model lives in. A cloud-backed principal
    #: can never hold privileged authority, whatever its role says.
    model_kind: ProviderKind
    #: The agent's own declared least-privilege set. The broker intersects this
    #: with the role's permissions; declaring more than the role allows gains
    #: nothing.
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    task_id: str | None = None

    @property
    def permissions(self) -> frozenset[Capability]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    @property
    def risk_ceiling(self) -> RiskLevel:
        return ROLE_RISK_CEILING.get(self.role, RiskLevel.LOW)
