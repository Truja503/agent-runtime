"""Minimal SQLite helpers.

Deliberately no ORM. The runtime stores a handful of rows with a handful of
columns; an ORM would add a dependency and hide the schema without buying
anything at this size.

Connections are short-lived and opened per operation. Callers wrap these in
``asyncio.to_thread`` so the event loop is never blocked by disk I/O.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with sane defaults."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_schema(path: Path, ddl: str) -> None:
    with connect(path) as connection:
        connection.executescript(ddl)
