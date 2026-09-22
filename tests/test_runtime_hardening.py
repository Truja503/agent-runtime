"""Regression coverage for routing, recovery, outcomes and cancellation boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.api.control import runtime_state
from app.config import Settings
from app.container import Runtime
from app.main import create_app
from app.models.discovery import discover
from app.models.profiles import ModelPool, ModelProfile, load_configuration
from app.models.scripted import ScriptedModelProvider
from app.observability.events import Event, EventType
from app.tasks.state import RECOVERABLE_STATUSES, Task, TaskOptions, TaskStatus, can_transition
from app.tasks.store import SQLiteTaskStore
from tests.conftest import API_TOKEN


def test_role_step_defaults_and_precedence(tmp_path: Any, monkeypatch: Any) -> None:
    for key in (
        "DEFAULT_MAX_STEPS",
        "SUPERVISOR_MAX_STEPS",
        "RESEARCHER_MAX_STEPS",
        "CODER_MAX_STEPS",
        "REVIEWER_MAX_STEPS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None, model_profiles_path=tmp_path / "models.json")
    pool = ModelPool(settings, load_configuration(settings))
    assert [pool.steps_for(r) for r in pool.configuration.agents] == [4, 10, 16, 10]
    settings.default_max_steps = 7
    assert pool.steps_for("coder") == 7
    settings.coder_max_steps = 11
    assert pool.steps_for("coder") == 11
    pool.configuration.agents["coder"].max_steps = 19
    assert pool.steps_for("coder") == 19


@pytest.mark.parametrize("sqlite", [False, True])
async def test_startup_reconciles_only_inflight_once(runtime: Runtime, sqlite: bool) -> None:
    if sqlite:
        runtime.tasks._store = SQLiteTaskStore(runtime.settings.database_path)
    tasks = []
    for status in TaskStatus:
        task = Task(goal="persisted", status=status, result={"preserved": True})
        await runtime.tasks._store.create(task)
        tasks.append(task)
    await runtime.start()
    for original in tasks:
        task = await runtime.tasks.get(original.id)
        expected = (
            TaskStatus.INTERRUPTED if original.status in RECOVERABLE_STATUSES else original.status
        )
        if original.status == TaskStatus.WAITING_FOR_APPROVAL:
            assert task.status == TaskStatus.FAILED
            assert task.error == "approval_missing"
            assert task.result == {"preserved": True, "approvals": []}
            continue
        assert task.status == expected
        assert task.result == {"preserved": True}
        events = await runtime.tasks.events_for(task.id)
        assert len(events) == int(original.status in RECOVERABLE_STATUSES)
        if events:
            assert events[0].type == EventType.TASK_INTERRUPTED
            assert "restarted" in task.error
    await runtime.start()
    assert await runtime.tasks.reconcile_startup() == 0
    assert not any(can_transition(TaskStatus.INTERRUPTED, target) for target in TaskStatus)


async def test_recovery_processes_more_than_one_page(runtime: Runtime) -> None:
    for _ in range(105):
        await runtime.tasks._store.create(Task(goal="queued"))
    assert await runtime.tasks.reconcile_startup() == 105


async def test_saved_routing_changes_actual_subsequent_requests(runtime: Runtime) -> None:
    # PUT constructs real per-profile providers; the injected fixture model is discarded.
    body = runtime.pool.configuration.model_dump(mode="json")
    expected = {}
    for role in body["agents"]:
        body["profiles"][role]["provider"] = "scripted"
        body["profiles"][role]["model"] = f"{role}-version-two"
        body["agents"][role]["max_steps"] = 8
        expected[role] = f"{role}-version-two"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.put("/models", json=body)
        assert response.status_code == 200
        for role, model in expected.items():
            assert response.json()["effective_agents"][role]["model"] == model
            assert response.json()["effective_agents"][role]["max_steps"] == 8
    for role in expected:
        task = await runtime.tasks.create(
            "review", created_by="test", options=TaskOptions(agent=role)
        )
        await runtime.run_task(task.id)
        requests = [
            e
            for e in await runtime.tasks.events_for(task.id)
            if e.type == EventType.MODEL_REQUEST and e.actor == role
        ]
        assert requests and all(e.payload["model"] == expected[role] for e in requests)
    await runtime.aclose()


async def test_worker_exhaustion_never_reports_supervisor_success(runtime: Runtime) -> None:
    runtime.supervisor.model = ScriptedModelProvider(
        script={"supervisor": [json.dumps({"workers": ["coder"], "plan": "Inspect"})]}
    )
    runtime.workers["coder"].max_steps = 1
    runtime.workers["coder"].model = ScriptedModelProvider(
        script={
            "coder": [
                json.dumps(
                    {"action": "use_tool", "tool": "filesystem.list", "arguments": {"path": "."}}
                )
            ]
        }
    )
    task = await runtime.tasks.create("inspect", created_by="test")
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.result["worker_results"][0]["status"] == "exhausted"
    events = await runtime.tasks.events_for(task.id)
    assert not any(e.actor == "supervisor" and e.type == EventType.AGENT_COMPLETED for e in events)
    assert any(e.actor == "supervisor" and e.type == EventType.AGENT_FAILED for e in events)


@pytest.mark.parametrize("boundary", ["before_start", "dispatch", "after_write"])
async def test_cancel_at_write_boundaries(runtime: Runtime, boundary: str) -> None:
    reached = asyncio.Event()
    original_sink = runtime.events._sinks[0]

    class Gate:
        async def append(self, event: Event) -> None:
            await original_sink.append(event)
            target = EventType.TOOL_ALLOWED if boundary == "dispatch" else EventType.TOOL_COMPLETED
            if event.type == target:
                reached.set()
                await asyncio.Event().wait()

        async def list_for_task(self, task_id: str) -> list[Event]:
            return await original_sink.list_for_task(task_id)

    runtime.events._sinks[0] = Gate()
    runtime.workers["coder"].model = ScriptedModelProvider(
        script={
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "filesystem.write",
                        "arguments": {"path": name, "content": "ok"},
                    }
                )
                for name in ("first.txt", "second.txt")
            ]
        }
    )
    task = await runtime.tasks.create(
        "write", created_by="test", options=TaskOptions(agent="coder")
    )
    if boundary == "before_start":
        await runtime._execution_lock.acquire()
    job = runtime.schedule_task(task.id)
    if boundary != "before_start":
        await asyncio.wait_for(reached.wait(), 2)
    await runtime.tasks.cancel(task.id, actor="test")
    if boundary == "before_start":
        runtime._execution_lock.release()
    assert job.done()
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.CANCELLED
    assert (runtime.settings.workspace_root / "first.txt").exists() == (boundary == "after_write")
    assert not (runtime.settings.workspace_root / "second.txt").exists()
    assert not any(
        e.type == EventType.TASK_COMPLETED for e in await runtime.tasks.events_for(task.id)
    )


async def test_old_success_graph_returns_to_idle(runtime: Runtime) -> None:
    task = await runtime.tasks.create("review", created_by="test")
    await runtime.run_task(task.id)
    task = await runtime.tasks.get(task.id)
    task.updated_at = datetime.now(UTC) - timedelta(minutes=1)
    await runtime.tasks._store.update(task)
    state = await runtime_state(runtime)
    assert all(agent["state"] == "idle" for agent in state["agents"])


async def test_discovery_unknown_capabilities_do_not_invent_chat_support() -> None:
    paths = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"models": [{"name": "unknown"}]} if request.url.path == "/api/tags" else {}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await discover(ModelProfile(model="unknown"), client)
    assert result["models"][0]["chat_capable"] is None
    assert paths == ["/api/tags", "/api/show"]


async def test_effective_override_and_step_budget_do_not_mutate_defaults(runtime: Runtime) -> None:
    from app.models.base import ModelRequest

    started = asyncio.Event()

    class Waiting(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            started.set()
            await asyncio.Event().wait()

    runtime.pool._injected = Waiting(model="override-model")
    runtime.pool.configuration.profiles["override"] = ModelProfile(model="override-model")
    configured = runtime.workers["coder"].model.model
    configured_steps = runtime.workers["coder"].max_steps
    task = await runtime.tasks.create(
        "wait",
        created_by="test",
        options=TaskOptions(agent="coder", model_profile="override", max_steps=3),
    )
    runtime.schedule_task(task.id)
    await asyncio.wait_for(started.wait(), 2)
    state = await runtime_state(runtime)
    coder = next(a for a in state["agents"] if a["id"] == "coder")
    assert coder["model"] == "override-model"
    assert coder["configured_model"] == configured
    assert coder["profile"] == "override" and coder["max_steps"] == 3
    await runtime.tasks.cancel(task.id, actor="test")
    assert runtime.workers["coder"].max_steps == configured_steps
    assert runtime.workers["coder"].model.model == configured


async def test_truncated_response_is_not_executed(runtime: Runtime) -> None:
    from pydantic import SecretStr

    from app.models.local import LocalModelProvider
    from tests.test_providers import FakeOpenAIClient

    fake = FakeOpenAIClient(text='{"action":"finish","summary":"done"}')
    original_create = fake.chat.completions.create

    async def truncated(**kwargs: Any) -> Any:
        result = await original_create(**kwargs)
        result.choices[0].finish_reason = "length"
        return result

    fake.chat.completions.create = truncated
    runtime.workers["coder"].model = LocalModelProvider(
        base_url="http://localhost/v1", model="test", api_key=SecretStr("unused"), client=fake
    )
    task = await runtime.tasks.create("go", created_by="test", options=TaskOptions(agent="coder"))
    await runtime.run_task(task.id)
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.FAILED
    events = await runtime.tasks.events_for(task.id)
    assert any(
        e.type == EventType.MODEL_INVALID_RESPONSE and e.payload["reason"] == "output_limit"
        for e in events
    )
    assert not any(e.type == EventType.TOOL_REQUESTED for e in events)


def test_numeric_usage_remains_visible_without_exposing_tokens() -> None:
    from app.observability.events import redact

    assert redact({"input_tokens": 123, "output_tokens": 45, "api_token": "secret"}) == {
        "input_tokens": 123,
        "output_tokens": 45,
        "api_token": "[redacted]",
    }
    assert redact({"input_tokens": "secret"}) == {"input_tokens": "[redacted]"}
