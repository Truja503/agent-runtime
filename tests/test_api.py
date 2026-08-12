"""API surface, including what is deliberately absent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.container import Runtime
from app.main import create_app
from privileged.service import PrivilegedRequestService
from tests.conftest import API_TOKEN, OPERATOR_ID, OPERATOR_SECRET, VIEWER_TOKEN

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
VIEWER_AUTH = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
OPERATOR_HEADERS = {"X-Operator-Id": OPERATOR_ID, "X-Operator-Secret": OPERATOR_SECRET}


@pytest.fixture
async def client(settings: Settings, runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def privileged_client(
    settings: Settings, runtime: Runtime
) -> AsyncIterator[httpx.AsyncClient]:
    enabled = settings.model_copy(update={"privileged_api_enabled": True})
    app = create_app(enabled, runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def wait_for_status(
    client: httpx.AsyncClient, task_id: str, targets: set[str], tries: int = 200
) -> dict[str, object]:
    for _ in range(tries):
        response = await client.get(f"/tasks/{task_id}", headers=AUTH)
        body = response.json()
        if body["status"] in targets:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"task stayed in {body['status']!r}")


async def test_health_is_public_and_leaks_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "scripted"
    assert "key" not in str(body).lower()


async def test_endpoints_require_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.post("/tasks", json={"goal": "x"})).status_code == 401
    assert (await client.get("/tasks")).status_code == 401
    assert (await client.get("/auth/whoami")).status_code == 401


async def test_bad_token_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/auth/whoami", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_whoami_never_echoes_the_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/whoami", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"name": "tester", "role": "operator"}
    assert API_TOKEN not in response.text


async def test_viewer_cannot_create_tasks(client: httpx.AsyncClient) -> None:
    response = await client.post("/tasks", json={"goal": "x"}, headers=VIEWER_AUTH)
    assert response.status_code == 403


async def test_create_task_and_read_it_back(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/tasks", json={"goal": "Review this project"}, headers=AUTH
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    body = await wait_for_status(client, task_id, {"completed", "failed"})
    assert body["status"] == "completed"

    events = await client.get(f"/tasks/{task_id}/events", headers=AUTH)
    assert events.status_code == 200
    types = {event["type"] for event in events.json()}
    assert "task_created" in types
    assert "tool_allowed" in types


async def test_unknown_task_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/tasks/does-not-exist", headers=AUTH)).status_code == 404
    assert (
        await client.get("/tasks/does-not-exist/events", headers=AUTH)
    ).status_code == 404


async def test_goal_is_validated(client: httpx.AsyncClient) -> None:
    assert (await client.post("/tasks", json={"goal": ""}, headers=AUTH)).status_code == 422


async def test_tool_listing_shows_metadata(client: httpx.AsyncClient) -> None:
    response = await client.get("/tools", headers=AUTH)
    assert response.status_code == 200
    by_name = {tool["name"]: tool for tool in response.json()}
    assert by_name["filesystem.write"]["risk"] == "medium"
    assert by_name["system.request_privileged_action"]["privileged"] is True
    assert "shell.exec" not in by_name


# --- the privileged surface ----------------------------------------------


async def test_privileged_endpoints_do_not_exist_by_default(
    client: httpx.AsyncClient,
) -> None:
    """Not "forbidden" — absent. There is no route to attack."""
    for path in ("/privileged/requests", "/privileged/requests/abc/approve"):
        response = await client.post(path, json={}, headers=AUTH)
        assert response.status_code == 404


async def test_api_session_cannot_approve_a_privileged_action(
    privileged_client: httpx.AsyncClient, privileged_service: PrivilegedRequestService
) -> None:
    """The headline check: an ordinary API token buys nothing here."""
    request = await privileged_service.create_request(
        requested_by="coder", request_text="restart nginx"
    )
    response = await privileged_client.post(
        f"/privileged/requests/{request.request_id}/approve", headers=AUTH
    )
    # Missing operator headers entirely: the bearer token is not consulted.
    assert response.status_code == 422

    response = await privileged_client.post(
        f"/privileged/requests/{request.request_id}/approve",
        headers={**AUTH, "X-Operator-Id": OPERATOR_ID, "X-Operator-Secret": "guessed"},
    )
    assert response.status_code == 401

    stored = await privileged_service.get_request(request.request_id)
    assert stored is not None
    assert stored.status.value == "awaiting_approval"


async def test_operator_credentials_approve_and_execute(
    privileged_client: httpx.AsyncClient,
    privileged_service: PrivilegedRequestService,
    privileged_runner: object,
) -> None:
    request = await privileged_service.create_request(
        requested_by="coder", request_text="restart redis"
    )
    response = await privileged_client.post(
        f"/privileged/requests/{request.request_id}/approve", headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert privileged_runner.calls == [  # type: ignore[attr-defined]
        ["/usr/bin/systemctl", "restart", "redis"]
    ]


async def test_operator_can_deny(
    privileged_client: httpx.AsyncClient,
    privileged_service: PrivilegedRequestService,
    privileged_runner: object,
) -> None:
    request = await privileged_service.create_request(
        requested_by="coder", request_text="restart postgresql"
    )
    response = await privileged_client.post(
        f"/privileged/requests/{request.request_id}/deny",
        json={"reason": "change freeze"},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert privileged_runner.calls == []  # type: ignore[attr-defined]
