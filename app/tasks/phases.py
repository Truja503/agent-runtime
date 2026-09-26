"""Data-only plans and durable checkpoints. A plan never grants a capability."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tasks.evidence import AcceptanceCriteria
from app.tools.web import WebRequest

WorkerName = Literal["researcher", "coder", "reviewer"]
PhaseStatus = Literal[
    "pending",
    "running",
    "waiting_for_approval",
    "verifying",
    "passed",
    "failed",
    "stalled",
    "paused",
]


class PhasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=2000)
    workers: list[WorkerName] = Field(min_length=1, max_length=3)
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    requirements: AcceptanceCriteria = Field(default_factory=AcceptanceCriteria)
    verification: list[Literal["environment", "build", "test", "visual_qa"]] = Field(
        default_factory=list, max_length=4
    )
    workflow: Literal["workers", "visual"] = "workers"

    @model_validator(mode="after")
    def ordered_workers(self) -> PhasePlan:
        self.workers = sorted(set(self.workers), key=("researcher", "coder", "reviewer").index)
        if self.verification and not set(self.workers) & {"coder", "reviewer"}:
            raise ValueError("execution verification needs a selected coder or reviewer")
        if self.workflow == "visual" or "visual_qa" in self.verification:
            if "reviewer" not in self.workers:
                raise ValueError("visual phases require reviewer")
            self.workflow = "visual"
        return self


class ProjectPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1500)
    phases: list[PhasePlan] = Field(min_length=1, max_length=12)
    web_requests: list[WebRequest] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def sequential_dependencies(self) -> ProjectPlan:
        seen: set[str] = set()
        for phase in self.phases:
            if phase.id in seen or not set(phase.depends_on) <= seen:
                raise ValueError("phase IDs must be unique and depend only on earlier phases")
            seen.add(phase.id)
        return self


class PhaseState(BaseModel):
    id: str
    status: PhaseStatus = "pending"
    attempt: int = Field(default=0, ge=0)
    active_worker: str | None = None
    worker_results: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    outstanding: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    file_hashes: dict[str, str | None] = Field(default_factory=dict)
    pending_approvals: list[str] = Field(default_factory=list)
    last_meaningful_progress: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    stop_reason: str | None = None
    workflow: dict[str, Any] | None = None


class PhaseExecution(BaseModel):
    plan: ProjectPlan
    current_phase_index: int = Field(default=0, ge=0)
    phases: list[PhaseState]
    status: PhaseStatus = "pending"
    research_status: str = "not_requested"
    web_results: list[dict[str, Any]] = Field(default_factory=list)
    web_dispatched: bool = False

    @model_validator(mode="after")
    def checkpoint_matches_plan(self) -> PhaseExecution:
        if [p.id for p in self.phases] != [p.id for p in self.plan.phases]:
            raise ValueError("checkpoint phases must match the ordered project plan")
        if self.current_phase_index >= len(self.phases):
            raise ValueError("checkpoint current phase is outside the plan")
        return self

    @property
    def current(self) -> PhaseState:
        return self.phases[self.current_phase_index]
