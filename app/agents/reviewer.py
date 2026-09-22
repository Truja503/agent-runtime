"""Inspects and validates. Cannot change what it is reviewing."""

from __future__ import annotations

from pydantic import model_validator

from app.agents.base import AgentDecision, WorkerAgent
from app.policy.permissions import AgentRole
from app.tasks.review import ReviewResult


class ReviewerDecision(AgentDecision):
    review: ReviewResult | None = None

    @model_validator(mode="after")
    def require_review_on_finish(self) -> ReviewerDecision:
        if self.action == "finish" and self.review is None:
            raise ValueError("reviewer finish requires a structured review verdict")
        return self


class ReviewerAgent(WorkerAgent):
    name = "reviewer"
    decision_type = ReviewerDecision
    role = AgentRole.REVIEWER
    #: Read and run tests. No write: a reviewer that can edit the code it is
    #: reviewing is not a reviewer.
    allowed_tools = frozenset(
        {
            "filesystem.read",
            "filesystem.list",
            "tests.run",
            "browser.preview",
            "browser.screenshot",
            "browser.console_errors",
        }
    )
    mandate = (
        "Distinguish SOURCE INSPECTION, VISUAL INSPECTION, and RUNTIME ERRORS in findings. "
        "Use browser tools for rendered evidence. Never claim visual inspection without generated "
        "screenshots, or pixel inspection unless image inputs were actually supplied. "
        "PASS is a model verdict, not runtime evidence. When explicit inspection requirements "
        "exist, inspect those files completely. "
        "Validate work without modifying files. Read complete current versions of relevant "
        "files, following every continuation page. Do not infer missing code from a partial "
        "read. Run tests only when requested/permitted. On finish, include review: "
        "{verdict: pass|pass_with_warnings|fail, summary: string, findings: [{severity: "
        "info|warning|critical, category: string, message: string, affected_files: [paths], "
        "evidence_event_ids: []}], acceptance_criteria: [{criterion: string, result: "
        "pass|fail|not_verified, required: boolean}]}. Review is model-reviewed, not runtime "
        "proof. Critical defects require fail. Do not claim browser behavior without "
        "browser evidence. A completed review can reject the implementation."
    )
