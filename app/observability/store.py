"""Event sinks: in-memory (tests, ephemeral runs) and SQLite (default)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from app.observability.events import Event, EventType
from app.storage import apply_schema, connect

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    task_id     TEXT,
    actor       TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id, timestamp);
"""


class InMemoryEventStore:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append(self, event: Event) -> None:
        self.events.append(event)

    async def list_for_task(self, task_id: str) -> list[Event]:
        return [event for event in self.events if event.task_id == task_id]


class SQLiteEventStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        apply_schema(path, EVENTS_DDL)

    async def append(self, event: Event) -> None:
        await asyncio.to_thread(self._append, event)

    def _append(self, event: Event) -> None:
        with connect(self._path) as connection:
            connection.execute(
                "INSERT INTO events (id, type, timestamp, task_id, actor, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.type.value,
                    event.timestamp.isoformat(),
                    event.task_id,
                    event.actor,
                    json.dumps(event.payload, default=str),
                ),
            )

    async def list_for_task(self, task_id: str) -> list[Event]:
        return await asyncio.to_thread(self._list_for_task, task_id)

    def _list_for_task(self, task_id: str) -> list[Event]:
        with connect(self._path) as connection:
            rows = connection.execute(
                "SELECT id, type, timestamp, task_id, actor, payload FROM events"
                " WHERE task_id = ? ORDER BY timestamp, rowid",
                (task_id,),
            ).fetchall()
        return [
            Event(
                id=row["id"],
                type=EventType(row["type"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
                task_id=row["task_id"],
                actor=row["actor"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]
