"""Regression proofs for Windows browser execution and long-running workflows."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.container import Runtime
from app.models.base import ModelRequest
from app.models.scripted import ScriptedModelProvider
from app.observability.events import EventType
from app.tasks.evidence import AcceptanceCriteria
from app.tasks.state import TaskOptions, TaskStatus
from app.tools.broker import InvocationStatus, ToolInvocation
from app.tools.browser import BrowserArgs, BrowserTools
from app.tools.filesystem import Workspace
from privileged.schemas import RequestStatus
from tests.conftest import OPERATOR_ID, OPERATOR_SECRET
from tests.test_acceptance import finish, tool
from tests.test_browser import mock_qa, review


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Selector subprocess regression")
def test_windows_selector_asgi_serves_opens_screenshots_and_console(workspace: Workspace) -> None:
    project = workspace.root / "diagnostic"
    project.mkdir()
    (project / "index.html").write_text(
        "<title>Windows diagnostic</title><h1>Local page</h1>"
        '<script>console.error("diagnostic-console")</script>'
    )
    application = FastAPI()
    browser = BrowserTools(workspace)

    @application.get("/diagnostic")
    async def capture() -> dict[str, Any]:
        assert "Selector" in type(asyncio.get_running_loop()).__name__
        result = await browser.capture(BrowserArgs(project="diagnostic"))
        result.pop("_images")
        return result

    async def probe() -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.get(f"http://127.0.0.1:{port}/diagnostic")
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            listener.close()
        assert response.status_code == 200
        report = response.json()
        assert report["browser_ready"] and report["server_stopped"]
        assert report["desktop_rendered"] and report["mobile_rendered"]
        assert report["viewport"] == {
            "desktop": {"width": 1440, "height": 1000},
            "mobile": {"width": 390, "height": 844},
        }
        for preview in report["previews"]:
            assert preview["url"].startswith("http://127.0.0.1:")
            assert preview["title"] == "Windows diagnostic"
            assert "diagnostic-console" in preview["console_errors"]
            assert workspace.resolve(preview["screenshot"]).read_bytes().startswith(b"\x89PNG")
        assert (project / "qa/report.json").is_file()

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(probe())


async def test_readiness_launches_real_chromium(workspace: Workspace) -> None:
    result = await BrowserTools(workspace).readiness()
    assert result["status"] == "READY"
    assert result["playwright_python_installed"] and result["chromium_installed"]
    assert result["launch_test"]
    assert await asyncio.to_thread(Path(result["chromium_executable"]).is_file)
    assert result["error"] is None


async def test_package_present_but_browser_missing(
    runtime: Runtime, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(workspace.root / "missing-browser"))
    (workspace.root / "site").mkdir()
    (workspace.root / "site/index.html").write_text("<h1>Local</h1>")
    readiness = await BrowserTools(workspace).readiness()
    assert readiness["status"] == "UNAVAILABLE"
    assert readiness["playwright_python_installed"]
    assert not readiness["chromium_installed"] and not readiness["launch_test"]
    assert "Executable doesn't exist" in readiness["error"]
    for name in ("browser.preview", "browser.screenshot", "browser.console_errors"):
        result = await runtime.broker.invoke(
            runtime.workers["coder"].principal("missing-browser"),
            ToolInvocation(tool=name, arguments={"project": "site"}),
        )
        assert result.status == InvocationStatus.FAILED
        assert result.output and result.output["error_code"] == "browser_unavailable"
    assert not (workspace.root / "site/qa/report.json").exists()
    assert not await runtime.privileged_service.list_pending()


async def test_browser_package_missing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.tools.browser.async_playwright", None)
    result = await BrowserTools(workspace).readiness()
    assert result["status"] == "UNAVAILABLE"
    assert result["playwright_python_installed"] is False


async def test_launch_error_is_not_replaced_by_install_advice(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenContext:
        async def __aenter__(self) -> Any:
            raise OSError("diagnostic launch failure")

        async def __aexit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.tools.browser.async_playwright", BrokenContext)
    result = await BrowserTools(workspace).readiness()
    assert result["error"] == "OSError: diagnostic launch failure"
    assert not result["launch_test"]


async def test_cancel_waits_for_browser_worker_cleanup(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace.root / "site").mkdir()
    (workspace.root / "site/index.html").write_text("page")
    started, cleaned = threading.Event(), threading.Event()
    browser = BrowserTools(workspace)

    async def slow(project: Path, args: BrowserArgs) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.05)
            cleaned.set()
        return {}

    monkeypatch.setattr(browser, "_capture", slow)
    task = asyncio.create_task(browser.capture(BrowserArgs(project="site")))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert not browser.lock.locked()


async def test_repair_no_writes_is_not_counted_and_receives_failures(runtime: Runtime) -> None:
    calls = mock_qa(runtime)
    spec = runtime.registry.get("browser.screenshot")
    original = spec.handler

    async def with_errors(args: Any) -> dict[str, Any]:
        result = await original(args)
        result["previews"][0]["console_errors"] = ["fixture page error"]
        return result

    runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": with_errors})
    requests: list[ModelRequest] = []

    class Recording(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            requests.append(request)
            return await super().generate(request)

    model = Recording(script={"coder": [finish(), finish()], "reviewer": [review("fail")]})
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix page",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            max_repair_cycles=2,
            acceptance=AcceptanceCriteria(required_files=["site/qa/report.json"]),
        ),
    )
    await runtime.run_task(task.id)
    result = (await runtime.tasks.get(task.id)).result
    assert result and result["real_repair_cycles"] == 0
    assert result["repair_attempts"][-1]["status"] == "NO_REPAIR_PERFORMED"
    assert calls == ["qa"]
    contexts = "\n".join(
        m.content for r in requests if r.metadata["agent"] == "coder" for m in r.messages
    )
    assert "fixture page error" in contexts
    assert "missing file evidence: site/qa/report.json" in contexts
    assert "Fix overflow" in contexts


@pytest.mark.parametrize(
    "repair_path,content",
    [
        ("site/index.html", "unchanged"),
        ("unrelated.txt", "changed"),
        ("site/qa/report.json", "claimed evidence"),
    ],
)
async def test_only_changed_project_source_counts_as_repair(
    runtime: Runtime,
    repair_path: str,
    content: str,
) -> None:
    calls = mock_qa(runtime)
    project = runtime.settings.workspace_root / "site"
    project.mkdir()
    (project / "index.html").write_text("unchanged")
    model = ScriptedModelProvider(
        script={
            "coder": [finish(), tool("filesystem.write", repair_path, content=content), finish()],
            "reviewer": [review("fail")],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix page", created_by="test", options=TaskOptions(visual_project="site")
    )
    await runtime.run_task(task.id)
    result = (await runtime.tasks.get(task.id)).result
    assert result and result["real_repair_cycles"] == 0
    assert result["repair_attempts"][-1]["status"] == "NO_REPAIR_PERFORMED"
    assert calls == ["qa"]


async def test_browser_failure_does_not_consume_real_repair(runtime: Runtime) -> None:
    mock_qa(runtime, fail=True)
    model = ScriptedModelProvider(
        script={
            "coder": [
                finish(),
                tool("filesystem.write", "site/index.html", content="fixed"),
                finish(),
            ],
            "reviewer": [review("fail"), review("fail")],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Fix page", created_by="test", options=TaskOptions(visual_project="site")
    )
    await runtime.run_task(task.id)
    result = (await runtime.tasks.get(task.id)).result
    assert result and result["real_repair_cycles"] == 0
    assert result["workflow"]["stop_reason"] == "browser_infrastructure_failure"
    assert len(result["repair_attempts"]) == 1


async def test_browser_install_request_never_creates_approval(runtime: Runtime) -> None:
    result = await runtime.broker.invoke(
        runtime.workers["coder"].principal("setup"),
        ToolInvocation(
            tool="system.request_privileged_action",
            arguments={"request": "install Playwright Chromium"},
        ),
    )
    assert result.status == InvocationStatus.FAILED
    assert result.reason and "operator" in result.reason
    assert not await runtime.privileged_service.list_pending()


async def test_rejected_request_is_not_pending(runtime: Runtime) -> None:
    result = await runtime.broker.invoke(
        runtime.workers["coder"].principal("invalid"),
        ToolInvocation(
            tool="system.request_privileged_action",
            arguments={"request": "install arbitrary software"},
        ),
    )
    assert result.status == InvocationStatus.FAILED
    assert result.reason == "approval_rejected"
    assert not await runtime.privileged_service.list_pending()


async def test_research_web_coder_qa_reviewer_order(runtime: Runtime) -> None:
    calls = mock_qa(runtime)
    spec = runtime.registry.get("web.search")

    async def public_result(args: Any) -> dict[str, Any]:
        return {"status": "completed", "results": [{"url": "https://gsap.com/docs/v3/"}]}

    runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": public_result})
    model = ScriptedModelProvider(
        script={
            "researcher": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "web.request",
                        "arguments": {"operation": "web.search", "query": "GSAP docs"},
                    }
                ),
                finish(),
            ],
            "coder": [finish()],
            "reviewer": [review("pass")],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Research current official documentation using Web, then build",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            research_required=True,
            web_research_required=True,
        ),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert result.result and result.result["research_status"] == "completed"
    assert calls == ["qa"]
    ordered = [
        (e.actor, e.type)
        for e in await runtime.tasks.events_for(task.id)
        if e.type == EventType.AGENT_STARTED
        or (
            e.type == EventType.TOOL_COMPLETED
            and e.payload.get("tool") in {"web.search", "browser.screenshot"}
        )
    ]
    assert ordered == [
        ("supervisor", EventType.AGENT_STARTED),
        ("researcher", EventType.AGENT_STARTED),
        ("web", EventType.TOOL_COMPLETED),
        ("coder", EventType.AGENT_STARTED),
        ("reviewer", EventType.TOOL_COMPLETED),
        ("reviewer", EventType.AGENT_STARTED),
    ]


async def test_research_only_task_cannot_claim_unexecuted_web_research(runtime: Runtime) -> None:
    runtime.supervisor.model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["researcher"],
                        "plan": "Research only",
                    }
                )
            ]
        }
    )
    runtime.workers["researcher"].model = ScriptedModelProvider(script={"researcher": [finish()]})
    task = await runtime.tasks.create(
        "Research current official Web documentation",
        created_by="test",
        options=TaskOptions(research_required=True, web_research_required=True),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.result and "web_research_unavailable" in result.result["acceptance_failures"]


@pytest.mark.parametrize("degraded", [False, True])
async def test_required_research_cannot_be_skipped(runtime: Runtime, degraded: bool) -> None:
    calls = mock_qa(runtime)
    runtime.supervisor.model = ScriptedModelProvider(
        script={
            "supervisor": [
                json.dumps(
                    {
                        "workers": ["coder", "reviewer"],
                        "plan": "omitted required research",
                        "web_requests": [
                            {"operation": "web.search", "query": "GSAP documentation"}
                        ],
                    }
                )
            ]
        }
    )
    model = ScriptedModelProvider(
        script={
            "researcher": [finish()],
            "coder": [finish()],
            "reviewer": [review("pass")],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Research current official documentation using Web then build",
        created_by="test",
        options=TaskOptions(
            visual_project="site",
            research_required=True,
            web_research_required=True,
            allow_degraded_research=degraded,
        ),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    assert result.result
    expected_research = "degraded" if degraded else "web_research_unavailable"
    assert result.result["research_status"] == expected_research
    assert bool(calls) is degraded
    assert result.status == (TaskStatus.COMPLETED if degraded else TaskStatus.FAILED)
    starts = [
        e.actor
        for e in await runtime.tasks.events_for(task.id)
        if e.type == EventType.AGENT_STARTED
    ]
    assert starts[:2] == ["supervisor", "researcher"]


@pytest.mark.parametrize("outcome", ["executed", "denied", "expired", "missing"])
async def test_approval_resumes_same_workflow(runtime: Runtime, outcome: str) -> None:
    requests: list[ModelRequest] = []

    class Recording(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> Any:
            requests.append(request)
            return await super().generate(request)

    runtime.workers["coder"].model = Recording(
        script={
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "system.request_privileged_action",
                        "arguments": {"request": "restart nginx"},
                    }
                ),
                tool("filesystem.write", "resumed.txt", content="continued"),
                finish(),
            ]
        }
    )
    task = await runtime.tasks.create(
        "Repair", created_by="test", options=TaskOptions(agent="coder")
    )
    job = runtime.schedule_task(task.id)
    for _ in range(200):
        current = await runtime.tasks.get(task.id)
        if current.status == TaskStatus.WAITING_FOR_APPROVAL:
            break
        await asyncio.sleep(0.01)
    assert current.result
    request_id = current.result["pending_approvals"][0]
    await runtime.reconcile_approvals()
    assert (await runtime.tasks.get(task.id)).status == TaskStatus.WAITING_FOR_APPROVAL
    service = runtime.privileged_service
    if outcome == "executed":
        await service.approve_and_execute(
            request_id=request_id, operator_id=OPERATOR_ID, secret=OPERATOR_SECRET
        )
    elif outcome == "denied":
        await service.deny(request_id=request_id, operator_id=OPERATOR_ID, secret=OPERATOR_SECRET)
    elif outcome == "expired":
        record = await service.get_request(request_id)
        assert record
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await service._store.save(record)
    else:
        service._store._requests.pop(request_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(job, 5)
    final = await runtime.tasks.get(task.id)
    assert final.status == TaskStatus.COMPLETED
    assert final.result and final.result["approval_history"][0]["status"] == outcome
    assert (runtime.settings.workspace_root / "resumed.txt").read_text() == "continued"
    contexts = "\n".join(m.content for r in requests for m in r.messages)
    expected = "approval_rejected" if outcome == "denied" else f"approval_{outcome}"
    assert expected in contexts if outcome != "executed" else '"exit_code": 0' in contexts
    events = await runtime.tasks.events_for(task.id)
    assert sum(e.type == EventType.PRIVILEGED_ACTION_REQUESTED for e in events) == 1


@pytest.mark.parametrize(
    "status", ["missing", "expired", "rejected", "executed", "awaiting_approval"]
)
async def test_stale_waiting_reconciliation(runtime: Runtime, status: str) -> None:
    task = await runtime.tasks.create("Old task", created_by="test")
    await runtime.tasks.transition(task.id, TaskStatus.PLANNING)
    await runtime.tasks.transition(task.id, TaskStatus.WAITING_FOR_APPROVAL)
    record = await runtime.privileged_service.create_request(
        requested_by="coder",
        request_text="restart nginx",
        task_id=task.id,
    )
    if status != "missing":
        record.status = RequestStatus(status)
        await runtime.privileged_service._store.save(record)
    await runtime.tasks.record_result(
        task.id,
        {
            "pending_approvals": [record.request_id if status != "missing" else "gone"],
        },
    )
    await runtime.reconcile_approvals()
    final = await runtime.tasks.get(task.id)
    assert (final.status == TaskStatus.WAITING_FOR_APPROVAL) is (status == "awaiting_approval")
    assert final.result and final.result["approvals"][0]["status"] == status
    if status != "awaiting_approval":
        assert final.error and final.error.startswith("approval_")
