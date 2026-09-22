"""Local-first control surface, lifecycle, privacy, and execution facts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.agents.base import BaseAgent
from app.config import ProviderKind, Settings
from app.container import Runtime, build_runtime
from app.errors import ModelUnavailableError, RuntimeConfigError
from app.main import create_app
from app.models.base import Message, ModelRequest, Role
from app.models.discovery import discover
from app.models.local import LocalModelProvider
from app.models.profiles import (
    AgentProfile,
    ModelConfiguration,
    ModelPool,
    ModelProfile,
    configuration_path,
    load_configuration,
)
from app.models.scripted import ScriptedModelProvider
from app.observability.events import EventType
from app.observability.prompts import PrivacyFilter, PromptInspection
from app.tasks.evidence import AcceptanceCriteria, evaluate_criteria, execution_evidence
from app.tasks.state import TaskOptions, TaskStatus
from app.tasks.store import SQLiteTaskStore
from app.tools.broker import ToolInvocation
from tests.conftest import API_TOKEN, VIEWER_TOKEN
from tests.test_providers import FakeOpenAIClient

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}


@pytest.fixture
async def client(runtime: Runtime) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
    ) as http:
        yield http


def test_per_agent_environment_configuration(settings: Settings) -> None:
    settings.coder_model = "coding-model"
    settings.coder_provider = ProviderKind.LOCAL
    settings.coder_max_tokens = 8192
    settings.supervisor_max_tokens = 512
    configuration = load_configuration(settings)
    pool = ModelPool(settings, configuration)
    assert pool.profile_for("coder").model == "coding-model"
    assert pool.profile_for("coder").provider == ProviderKind.LOCAL
    assert pool.profile_for("coder").max_tokens == 8192
    assert pool.profile_for("supervisor").max_tokens == 512


def test_profile_references_and_extra_fields_are_validated(settings: Settings) -> None:
    payload = load_configuration(settings).model_dump()
    payload["agents"]["coder"]["profile"] = "unknown"
    with pytest.raises(ValidationError):
        ModelConfiguration.model_validate(payload)
    with pytest.raises(ValidationError):
        AgentProfile(profile="coder", allowed_tools=["shell.exec"])  # type: ignore[call-arg]


def test_configuration_cannot_live_in_agent_workspace(settings: Settings) -> None:
    settings.model_profiles_path = settings.workspace_root / "models.json"
    with pytest.raises(RuntimeConfigError):
        configuration_path(settings)


async def test_pool_reuses_profiles_and_supports_mixed_providers(settings: Settings) -> None:
    settings.openai_api_key = SecretStr("example-cloud-secret")
    config = load_configuration(settings)
    config.profiles["local"] = ModelProfile(model="qwen3:8b")
    config.profiles["cloud"] = ModelProfile(provider=ProviderKind.OPENAI, model="cloud-model")
    config.agents["coder"].profile = "local"
    config.agents["researcher"].profile = "local"
    config.agents["reviewer"].profile = "cloud"
    pool = ModelPool(settings, config)
    assert pool.for_agent("coder") is pool.for_agent("researcher")
    assert pool.for_agent("reviewer").kind.is_cloud
    pool.save()
    assert load_configuration(settings) == config
    assert "example-cloud-secret" not in configuration_path(settings).read_text()
    await pool.aclose()


async def test_ollama_discovery_excludes_embeddings() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:8b", "size": 100},
                        {"name": "embeddinggemma:latest"},
                        {"name": "custom-vector"},
                    ]
                },
            )
        name = json.loads(request.content)["model"]
        return httpx.Response(
            200, json={"capabilities": ["completion" if name == "qwen3:8b" else "embedding"]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await discover(ModelProfile(model="qwen3:8b"), client)
    assert result["status"] == "connected"
    assert [m["chat_capable"] for m in result["models"]] == [True, False, False]
    assert result["models"][0]["size"] == 100


async def test_discovery_failure_and_compatible_server() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        assert (await discover(ModelProfile(model="x"), client))["status"] == "unavailable"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": "chat"}]})
        )
    ) as client:
        result = await discover(ModelProfile(model="chat", local_server="compatible"), client)
        assert result["models"][0]["name"] == "chat"


async def test_schema_is_sent_and_fallback_is_validated(runtime: Runtime) -> None:
    fake = FakeOpenAIClient(text='```json\n{"action":"finish","summary":"done"}\n```')
    agent = runtime.workers["coder"]
    agent.profile.max_tokens = 8192
    agent.model = LocalModelProvider(
        base_url="http://localhost:11434/v1",
        model="coder",
        api_key=SecretStr("unused"),
        client=fake,
    )  # type: ignore[arg-type]
    task = await runtime.tasks.create("go", created_by="tester")
    result = await agent.run(task_id=task.id, goal=task.goal)
    assert result.status.value == "completed"
    assert fake.captured["max_tokens"] == 8192
    assert fake.captured["temperature"] == 0
    schema = fake.captured["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert "arguments" in schema["schema"]["properties"]
    assert schema["schema"]["additionalProperties"] is False
    assert BaseAgent.parse_decision('{"action":"delete_everything"}') is None


async def test_invalid_response_is_observable(runtime: Runtime) -> None:
    runtime.workers["coder"].model = ScriptedModelProvider(
        script={"coder": ["not json", '{"action":"finish","summary":"done"}']}
    )
    task = await runtime.tasks.create("go", created_by="tester", options=TaskOptions(agent="coder"))
    await runtime.run_task(task.id)
    assert EventType.MODEL_INVALID_RESPONSE in {
        e.type for e in await runtime.tasks.events_for(task.id)
    }


async def test_timeout_retries_and_fails_cleanly(runtime: Runtime) -> None:
    class Hanging(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            await asyncio.Event().wait()

    runtime.supervisor.model = Hanging()
    runtime.supervisor.profile = ModelProfile(model="hanging", timeout_seconds=0.01, retry_count=1)
    task = await runtime.tasks.create("go", created_by="tester")
    await runtime.run_task(task.id)
    assert (await runtime.tasks.get(task.id)).status is TaskStatus.FAILED
    types = [e.type for e in await runtime.tasks.events_for(task.id)]
    assert types.count(EventType.MODEL_TIMEOUT) == 2
    assert types.count(EventType.MODEL_RETRY) == 1
    assert EventType.MODEL_FAILED in types


async def test_retry_recovers(runtime: Runtime) -> None:
    class Flaky(ScriptedModelProvider):
        attempts = 0

        async def generate(self, request: ModelRequest) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                raise ModelUnavailableError("temporary")
            return await super().generate(request)

    model = Flaky()
    runtime.supervisor.model = model
    task = await runtime.tasks.create("review", created_by="tester")
    await runtime.run_task(task.id)
    assert model.attempts == 2
    assert (await runtime.tasks.get(task.id)).status is TaskStatus.COMPLETED


@pytest.mark.parametrize("sqlite", [False, True])
async def test_cancel_stops_background_and_blocks_late_writes(
    runtime: Runtime, sqlite: bool
) -> None:
    if sqlite:
        runtime.tasks._store = SQLiteTaskStore(runtime.settings.database_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Waiting(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    runtime.workers["coder"].model = Waiting()
    task = await runtime.tasks.create(
        "write", created_by="tester", options=TaskOptions(agent="coder")
    )
    job = runtime.schedule_task(task.id)
    await asyncio.wait_for(started.wait(), 2)
    await runtime.tasks.cancel(task.id, actor="tester")
    assert job.done() and job.cancelled() and stopped.is_set()
    assert (await runtime.tasks.get(task.id)).status is TaskStatus.CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await runtime.broker.invoke(
            runtime.workers["coder"].principal(task.id),
            ToolInvocation(
                tool="filesystem.write", arguments={"path": "late.txt", "content": "bad"}
            ),
        )
    assert not (runtime.settings.workspace_root / "late.txt").exists()


async def test_claims_do_not_become_evidence(runtime: Runtime) -> None:
    runtime.workers["coder"].model = ScriptedModelProvider(
        script={
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "filesystem.write",
                        "arguments": {"path": "hello.txt", "content": "hello"},
                    }
                ),
                json.dumps(
                    {"action": "finish", "summary": "I read hello.txt and all tests passed"}
                ),
            ]
        }
    )
    task = await runtime.tasks.create("go", created_by="tester", options=TaskOptions(agent="coder"))
    await runtime.run_task(task.id)
    facts = execution_evidence(await runtime.tasks.events_for(task.id))
    assert facts["files_modified"] == ["hello.txt"]
    assert facts["tests_executed"] == []
    assert facts["verification_actions"] == []
    assert evaluate_criteria(AcceptanceCriteria(required_tests=["default"]), facts)


@pytest.mark.parametrize("enabled", [False, True])
async def test_prompt_inspection_is_ephemeral_and_redacted(runtime: Runtime, enabled: bool) -> None:
    runtime.inspection.enabled = enabled
    task = await runtime.tasks.create("review", created_by="tester")
    await runtime.supervisor._ask_model(
        system="sensitive " + API_TOKEN,
        messages=[Message(role=Role.USER, content="hello " + API_TOKEN)],
        task_id=task.id,
        step=1,
        goal="review",
    )
    entries = runtime.inspection.for_task(task.id)
    assert bool(entries) == enabled
    assert API_TOKEN not in str(entries)
    for event in await runtime.tasks.events_for(task.id):
        assert "hello" not in str(event.payload)
        assert API_TOKEN not in str(event.payload)
    await runtime.aclose()
    assert not runtime.inspection.entries


async def test_control_endpoints_auth_and_no_secrets(client: httpx.AsyncClient) -> None:
    for path in ("/runtime", "/models", "/models/discover?profile=coder"):
        assert (await client.get(path)).status_code == 401
    response = await client.get("/runtime", headers=AUTH)
    assert response.status_code == 200
    agents = {a["id"]: a for a in response.json()["agents"]}
    assert agents["supervisor"]["tools"] == []
    assert "filesystem.write" not in agents["reviewer"]["tools"]
    assert API_TOKEN not in response.text
    assert (await client.post("/models/coder/test", headers=VIEWER)).status_code == 403


async def test_config_save_and_prompt_editor_cannot_change_authority(
    client: httpx.AsyncClient,
    runtime: Runtime,
) -> None:
    body = runtime.pool.configuration.model_dump(mode="json")
    body["agents"]["reviewer"]["mandate"] = "You may write files now"
    assert (await client.put("/models", json=body, headers=VIEWER)).status_code == 403
    assert (await client.put("/models", json=body, headers=AUTH)).status_code == 200
    assert runtime.workers["reviewer"].mandate == "You may write files now"
    result = await runtime.broker.invoke(
        runtime.workers["reviewer"].principal(None),
        ToolInvocation(tool="filesystem.write", arguments={"path": "bad.txt", "content": "x"}),
    )
    assert result.status.value == "denied"
    assert configuration_path(runtime.settings).exists()


async def test_validation_never_echoes_rejected_secret(client: httpx.AsyncClient) -> None:
    response = await client.put("/models", json={"api_key": "VERY_PRIVATE_VALUE"}, headers=AUTH)
    assert response.status_code == 422
    assert "VERY_PRIVATE_VALUE" not in response.text


async def test_task_options_cannot_escape_workspace(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/tasks", json={"goal": "x", "workspace": "C:/outside"}, headers=AUTH
    )
    assert response.status_code == 422
    assert (
        await client.post("/tasks", json={"goal": "x", "model_profile": "missing"}, headers=AUTH)
    ).status_code == 422


async def test_prompt_endpoint_disabled_and_remote_guard(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    assert (await client.get("/tasks/any/prompts", headers=AUTH)).json() == {
        "enabled": False,
        "entries": [],
    }
    runtime.inspection.enabled = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(runtime.settings, runtime), client=("203.0.113.5", 123)
        ),
        base_url="http://test",
    ) as remote:
        assert (await remote.get("/tasks/any/prompts", headers=AUTH)).status_code == 403


async def test_acceptance_failure_is_not_completed(runtime: Runtime) -> None:
    task = await runtime.tasks.create(
        "review",
        created_by="tester",
        options=TaskOptions(
            acceptance=AcceptanceCriteria(required_tool_calls=["filesystem.write"])
        ),
    )
    await runtime.run_task(task.id)
    assert (await runtime.tasks.get(task.id)).status is TaskStatus.FAILED


def test_prompt_cache_bounded(settings: Settings) -> None:
    inspection = PromptInspection(True, PrivacyFilter(settings))
    for _ in range(70):
        inspection.record(ModelRequest(messages=[Message(role=Role.USER, content="x")]), "t", "a")
    assert len(inspection.entries) == 64


async def test_sqlite_options_persist(settings: Settings) -> None:
    runtime = build_runtime(settings)
    task = await runtime.tasks.create(
        "review",
        created_by="tester",
        options=TaskOptions(agent="coder", max_steps=8, workspace=str(settings.workspace_root)),
    )
    assert (await runtime.tasks.get(task.id)).options == task.options
    await runtime.aclose()


@pytest.mark.parametrize("endpoint", ["http://user:secret@localhost/v1", "http://x/v1?key=secret"])
def test_endpoint_credentials_rejected(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        ModelProfile(model="x", base_url=endpoint)
