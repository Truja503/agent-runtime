"""Fixed Docker operations. Generated programs never run in the host process namespace."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.errors import ToolExecutionError
from app.tools.browser import CSP


class DockerExecutor:
    def __init__(self, image: str, owner: str = "standalone") -> None:
        self.image = image
        self.owner = owner
        self.binary = shutil.which("docker")
        if not self.binary and os.name == "nt":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs/DockerDesktop/resources/bin/docker.exe",
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "Docker/Docker/resources/bin/docker.exe",
            ]
            self.binary = next((str(path) for path in candidates if path.is_file()), None)
        self.servers: dict[str, tuple[ThreadingHTTPServer, str, threading.Timer]] = {}

    def command(self, arguments: list[str], timeout: int = 120) -> tuple[int, str]:
        if not self.binary:
            raise ToolExecutionError("Docker Desktop is unavailable; no host execution fallback")
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(  # noqa: S603 - only runtime-owned Docker argv
                [self.binary, *arguments],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0,
            )
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() > deadline or output.tell() > 8_000_000:
                    process.kill()
                    process.wait()
                    output.seek(max(0, output.tell() - 4000))
                    diagnostic = output.read(4000).decode("utf-8", errors="replace")
                    raise ToolExecutionError(
                        f"Docker operation exceeded {timeout}s time/output limit: {diagnostic}"
                    )
                time.sleep(0.02)
            output.seek(0)
            data = output.read(8_000_001)
            if len(data) > 8_000_000:
                raise ToolExecutionError("Docker output exceeded limit")
            return process.returncode, data.decode("utf-8", errors="replace")

    async def readiness(self) -> dict[str, Any]:
        if not self.binary or not self.image.startswith("sha256:") or len(self.image) != 71:
            return {
                "status": "NOT READY",
                "reason": "Docker and an operator-built image ID are required",
            }
        code, details = await asyncio.to_thread(self.command, ["image", "inspect", self.image], 15)
        return {
            "status": "READY" if code == 0 else "NOT READY",
            "reason": details[-1000:] if code else None,
        }

    def base(self, project: Path, name: str) -> list[str]:
        if not self.image.startswith("sha256:") or len(self.image) != 71:
            raise ToolExecutionError("operator must configure a local immutable toolchain image ID")
        if "," in str(project):
            raise ToolExecutionError("project path cannot contain Docker mount separators")
        return [
            "run",
            "--rm",
            "--pull=never",
            "--platform=linux/amd64",
            "--name",
            name,
            "--label",
            "agent-runtime.toolchain=" + self.owner,
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=512m",
            "--cpus=1",
            "--init",
            "--log-driver=local",
            "--log-opt=max-size=1m",
            "--log-opt=max-file=2",
            "--workdir=/project",
            "--tmpfs=/tmp:rw,nosuid,size=67108864",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--mount",
            f"type=bind,source={project},target=/project",
        ]

    async def run(
        self, project: Path, operation: str, artifacts: Path | None = None
    ) -> dict[str, Any]:
        commands = {
            "dependencies": ["/usr/local/bin/python", "-I", "/adapter/install.py"],
            "build": ["/usr/local/bin/node", "/project/node_modules/vite/bin/vite.js", "build"],
            "test": ["/project/.venv/bin/python", "-I", "/adapter/test.py"],
        }
        if operation not in commands:
            raise ToolExecutionError("unsupported deterministic operation")
        name = "ar-project-" + uuid.uuid4().hex
        args = self.base(project, name)
        if artifacts:
            if "," in str(artifacts):
                raise ToolExecutionError("invalid artifact mount path")
            args += ["--mount", f"type=bind,source={artifacts},target=/artifacts,readonly"]
        args += [self.image, *commands[operation]]
        execution = asyncio.create_task(
            asyncio.to_thread(self.command, args, 600 if operation == "dependencies" else 120)
        )
        try:
            code, output = await asyncio.shield(execution)
            return {
                "status": "completed" if code == 0 else "failed",
                "exit_code": code,
                "passed": code == 0,
                "output": output[-8000:],
            }
        finally:
            # Cancellation may precede Docker creating the container. Keep removing
            # the runtime-owned name until the bounded CLI operation has settled.
            while True:
                await asyncio.to_thread(self.command, ["rm", "-f", name], 15)
                if execution.done():
                    break
                await asyncio.wait({execution}, timeout=0.2)
            await asyncio.gather(execution, return_exceptions=True)

    async def serve(self, project: Path) -> dict[str, Any]:
        key = str(project)
        if key in self.servers:
            await self.stop(project)
        name = "ar-project-" + uuid.uuid4().hex
        args = self.base(project, name) + [
            "--detach",
            self.image,
            "/project/.venv/bin/python",
            "-I",
            "/adapter/serve.py",
        ]
        args.remove("--rm")  # Keep startup diagnostics until runtime-owned cleanup.
        started = asyncio.create_task(asyncio.to_thread(self.command, args, 20))
        try:
            code, output = await asyncio.shield(started)
            if code:
                raise ToolExecutionError("Flask container startup failed: " + output[-2000:])
            for _ in range(20):
                code, _ = await asyncio.to_thread(
                    self.command,
                    ["exec", name, "/usr/local/bin/python", "-I", "/adapter/relay.py", "/"],
                    10,
                )
                if not code:
                    break
                await asyncio.sleep(0.1)
            else:
                _, diagnostic = await asyncio.to_thread(
                    self.command, ["logs", "--tail", "40", name], 10
                )
                raise ToolExecutionError("Flask startup failed: " + diagnostic[-4000:])
            executor = self
            relay_slots = threading.BoundedSemaphore(4)

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    if not relay_slots.acquire(blocking=False):
                        self.send_error(503, "bounded project relay is busy")
                        return
                    try:
                        self.handle_get()
                    finally:
                        relay_slots.release()

                def handle_get(self) -> None:
                    if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":  # type: ignore[attr-defined]
                        self.send_error(403)
                        return
                    if (
                        len(self.path) > 4096
                        or urlsplit(self.path).scheme
                        or self.path.startswith("//")
                    ):
                        self.send_error(400)
                        return
                    try:
                        code, output = executor.command(
                            [
                                "exec",
                                name,
                                "/usr/local/bin/python",
                                "-I",
                                "/adapter/relay.py",
                                self.path,
                            ],
                            12,
                        )
                        if code:
                            raise ValueError("Flask relay failed")
                        data = json.loads(output)
                        body = base64.b64decode(data["body"], validate=True)
                        self.send_response(int(data["status"]))
                        self.send_header(
                            "Content-Type",
                            str(data.get("content_type") or "application/octet-stream")
                            .replace("\r", "")
                            .replace("\n", ""),
                        )
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Security-Policy", CSP)
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Cache-Control", "no-store")
                        location = data.get("location")
                        if (
                            location
                            and location.startswith("/")
                            and not location.startswith("//")
                            and not any(c in location for c in "\r\n\\")
                        ):
                            self.send_header("Location", location)
                        self.end_headers()
                        if self.command == "GET":
                            self.wfile.write(body)
                    except (ValueError, OSError, ToolExecutionError):
                        self.send_error(502)

                do_HEAD = do_GET

                def log_message(self, format: str, *args: Any) -> None:
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            timer = threading.Timer(180, self.stop_sync, args=(key,))
            timer.daemon = True
            self.servers[key] = (server, name, timer)
            timer.start()
            return {
                "status": "completed",
                "url": f"http://127.0.0.1:{server.server_port}",
                "server": "running",
                "expires_in_seconds": 180,
            }
        except BaseException:
            await asyncio.gather(started, return_exceptions=True)
            await asyncio.to_thread(self.command, ["rm", "-f", name], 15)
            raise

    def stop_sync(self, key: str) -> None:
        record = self.servers.pop(key, None)
        if record:
            server, name, timer = record
            timer.cancel()
            server.shutdown()
            server.server_close()
            self.command(["rm", "-f", name], 15)

    async def stop(self, project: Path) -> dict[str, Any]:
        await asyncio.to_thread(self.stop_sync, str(project))
        return {"status": "completed", "server": "stopped"}

    async def recover(self) -> None:
        if (await self.readiness())["status"] != "READY":
            return
        code, output = await asyncio.to_thread(
            self.command,
            ["ps", "-aq", "--filter", "label=agent-runtime.toolchain=" + self.owner],
            15,
        )
        if code:
            raise ToolExecutionError("could not inspect orphaned project containers")
        for container in output.splitlines():
            if container and all(c in "0123456789abcdef" for c in container):
                await asyncio.to_thread(self.command, ["rm", "-f", container], 15)
