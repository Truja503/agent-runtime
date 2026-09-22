"""Structured review claims, separate from tool-derived execution evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["info", "warning", "critical"]
    category: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    affected_files: list[str] = Field(default_factory=list, max_length=100)
    # References are claims until joined to real broker events. They never prove a defect.
    evidence_event_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewedCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1, max_length=1000)
    result: Literal["pass", "fail", "not_verified"]
    required: bool = True


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pass", "pass_with_warnings", "fail"]
    summary: str = Field(min_length=1, max_length=4000)
    source_inspection: str = Field(default="not reported", max_length=2000)
    visual_inspection: Literal["not_performed", "rendered_dom_only", "screenshots_provided"] = (
        "not_performed"
    )
    runtime_errors: list[str] = Field(default_factory=list, max_length=50)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=100)
    acceptance_criteria: list[ReviewedCriterion] = Field(default_factory=list, max_length=100)
