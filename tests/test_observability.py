"""Events must record what happened without recording secrets."""

from __future__ import annotations

from pathlib import Path

from app.observability.events import EventBus, EventType, redact
from app.observability.logging import scrub
from app.observability.store import InMemoryEventStore, SQLiteEventStore


def test_sensitive_keys_are_redacted() -> None:
    payload = redact(
        {
            "api_key": "sk-abcdef123456",
            "Authorization": "Bearer abcdef",
            "nested": {"password": "hunter2", "safe": "value"},
            "tokens": ["t1", "t2"],
        }
    )
    assert payload["api_key"] == "[redacted]"
    assert payload["Authorization"] == "[redacted]"
    assert payload["nested"]["password"] == "[redacted]"
    assert payload["nested"]["safe"] == "value"
    assert payload["tokens"] == "[redacted]"


def test_long_values_are_truncated() -> None:
    long_prompt = "x" * 5000
    redacted = redact({"content": long_prompt})["content"]
    assert len(redacted) < 400
    assert "chars]" in redacted


async def test_event_bus_redacts_before_storing() -> None:
    sink = InMemoryEventStore()
    bus = EventBus([sink])
    await bus.emit(
        EventType.MODEL_REQUEST,
        task_id="t-1",
        actor="coder",
        api_key="sk-should-never-appear",
    )
    stored = sink.events[0]
    assert stored.payload["api_key"] == "[redacted]"
    assert "sk-should-never-appear" not in str(stored.model_dump())


def test_log_scrubber_catches_credential_shapes() -> None:
    assert "sk-" not in scrub("using key sk-abcdefghijklmnopqrstuvwxyz")
    assert "ghp_" not in scrub("token ghp_abcdefghijklmnopqrstuvwxyz1234")
    assert "redacted" in scrub("Authorization: Bearer abcdefghijklmnop")


async def test_sqlite_event_store_round_trip(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    bus = EventBus([store])
    await bus.emit(EventType.TASK_CREATED, task_id="t-1", actor="tester", goal="hello")
    await bus.emit(EventType.TASK_COMPLETED, task_id="t-1", actor="tester")
    await bus.emit(EventType.TASK_CREATED, task_id="t-2", actor="tester")

    events = await store.list_for_task("t-1")
    assert [event.type for event in events] == [
        EventType.TASK_CREATED,
        EventType.TASK_COMPLETED,
    ]
    assert events[0].payload["goal"] == "hello"


async def test_prompts_are_never_stored_verbatim(runtime: object) -> None:
    """Model events record shape and usage, not content."""
    task = await runtime.tasks.create("a goal with sensitive detail", created_by="t")  # type: ignore[attr-defined]
    await runtime.run_task(task.id)  # type: ignore[attr-defined]
    events = await runtime.tasks.events_for(task.id)  # type: ignore[attr-defined]

    model_events = [e for e in events if e.type is EventType.MODEL_REQUEST]
    assert model_events
    for event in model_events:
        assert "prompt" not in event.payload
        assert "messages" not in event.payload
        assert set(event.payload) <= {
            "provider",
            "model",
            "step",
            "prompt_chars",
            "prompt_hash",
            "phase_id",
            "phase_index",
            "phase_title",
            "phase_attempt",
            "phase_count",
        }
