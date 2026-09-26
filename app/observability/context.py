"""Task-local phase context, inherited by nested async tools without model input."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class PhaseContext:
    task_id: str
    phase_id: str
    phase_index: int  # human-facing, one based
    phase_title: str
    phase_attempt: int
    phase_count: int
    observe: Callable[[Any], Awaitable[None]] | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in ("phase_id", "phase_index", "phase_title", "phase_attempt", "phase_count")
        }


CURRENT_PHASE: ContextVar[PhaseContext | None] = ContextVar("current_phase", default=None)
