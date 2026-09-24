"""Authority checks and deterministic adapters; Docker proof is explicitly opt-in."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.container import Runtime
from app.errors import ToolExecutionError
from app.main import create_app
from app.models.scripted import ScriptedModelProvider
from app.tasks.state import TaskOptions
from app.tools.broker import ToolInvocation
from app.tools.project_executor import DockerExecutor
from app.tools.project_manifest import ProjectArgs
from tests.test_acceptance import finish, tool
from tests.test_api import AUTH, OPERATOR_HEADERS
from tests.test_browser import mock_qa, review


def project(runtime: Runtime, *, extra: bool = False) -> ProjectArgs:
    root = runtime.settings.workspace_root / "site"
    root.mkdir()
    (root / "project.json").write_text(
        json.dumps({"python": {"Flask": "3.1.2", **({"other-package": "1.0.0"} if extra else {})}})
    )
    return ProjectArgs(project="site")


def fake_install(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def ready() -> dict[str, Any]:
        return {"status": "READY"}

    async def download(*args: Any) -> list[Any]:
        calls.append("download")
        return []

    async def run(*args: Any) -> dict[str, Any]:
        calls.append(args[1])
        return {"status": "completed", "passed": True, "exit_code": 0}

    monkeypatch.setattr(runtime.projects.executor, "readiness", ready)
    monkeypatch.setattr(runtime.projects.executor, "run", run)
    monkeypatch.setattr("app.tools.project.download_manifest", download)
    return calls


async def test_dependency_request_cannot_install_without_operator(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = project(runtime)
    calls = fake_install(runtime, monkeypatch)
    result = await runtime.broker.invoke(
        runtime.workers["coder"].principal("task"),
        ToolInvocation(tool="project.dependencies", arguments=args.model_dump()),
    )
    assert result.status == "approval_required" and not calls
    assert result.request_id
    app = create_app(runtime.settings, runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = f"/project-toolchain/approvals/{result.request_id}"
        assert (await client.post(url, headers=AUTH, json={"approve": True})).status_code == 422
        assert not calls
        response = await client.post(
            url, headers={**AUTH, **OPERATOR_HEADERS}, json={"approve": True}
        )
        assert response.status_code == 200 and response.json()["status"] == "executed"
    assert calls == ["download", "dependencies"]
    assert (await runtime.projects.inspect(args))["environment"] == "READY"
    assert (await runtime.projects.dependencies(args))["status"] == "completed"
    assert calls == ["download", "dependencies"]


async def test_unknown_requires_explicit_additional_approval(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = project(runtime, extra=True)
    calls = fake_install(runtime, monkeypatch)
    request = await runtime.projects.dependencies(args)
    with pytest.raises(ToolExecutionError, match="allowlist"):
        await runtime.projects.decide(request["request_id"], "operator", True)
    assert not calls
    await runtime.projects.decide(request["request_id"], "operator", True, True)
    assert calls == ["download", "dependencies"]


async def test_manifest_change_invalidates_approval(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = project(runtime)
    calls = fake_install(runtime, monkeypatch)
    request = await runtime.projects.dependencies(args)
    (runtime.settings.workspace_root / "site/project.json").write_text(
        '{"python":{"Flask":"3.1.1"}}'
    )
    with pytest.raises(ToolExecutionError, match="changed"):
        await runtime.projects.decide(request["request_id"], "operator", True)
    assert not calls


async def test_reviewer_cannot_install_dependencies(runtime: Runtime) -> None:
    args = project(runtime)
    result = await runtime.broker.invoke(
        runtime.workers["reviewer"].principal("task"),
        ToolInvocation(tool="project.dependencies", arguments=args.model_dump()),
    )
    assert result.status == "denied"
    assert runtime.projects.requests() == []


async def test_project_scope_is_project_rooted_and_still_confined(runtime: Runtime) -> None:
    runtime.broker.project_scopes["task"] = "site"

    listed = await runtime.broker.invoke(
        runtime.workers["coder"].principal("task"),
        ToolInvocation(tool="filesystem.list", arguments={"path": "."}),
    )
    assert listed.status == "completed"
    assert listed.output and listed.output["path"] == "site"

    written = await runtime.broker.invoke(
        runtime.workers["coder"].principal("task"),
        ToolInvocation(
            tool="filesystem.write",
            arguments={"path": "app/main.py", "content": "safe"},
        ),
    )
    assert written.status == "completed"
    assert (runtime.settings.workspace_root / "site/app/main.py").read_text() == "safe"
    assert not (runtime.settings.workspace_root / "app/main.py").exists()

    for path in (
        "../app/main.py",
        "site/../app/main.py",
        "/tmp/main.py",
        "site/.venv/bin/python",
        "site/node_modules/vite/bin/vite.js",
    ):
        result = await runtime.broker.invoke(
            runtime.workers["coder"].principal("task"),
            ToolInvocation(tool="filesystem.write", arguments={"path": path, "content": "bad"}),
        )
        assert result.status == "denied"

    result = await runtime.broker.invoke(
        runtime.workers["coder"].principal("task"),
        ToolInvocation(tool="tests.run", arguments={"suite": "default"}),
    )
    assert result.status == "denied" and "project.test" in (result.reason or "")


async def test_fixed_docker_argv_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = DockerExecutor("sha256:" + "a" * 64)
    recorded: list[list[str]] = []

    def command(args: list[str], timeout: int = 120) -> tuple[int, str]:
        recorded.append(args)
        return 0, "passed"

    monkeypatch.setattr(executor, "command", command)
    for operation in ("dependencies", "build", "test"):
        result = await executor.run(
            tmp_path, operation, tmp_path if operation == "dependencies" else None
        )
        assert result["passed"]
        run, cleanup = recorded[-2:]
        assert "--network=none" in run and "--read-only" in run
        assert "--cap-drop=ALL" in run and "--pull=never" in run
        assert "--user=65532:65532" in run
        assert cleanup[:2] == ["rm", "-f"]
        assert "shell" not in run and "npm run" not in run
    assert "/project/node_modules/vite/bin/vite.js" in recorded[2]
    assert "/adapter/test.py" in recorded[4]
    with pytest.raises(ToolExecutionError):
        await executor.run(tmp_path, "arbitrary command")


async def test_install_cancellation_clears_pending(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = project(runtime)
    fake_install(runtime, monkeypatch)
    started = asyncio.Event()

    async def hanging(*args: Any) -> list[Any]:
        started.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr("app.tools.project.download_manifest", hanging)
    request = await runtime.projects.dependencies(args)
    job = asyncio.create_task(runtime.projects.decide(request["request_id"], "operator", True))
    await asyncio.wait_for(started.wait(), 2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    ticket = await runtime.projects.ticket(request["request_id"])
    assert ticket and ticket.status == "failed"
    assert not runtime.projects.installing


async def test_cancel_before_container_creation_still_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = DockerExecutor("sha256:" + "a" * 64)
    started, created, removed = threading.Event(), threading.Event(), threading.Event()
    cleanups: list[bool] = []

    def command(args: list[str], timeout: int = 120) -> tuple[int, str]:
        if args[0] == "run":
            started.set()
            assert created.wait(3)
            assert removed.wait(3)
            return 137, "cancelled"
        cleanups.append(created.is_set())
        if created.is_set():
            removed.set()
        else:
            created.set()  # Simulate creation just after the first failed removal.
        return 0, ""

    monkeypatch.setattr(executor, "command", command)
    job = asyncio.create_task(executor.run(tmp_path, "test"))
    assert await asyncio.to_thread(started.wait, 3)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    assert cleanups[0] is False and True in cleanups
    assert removed.is_set()


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_build_failure_becomes_repair_context(
    runtime: Runtime, repair_succeeds: bool
) -> None:
    calls = mock_qa(runtime)
    root = runtime.settings.workspace_root / "site/qa"
    root.mkdir(parents=True)
    (root / "report.json").write_text("{}")
    builds = []

    async def dependencies(args: Any) -> dict[str, Any]:
        return {"status": "completed", "environment": "READY"}

    async def build(args: Any) -> dict[str, Any]:
        builds.append("build")
        success = repair_succeeds and len(builds) > 1
        return {
            "status": "completed" if success else "failed",
            "passed": success,
            "output": "built" if success else "Fix asset import",
        }

    async def test(args: Any) -> dict[str, Any]:
        return {"status": "completed", "passed": True}

    for name, handler in (("dependencies", dependencies), ("build", build), ("test", test)):
        spec = runtime.registry.get("project." + name)
        runtime.registry._tools[spec.name] = spec.model_copy(update={"handler": handler})
    model = ScriptedModelProvider(
        script={
            "coder": [
                item
                for revision in range(3)
                for item in (
                    tool("filesystem.write", "site/app.py", content=f"revision {revision}"),
                    tool("filesystem.read", "site/app.py"),
                    finish(),
                )
            ],
            "reviewer": [
                item
                for verdict in (("fail", "pass", "pass") if repair_succeeds else ("fail",) * 3)
                for item in (
                    tool("filesystem.read", "site/app.py"),
                    tool("filesystem.read", "site/qa/report.json"),
                    review(verdict),
                )
            ],
        }
    )
    for worker in runtime.workers.values():
        worker.model = model
    task = await runtime.tasks.create(
        "Repair Flask assets",
        created_by="test",
        options=TaskOptions(visual_project="site", project_framework="flask"),
    )
    await runtime.run_task(task.id)
    result = await runtime.tasks.get(task.id)
    if not repair_succeeds:
        assert result.status == "failed"
        assert result.result and result.result["real_repair_cycles"] == 2
        assert result.result["workflow"]["stop_reason"] == "repair_limit_reached"
        assert len(builds) == 3 and calls == []
        assert result.result["acceptance"]["status"] == "rejected"
        return
    assert result.status == "completed", result.result
    assert result.result and result.result["real_repair_cycles"] == 1
    assert len(builds) == 2 and calls == ["qa"]
    assert not result.result["repair_attempts"][0]["browser_QA_performed"]
    assert result.result["acceptance"]["status"] == "accepted"
