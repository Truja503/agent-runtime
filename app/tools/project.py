"""Controlled project lifecycle and durable, operator-owned dependency approvals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.errors import ToolExecutionError
from app.privileged_bridge import PrivilegedTicket
from app.storage import apply_schema, connect
from app.tools.broker import CURRENT_TOOL_TASK
from app.tools.filesystem import Workspace
from app.tools.project_downloads import download_manifest
from app.tools.project_executor import DockerExecutor
from app.tools.project_manifest import NPM_PACKAGES, PYTHON_PACKAGES, ProjectArgs, read_manifest


class ProjectToolchain:
    def __init__(self, workspace: Workspace, database: Path, image: str = "") -> None:
        self.workspace, self.database = workspace, database.resolve()
        if self.database.is_relative_to(workspace.root):
            raise ToolExecutionError("toolchain approval storage must be outside model workspace")
        self.executor = DockerExecutor(
            image, hashlib.sha256(str(workspace.root).encode()).hexdigest()[:16]
        )
        self.installing: dict[str, asyncio.Task[Any]] = {}
        self.owners: dict[str, str | None] = {}
        self.lock = asyncio.Lock()
        apply_schema(
            self.database,
            "CREATE TABLE IF NOT EXISTS toolchain (key TEXT PRIMARY KEY, data TEXT NOT NULL);",
        )

    def get(self, key: str) -> dict[str, Any]:
        with connect(self.database) as connection:
            row = connection.execute("SELECT data FROM toolchain WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self, key: str, data: dict[str, Any]) -> None:
        with connect(self.database) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO toolchain(key,data) VALUES (?,?)", (key, json.dumps(data))
            )

    def requests(self, *, all_requests: bool = False) -> list[dict[str, Any]]:
        with connect(self.database) as connection:
            rows = connection.execute(
                "SELECT data FROM toolchain WHERE key LIKE 'dep-%'"
            ).fetchall()
        result = [json.loads(row[0]) for row in rows]
        return result if all_requests else result[-100:]

    def resolve(self, project: str) -> tuple[Path, Any, str]:
        root, manifest = read_manifest(self.workspace, project)
        runtime = Path(__file__).resolve().parents[2]
        if root == runtime or runtime.is_relative_to(root):
            raise ToolExecutionError("runtime cannot be a generated project")
        for protected in ("app", "privileged", "toolchain", ".git", "data", ".venv-control"):
            if root.is_relative_to(runtime / protected):
                raise ToolExecutionError("project overlaps protected runtime files")
        for directory in (root, root / ".venv", root / "node_modules"):
            if directory.is_symlink() or (
                hasattr(directory, "is_junction") and directory.is_junction()
            ):
                raise ToolExecutionError("project environments cannot be links or junctions")
        digest = hashlib.sha256(
            (manifest.model_dump_json() + self.executor.image).encode()
        ).hexdigest()
        return root, manifest, digest

    async def inspect(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ProjectArgs)
        root, manifest, digest = self.resolve(args.project)
        state = self.get("project:" + str(root))
        readiness = await self.executor.readiness()
        return {
            "project": args.project,
            "framework": manifest.framework,
            "dependencies": {"python": manifest.python, "npm": manifest.npm},
            "routes": manifest.routes,
            "executor": readiness,
            "environment": "READY"
            if state.get("installed") == digest and readiness["status"] == "READY"
            else "NOT READY",
            "build": state.get("build", "not_run"),
            "test": state.get("test", "not_run"),
            "server": "running" if str(root) in self.executor.servers else "stopped",
            "dependency_approval": state.get("request_id"),
        }

    async def dependencies(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ProjectArgs)
        root, manifest, digest = self.resolve(args.project)
        key = "project:" + str(root)
        state = self.get(key)
        if state.get("installed") == digest:
            return {"status": "completed", "project": args.project, "environment": "READY"}
        request_id = state.get("request_id", "")
        request = self.get(request_id)
        if request.get("digest") != digest or request.get("status") != "awaiting_approval":
            request_id = "dep-" + uuid.uuid4().hex
            request = {
                "request_id": request_id,
                "status": "awaiting_approval",
                "project": args.project,
                "digest": digest,
                "task_id": CURRENT_TOOL_TASK.get(),
                "manifest": manifest.model_dump(),
                "additional_python": sorted(set(manifest.python) - PYTHON_PACKAGES),
                "additional_npm": sorted(set(manifest.npm) - NPM_PACKAGES),
            }
            self.put(request_id, request)
            self.put(key, {**state, "request_id": request_id})
        return {**request, "status": "approval_required"}

    async def ticket(self, request_id: str) -> PrivilegedTicket | None:
        request = self.get(request_id)
        if not request:
            return None
        return PrivilegedTicket(
            request_id=request_id,
            status=request["status"],
            reason=request.get("reason"),
            result=request.get("result"),
        )

    async def decide(
        self, request_id: str, operator: str, approve: bool, allow_additional: bool = False
    ) -> dict[str, Any]:
        async with self.lock:
            request = self.get(request_id)
            if request.get("status") != "awaiting_approval":
                raise ToolExecutionError("dependency request is not pending")
            if not approve:
                request.update(status="denied", approved_by=operator)
                self.put(request_id, request)
                return request
            root, manifest, digest = self.resolve(request["project"])
            if request["digest"] != digest:
                raise ToolExecutionError("manifest changed; request fresh approval")
            if not allow_additional:
                manifest.check_allowed()
            current = asyncio.current_task()
            assert current
            self.installing[request_id] = current
            request.update(approved_by=operator, installing=True)
            self.put(request_id, request)
            key = "project:" + str(root)
            self.put(key, {**self.get(key), "installed": None})
            try:
                readiness = await self.executor.readiness()
                if readiness["status"] != "READY":
                    raise ToolExecutionError(str(readiness["reason"]))
                with tempfile.TemporaryDirectory(prefix="ar-dependencies-") as directory:
                    target = Path(directory)
                    async with asyncio.timeout(600):
                        artifacts = await download_manifest(manifest, target)
                    (target / "artifacts.json").write_text(json.dumps(artifacts), encoding="utf-8")
                    # Recheck after the potentially slow download, before touching the environment.
                    if self.resolve(request["project"])[2] != digest:
                        raise ToolExecutionError("manifest changed during approval execution")
                    result = await self.executor.run(root, "dependencies", target)
                    request.update(
                        status="executed" if result["passed"] else "failed", result=result
                    )
                    if result["passed"]:
                        self.put(
                            key, {**self.get(key), "installed": digest, "artifacts": artifacts}
                        )
            except asyncio.CancelledError:
                request.update(status="failed", reason="dependency installation cancelled")
                raise
            except Exception as exc:
                request.update(status="failed", reason=f"{type(exc).__name__}: {exc}"[:2000])
            finally:
                request["installing"] = False
                self.put(request_id, request)
                self.installing.pop(request_id, None)
            return request

    async def operation(self, operation: str, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ProjectArgs)
        if operation == "stop":
            return await self.executor.stop(self.workspace.resolve(args.project))
        root, manifest, digest = self.resolve(args.project)
        key = "project:" + str(root)
        if self.get(key).get("installed") != digest:
            raise ToolExecutionError("project environment NOT READY; request approved dependencies")
        if operation == "serve":
            self.owners[str(root)] = CURRENT_TOOL_TASK.get()
            return await self.executor.serve(root)
        if operation == "build" and manifest.frontend == "none":
            result = {
                "status": "completed",
                "passed": True,
                "output": "Flask/Jinja needs no asset build",
            }
        else:
            result = await self.executor.run(root, operation)
        self.put(key, {**self.get(key), operation: result})
        return {"project": args.project, **result}

    async def stop_task(self, task_id: str | None) -> None:
        for request_id, job in list(self.installing.items()):
            if task_id is None or self.get(request_id).get("task_id") == task_id:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        for project, owner in list(self.owners.items()):
            if task_id is None or owner == task_id:
                await self.executor.stop(Path(project))
                self.owners.pop(project, None)

    async def recover(self) -> None:
        for request in self.requests(all_requests=True):
            if request.get("installing"):
                request.update(
                    status="failed",
                    installing=False,
                    reason="runtime restarted during installation",
                )
                self.put(request["request_id"], request)
        await self.executor.recover()
