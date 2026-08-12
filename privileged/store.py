"""Storage for privileged requests.

This lives in its own database file, separate from the application's. In a real
deployment the two are owned by different Unix users: the app process may write
requests, and only the privileged daemon may write decisions. The split is
already reflected in the code, so making it real is a deployment change rather
than a rewrite.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from privileged.schemas import (
    INTENT_ADAPTER,
    ExecutionResult,
    PrivilegedRequest,
    RequestStatus,
)

REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS privileged_requests (
    request_id    TEXT PRIMARY KEY,
    requested_by  TEXT NOT NULL,
    task_id       TEXT,
    request_text  TEXT NOT NULL,
    intent        TEXT,
    status        TEXT NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT,
    decided_at    TEXT,
    approved_by   TEXT,
    result        TEXT,
    context       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_privileged_status ON privileged_requests (status, created_at);
"""


class PrivilegedRequestStore(Protocol):
    async def save(self, request: PrivilegedRequest) -> PrivilegedRequest: ...

    async def get(self, request_id: str) -> PrivilegedRequest | None: ...

    async def list_by_status(
        self, status: RequestStatus, limit: int = 50
    ) -> list[PrivilegedRequest]: ...


class InMemoryPrivilegedRequestStore:
    def __init__(self) -> None:
        self._requests: dict[str, PrivilegedRequest] = {}

    async def save(self, request: PrivilegedRequest) -> PrivilegedRequest:
        self._requests[request.request_id] = request.model_copy(deep=True)
        return request

    async def get(self, request_id: str) -> PrivilegedRequest | None:
        found = self._requests.get(request_id)
        return found.model_copy(deep=True) if found else None

    async def list_by_status(
        self, status: RequestStatus, limit: int = 50
    ) -> list[PrivilegedRequest]:
        matching = [r for r in self._requests.values() if r.status is status]
        matching.sort(key=lambda r: r.created_at)
        return [request.model_copy(deep=True) for request in matching[:limit]]


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class SQLitePrivilegedRequestStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        with _connect(path) as connection:
            connection.executescript(REQUESTS_DDL)

    async def save(self, request: PrivilegedRequest) -> PrivilegedRequest:
        await asyncio.to_thread(self._save, request)
        return request

    def _save(self, request: PrivilegedRequest) -> None:
        with _connect(self._path) as connection:
            connection.execute(
                "INSERT INTO privileged_requests (request_id, requested_by, task_id,"
                " request_text, intent, status, reason, created_at, expires_at,"
                " decided_at, approved_by, result, context)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(request_id) DO UPDATE SET"
                " intent=excluded.intent, status=excluded.status, reason=excluded.reason,"
                " expires_at=excluded.expires_at, decided_at=excluded.decided_at,"
                " approved_by=excluded.approved_by, result=excluded.result",
                (
                    request.request_id,
                    request.requested_by,
                    request.task_id,
                    request.request_text,
                    json.dumps(request.intent.model_dump(mode="json"))
                    if request.intent
                    else None,
                    request.status.value,
                    request.reason,
                    request.created_at.isoformat(),
                    request.expires_at.isoformat() if request.expires_at else None,
                    request.decided_at.isoformat() if request.decided_at else None,
                    request.approved_by,
                    json.dumps(request.result.model_dump(mode="json"))
                    if request.result
                    else None,
                    json.dumps(request.context),
                ),
            )

    async def get(self, request_id: str) -> PrivilegedRequest | None:
        return await asyncio.to_thread(self._get, request_id)

    def _get(self, request_id: str) -> PrivilegedRequest | None:
        with _connect(self._path) as connection:
            row = connection.execute(
                "SELECT * FROM privileged_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return _row_to_request(row) if row else None

    async def list_by_status(
        self, status: RequestStatus, limit: int = 50
    ) -> list[PrivilegedRequest]:
        return await asyncio.to_thread(self._list_by_status, status, limit)

    def _list_by_status(
        self, status: RequestStatus, limit: int
    ) -> list[PrivilegedRequest]:
        with _connect(self._path) as connection:
            rows = connection.execute(
                "SELECT * FROM privileged_requests WHERE status = ?"
                " ORDER BY created_at LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [_row_to_request(row) for row in rows]


def _row_to_request(row: sqlite3.Row) -> PrivilegedRequest:
    return PrivilegedRequest(
        request_id=row["request_id"],
        requested_by=row["requested_by"],
        task_id=row["task_id"],
        request_text=row["request_text"],
        intent=INTENT_ADAPTER.validate_python(json.loads(row["intent"]))
        if row["intent"]
        else None,
        status=RequestStatus(row["status"]),
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        approved_by=row["approved_by"],
        result=ExecutionResult.model_validate(json.loads(row["result"]))
        if row["result"]
        else None,
        context=json.loads(row["context"]),
    )
