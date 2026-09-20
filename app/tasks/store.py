"""Task stores. SQLite is the default; the in-memory one is for tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage import apply_schema, connect
from app.tasks.state import Task, TaskStatus

TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at DESC);
"""


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    async def create(self, task: Task) -> Task:
        self._tasks[task.id] = task.model_copy(deep=True)
        return task

    async def get(self, task_id: str) -> Task | None:
        found = self._tasks.get(task_id)
        return found.model_copy(deep=True) if found else None

    async def update(self, task: Task) -> Task:
        self._tasks[task.id] = task.model_copy(deep=True)
        return task

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        ordered = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return [task.model_copy(deep=True) for task in ordered[:limit]]


class SQLiteTaskStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        apply_schema(path, TASKS_DDL)
        with connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "options" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN options TEXT NOT NULL DEFAULT '{}'"
                )

    async def create(self, task: Task) -> Task:
        await self._write(self._insert, task)
        return task

    @staticmethod
    async def _write(operation: Any, task: Task) -> None:
        # Drain SQLite writes before cancellation can persist its terminal state.
        write = asyncio.create_task(asyncio.to_thread(operation, task))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    def _insert(self, task: Task) -> None:
        with connect(self._path) as connection:
            connection.execute(
                "INSERT INTO tasks (id, goal, status, created_by, created_at, updated_at,"
                " result, error, options) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._row(task),
            )

    async def get(self, task_id: str) -> Task | None:
        return await asyncio.to_thread(self._get, task_id)

    def _get(self, task_id: str) -> Task | None:
        with connect(self._path) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    async def update(self, task: Task) -> Task:
        await self._write(self._update, task)
        return task

    def _update(self, task: Task) -> None:
        with connect(self._path) as connection:
            connection.execute(
                "UPDATE tasks SET goal = ?, status = ?, created_by = ?, created_at = ?,"
                " updated_at = ?, result = ?, error = ?, options = ? WHERE id = ?",
                (*self._row(task)[1:], task.id),
            )

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        return await asyncio.to_thread(self._list, limit)

    def _list(self, limit: int) -> list[Task]:
        with connect(self._path) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._task(row) for row in rows]

    @staticmethod
    def _row(task: Task) -> tuple[object, ...]:
        return (
            task.id,
            task.goal,
            task.status.value,
            task.created_by,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            json.dumps(task.result) if task.result is not None else None,
            task.error,
            task.options.model_dump_json(),
        )

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            goal=row["goal"],
            status=TaskStatus(row["status"]),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            options=json.loads(row["options"]),
        )
