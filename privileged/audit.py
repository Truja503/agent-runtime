"""Audit sink for the privileged domain.

Defined here rather than imported from ``app`` so the privileged package has no
dependency on the application. The app supplies an adapter that forwards these
records into its own event bus; a standalone daemon can supply syslog, a file,
or nothing.
"""

from __future__ import annotations

from typing import Any, Protocol


class AuditSink(Protocol):
    async def record(self, event_type: str, payload: dict[str, Any]) -> None: ...


class NullAuditSink:
    """Used by the CLI when no richer sink is wired up."""

    async def record(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


class ListAuditSink:
    """Collects records in memory. Used by tests."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event_type: str, payload: dict[str, Any]) -> None:
        self.records.append((event_type, payload))
