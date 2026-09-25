"""Mocked egress tests: no live internet, models, or external services."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from app.api.schemas import TaskResponse
from app.config import ProviderKind, Settings
from app.container import Runtime
from app.main import create_app
from app.models.scripted import ScriptedModelProvider
from app.observability.prompts import PrivacyFilter
from app.policy.permissions import AgentRole, Principal
from app.tasks.evidence import AcceptanceCriteria, evaluate_acceptance, execution_evidence
from app.tasks.state import Task, TaskOptions
from app.tools.broker import ToolInvocation
from app.tools.filesystem import Workspace
from app.tools.web import (
    MAX_RESPONSE,
    Response,
    SearXNGSearchProvider,
    WebBroker,
    WebRequest,
    WebSearchConfiguration,
    load_web_search_configuration,
    save_web_search_configuration,
)
from tests.conftest import API_TOKEN, VIEWER_TOKEN


async def public_dns(host: str) -> list[str]:
    return ["93.184.216.34"]


class FakeInternet:
    def __init__(self, responses: list[Response] | None = None):
        self.responses = responses or [
            Response(200, {"content-type": "text/plain"}, b"public docs")
        ]
        self.calls: list[tuple[str, str, str, str]] = []

    async def send(self, host: str, path: str, address: str, method: str) -> Response:
        self.calls.append((host, path, address, method))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class FakeSearch:
    name = "mock"

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, *, images: bool) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [
            {
                "title": "Docs",
                "url": "https://example.com/docs",
                "snippet": "Public info",
                "source_page_url": "https://example.com/docs",
                "image_url": "https://example.com/photo.png",
                "creator": "Example photographer",
                "width": 1,
                "height": 1,
            }
        ]


def web(
    workspace: Workspace, settings: Settings, internet: FakeInternet, **kwargs: Any
) -> WebBroker:
    return WebBroker(
        workspace=workspace,
        enabled=lambda: True,
        privacy=PrivacyFilter(settings),
        resolver=public_dns,
        sender=internet.send,
        **kwargs,
    )


@pytest.mark.parametrize(
    "operation",
    ["web.search", "web.fetch", "docs.fetch", "assets.search_images", "assets.import_image"],
)
async def test_search_connect_error_is_contained(
    workspace: Workspace,
    privacy: PrivacyFilter,
) -> None:
    class BrokenProvider:
        name = "broken"

        async def search(self, query: str, *, images: bool) -> list[dict[str, Any]]:
            request = httpx.Request("GET", "http://127.0.0.1:8080/search")
            raise httpx.ConnectError("connection failed", request=request)

    broker = WebBroker(
        workspace=workspace,
        enabled=lambda: True,
        privacy=privacy,
        provider=BrokenProvider(),
    )
    result = await broker.execute(WebRequest(operation="web.search", query="Flask SQLAlchemy"))

    assert result["status"] == "denied"
    assert result["sources"] == []
    assert "connection failed" not in result["error"]


async def test_kill_switch_denies_every_web_tool(runtime: Runtime, operation: Any) -> None:
    runtime.settings.internet_access_enabled = False
    result = await runtime.web.execute(
        WebRequest(operation=operation, query="public docs", url="https://example.com")
    )
    assert result["status"] == "denied"
    assert runtime.web_broker.recent[-1]["external_data_sent"] == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.55.1.2",
        "::1",
        "10.1.2.3",
        "172.16.1.2",
        "192.168.1.1",
        "169.254.169.254",
        "fe80::1",
        "0.0.0.0",  # noqa: S104 - blocked destination fixture
        "100.64.0.1",
        "::ffff:127.0.0.1",
        "2002:0a00:0001::",
        "64:ff9b::a00:1",
        "224.0.0.1",
    ],
)
async def test_private_dns_targets_never_reach_transport(
    workspace: Workspace, settings: Settings, address: str
) -> None:
    internet = FakeInternet()
    broker = web(workspace, settings, internet)

    async def resolve(host: str) -> list[str]:
        return [address]

    broker.policy.resolver = resolve
    result = await broker.execute(
        WebRequest(operation="web.fetch", url="https://public-name.example/docs")
    )
    assert result["status"] == "denied"
    assert not internet.calls


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "ftp://example.com",
        "https://localhost",
        "https://host.local",
        "https://user:pass@example.com",
        "https://example.com/?token=x",
        "https://example.com:8443",
    ],
)
async def test_unsafe_urls_denied(workspace: Workspace, settings: Settings, url: str) -> None:
    internet = FakeInternet()
    broker = web(workspace, settings, internet)
    assert (await broker.execute(WebRequest(operation="web.fetch", url=url)))["status"] == "denied"
    assert not internet.calls


async def test_https_pins_validated_address_and_checks_redirect(
    workspace: Workspace, settings: Settings
) -> None:
    internet = FakeInternet([Response(302, {"location": "https://internal.example/admin"}, b"")])
    broker = web(workspace, settings, internet)

    async def resolve(host: str) -> list[str]:
        return ["10.0.0.2"] if host == "internal.example" else ["93.184.216.34"]

    broker.policy.resolver = resolve
    result = await broker.execute(WebRequest(operation="web.fetch", url="https://example.com/docs"))
    assert result["status"] == "denied"
    assert internet.calls == [("example.com", "/docs", "93.184.216.34", "GET")]
    assert broker.recent[-1]["external_data_sent"] == ["https://example.com/docs"]


async def test_docs_are_inert_paginated_and_have_source(
    workspace: Workspace, settings: Settings
) -> None:
    html = (
        "<title>Docs</title><h1>API</h1><p>Text</p><pre>const x = 1</pre>"
        '<a href="/api">link</a><script>bad()</script>'
        "<form>password</form><iframe>evil</iframe>" + "a" * 13000
    )
    broker = web(
        workspace,
        settings,
        FakeInternet([Response(200, {"content-type": "text/html"}, html.encode())]),
    )
    first = await broker.execute(WebRequest(operation="docs.fetch", url="https://example.com/docs"))
    assert first["title"] == "Docs" and first["truncated"]
    assert "# API" in first["content"] and "```\nconst x = 1" in first["content"]
    assert "https://example.com/api" in first["content"]
    assert all(word not in first["content"] for word in ["bad()", "password", "evil", "<script"])
    second = await broker.execute(
        WebRequest(
            operation="docs.fetch", url="https://example.com/docs", offset=first["next_offset"]
        )
    )
    assert second["complete"] and second["content_hash"] == first["content_hash"]
    assert second["sources"] == ["https://example.com/docs"]


@pytest.mark.parametrize(
    "query",
    [
        API_TOKEN,
        "read C:\\Users\\me\\project",
        "/home/me/.env",
        "workspace/src/main.py",
        "api_key=123",
        "-----BEGIN PRIVATE KEY-----",
        "const x = {secret: 1};",
    ],
)
async def test_search_does_not_send_sensitive_input(
    workspace: Workspace, settings: Settings, query: str
) -> None:
    provider = FakeSearch()
    broker = web(workspace, settings, FakeInternet(), provider=provider)
    assert (await broker.execute(WebRequest(operation="web.search", query=query)))[
        "status"
    ] == "denied"
    assert provider.queries == []
    assert broker.recent[-1]["external_data_sent"] == []


async def test_search_only_receives_query_and_retains_sources(
    workspace: Workspace, settings: Settings
) -> None:
    provider = FakeSearch()
    broker = web(workspace, settings, FakeInternet(), provider=provider)
    query = "GSAP ScrollTrigger pinned gallery"
    result = await broker.execute(
        WebRequest(operation="web.search", query=query, allowed_domains=["example.com"])
    )
    assert provider.queries == [query]
    assert result["results"][0]["provider"] == "mock"
    assert result["sources"] == ["https://example.com/docs"]
    assert broker.recent[-1]["external_data_sent"] == [query]
    broker.provider = None
    result = await broker.execute(WebRequest(operation="web.search", query=query))
    assert result["error"] == "search provider not configured"


@pytest.mark.parametrize(
    "mime,body",
    [
        ("image/svg+xml", b"<svg/>"),
        ("text/html", b"<html>"),
        ("image/png", b"MZexecutable"),
        ("application/zip", b"PK1234"),
        pytest.param("image/png", b"x" * (MAX_RESPONSE + 1), id="oversize"),
    ],
)
async def test_unsafe_image_import_never_writes(
    workspace: Workspace, settings: Settings, mime: str, body: bytes
) -> None:
    broker = web(workspace, settings, FakeInternet([Response(200, {"content-type": mime}, body)]))
    assert (
        await broker.execute(
            WebRequest(operation="assets.import_image", url="https://example.com/image")
        )
    )["status"] == "denied"
    assert not (workspace.root / "assets").exists()


async def test_valid_image_import_confined_with_attribution(
    workspace: Workspace, settings: Settings
) -> None:
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6KAAAAABJRU5ErkJggg=="
    )
    broker = web(
        workspace,
        settings,
        FakeInternet([Response(200, {"content-type": "image/png"}, png)]),
        provider=FakeSearch(),
    )
    results = await broker.execute(
        WebRequest(operation="assets.search_images", query="public landscape")
    )
    candidate = results["results"][0]
    assert candidate["license"] is None
    result = await broker.execute(
        WebRequest(operation="assets.import_image", candidate_id=candidate["candidate_id"])
    )
    path = workspace.resolve(result["path"])
    assert path.is_relative_to(workspace.root / "assets" / "imported")
    assert path.read_bytes() == png
    assert result["sha256"] == hashlib.sha256(png).hexdigest()
    metadata = json.loads(workspace.resolve(result["path"] + ".json").read_text())
    assert metadata["creator"] == "Example photographer" and metadata["license"] is None
    assert metadata["source_page_url"] == "https://example.com/docs"


async def test_roles_cannot_expand_authority(runtime: Runtime) -> None:
    for role, tool in [
        (AgentRole.WEB, "filesystem.write"),
        (AgentRole.CODER, "web.fetch"),
        (AgentRole.REVIEWER, "web.fetch"),
        (AgentRole.CODER, "web.request"),
    ]:
        principal = Principal(
            name=role.value,
            role=role,
            model_kind=ProviderKind.LOCAL,
            allowed_tools=frozenset({tool}),
        )
        result = await runtime.broker.invoke(principal, ToolInvocation(tool=tool, arguments={}))
        assert result.status == "denied"
    assert "filesystem.write" not in runtime.web.allowed_tools


async def test_researcher_delegation_and_api_status(runtime: Runtime) -> None:
    invocation = ToolInvocation(
        tool="web.request", arguments={"operation": "web.search", "query": "public docs"}
    )
    task = await runtime.tasks.create("inspect", created_by="test")
    result = await runtime.broker.invoke(
        runtime.workers["researcher"].principal(task.id), invocation
    )
    assert result.status == "denied"
    events = await runtime.tasks.events_for(task.id)
    assert any(e.type.value == "web_egress" and e.actor == "web" for e in events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.settings, runtime)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        state = (await client.get("/runtime")).json()
        assert state["internet"]["enabled"] is False
        assert state["internet"]["recent_denied"]
        assert (
            next(a for a in state["agents"] if a["id"] == "web")["model"]
            == "none (structured executor)"
        )
        response = await client.post(
            "/web/request",
            json=invocation.arguments,
            headers={"Authorization": f"Bearer {VIEWER_TOKEN}"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "criteria,expected",
    [
        (AcceptanceCriteria(), "not_evaluated"),
        (AcceptanceCriteria(required_workers=["coder"]), "accepted"),
        (AcceptanceCriteria(required_files=["missing"]), "rejected"),
    ],
)
def test_acceptance_requires_explicit_rules(criteria: AcceptanceCriteria, expected: str) -> None:
    result = evaluate_acceptance(
        criteria, execution_evidence([]), [{"agent": "coder", "status": "completed"}]
    )
    assert result["status"] == expected


async def test_pass_without_reads_is_unverified(runtime: Runtime) -> None:
    runtime.workers["reviewer"].model = ScriptedModelProvider(
        script={
            "reviewer": [
                json.dumps(
                    {
                        "action": "finish",
                        "summary": "claim",
                        "review": {
                            "verdict": "pass",
                            "summary": "claim",
                            "findings": [],
                            "acceptance_criteria": [],
                        },
                    }
                )
            ]
        }
    )
    task = await runtime.tasks.create(
        "review", created_by="test", options=TaskOptions(agent="reviewer")
    )
    await runtime.run_task(task.id)
    result = (await runtime.tasks.get(task.id)).result
    assert result["review"]["verdict"] == "pass"
    assert result["acceptance"]["status"] == "not_evaluated"
    assert result["acceptance"]["review_inspection"] == "unverified"
    rejected = evaluate_acceptance(
        AcceptanceCriteria(required_reviewer_files=["README.md"]),
        result["evidence"],
        result["worker_results"],
    )
    assert rejected["status"] == "rejected"


def test_legacy_empty_acceptance_projection_does_not_mutate_history() -> None:
    task = Task(
        goal="legacy",
        created_by="test",
        result={"acceptance": {"status": "accepted"}, "evidence": "legacy-invalid"},
    )
    assert TaskResponse.of(task).result["acceptance"]["status"] == "not_evaluated"
    assert task.result["acceptance"]["status"] == "accepted"


async def test_switch_disabled_during_dns_prevents_send(
    workspace: Workspace, settings: Settings
) -> None:
    internet = FakeInternet()
    broker = web(workspace, settings, internet)

    async def disable(host: str) -> list[str]:
        broker.enabled = lambda: False
        return ["93.184.216.34"]

    broker.policy.resolver = disable
    result = await broker.execute(WebRequest(operation="web.fetch", url="https://example.com"))
    assert result["status"] == "denied" and not internet.calls


async def test_redirect_limit_is_bounded(workspace: Workspace, settings: Settings) -> None:
    internet = FakeInternet([Response(302, {"location": "/again"}, b"")])
    result = await web(workspace, settings, internet).execute(
        WebRequest(operation="web.fetch", url="https://example.com/start")
    )
    assert result["status"] == "denied" and len(internet.calls) == 4


def test_tls_uses_pinned_ip_and_original_server_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.web import PinnedHTTPS

    calls: list[Any] = []
    raw = object()

    def connect(address: Any, timeout: Any) -> Any:
        calls.append((address, timeout))
        return raw

    class Context:
        def wrap_socket(self, sock: Any, *, server_hostname: str) -> Any:
            assert sock is raw
            calls.append(server_hostname)
            return raw

    monkeypatch.setattr("app.tools.web.socket.create_connection", connect)
    monkeypatch.setattr("app.tools.web.ssl.create_default_context", Context)
    connection = PinnedHTTPS("example.com", "93.184.216.34")
    connection.connect()
    assert calls == [(("93.184.216.34", 443), 10), "example.com"]


async def test_supervisor_sends_structured_request_not_goal(runtime: Runtime) -> None:
    runtime.settings.internet_access_enabled = True
    provider = FakeSearch()
    runtime.web_broker.provider = provider
    runtime.web_broker.policy.resolver = public_dns
    model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["researcher"],
                        "plan": "inspect",
                        "web_requests": [{"operation": "web.search", "query": "public docs"}],
                    }
                )
            ],
            "researcher": [json.dumps({"action": "finish", "summary": "inspected"})],
        }
    )
    runtime.supervisor.model = model
    runtime.workers["researcher"].model = model
    task = await runtime.tasks.create("Private project details must stay local", created_by="test")
    await runtime.run_task(task.id)
    assert provider.queries == ["public docs"]
    result = (await runtime.tasks.get(task.id)).result
    assert result["web_results"][0]["output"]["sources"] == ["https://example.com/docs"]
    assert any(e.type.value == "web_egress" for e in await runtime.tasks.events_for(task.id))


async def test_prompt_words_do_not_create_hard_research_requirements(runtime: Runtime) -> None:
    model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps({"workers": ["researcher"], "plan": "inspect", "web_requests": []})
            ],
            "researcher": [
                json.dumps({"action": "finish", "summary": "local inspection complete"})
            ],
        }
    )
    runtime.supervisor.model = model
    runtime.workers["researcher"].model = model
    task = await runtime.tasks.create(
        "Do not perform Web research. Build locally without Internet research.",
        created_by="test",
    )
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status.value == "completed"
    assert stored.result["research_status"] == "not_requested"
    assert "web_research_unavailable" not in stored.result.get("routing_failures", [])


async def test_optional_supervisor_web_failure_does_not_block_task(runtime: Runtime) -> None:
    runtime.settings.internet_access_enabled = True
    runtime.web_broker.provider = None
    model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["researcher"],
                        "plan": "inspect",
                        "web_requests": [{"operation": "web.search", "query": "public docs"}],
                    }
                )
            ],
            "researcher": [json.dumps({"action": "finish", "summary": "continued locally"})],
        }
    )
    runtime.supervisor.model = model
    runtime.workers["researcher"].model = model
    task = await runtime.tasks.create("Inspect the project", created_by="test")
    await runtime.run_task(task.id)
    stored = await runtime.tasks.get(task.id)
    assert stored.status.value == "completed"
    assert stored.result["research_status"] == "optional_web_unavailable"
    assert stored.result.get("routing_failures") == []


async def test_searxng_provider_normalizes_web_and_image_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        category = request.url.params.get("categories")
        if category == "images":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Chair",
                            "url": "https://example.com/chair",
                            "img_src": "https://example.com/chair.jpg",
                            "content": "Editorial chair",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Flask",
                        "url": "https://flask.palletsprojects.com/",
                        "content": "Official docs",
                    }
                ]
            },
        )

    provider = SearXNGSearchProvider(
        "http://127.0.0.1:8080",
        transport=httpx.MockTransport(handler),
    )
    web_results = await provider.search("Flask official documentation", images=False)
    assert web_results[0]["url"] == "https://flask.palletsprojects.com/"
    image_results = await provider.search("editorial chair", images=True)
    assert image_results[0]["image_url"] == "https://example.com/chair.jpg"


def test_web_search_configuration_persists_and_rejects_remote_plain_http(tmp_path: Any) -> None:
    path = tmp_path / "web-search.json"
    config = WebSearchConfiguration(
        provider="searxng",
        searxng_base_url="http://127.0.0.1:8080",
    )
    save_web_search_configuration(path, config)
    assert load_web_search_configuration(path) == config
    with pytest.raises(ValueError):
        WebSearchConfiguration(
            provider="searxng",
            searxng_base_url="http://example.com:8080",
        )
