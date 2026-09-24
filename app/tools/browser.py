"""Fixed local rendering checks, never model-controlled browser automation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import sys
import threading
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

try:
    from playwright.async_api import Route, async_playwright
except ImportError:  # Operator setup can be incomplete; keep the console available.
    async_playwright = None  # type: ignore[assignment]
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.errors import ToolExecutionError
from app.tools.filesystem import Workspace
from app.tools.project_manifest import ProjectArgs, read_manifest, route_path

VIEWPORTS = {"desktop": {"width": 1440, "height": 1000}, "mobile": {"width": 390, "height": 844}}
STATIC_EXTENSIONS = {
    ".html",
    ".htm",
    ".js",
    ".mjs",
    ".css",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".avif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
}
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none'; "
    "form-action 'none'; base-uri 'self'"
)


class BrowserArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=1, max_length=1024)
    viewport: Literal["desktop", "mobile", "both"] = "both"
    routes: list[str] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("routes")
    @classmethod
    def validate_routes(cls, routes: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(route_path(p) for p in routes)) if routes else routes


def served_file(project: Path, request_path: str, prefix: str) -> Path | None:
    """No directory listings, hidden files, QA artifacts, traversal or escaping symlinks."""
    path = unquote(urlsplit(request_path).path)
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :] or "index.html"
    parts = relative.replace("\\", "/").split("/")
    if any(p.startswith(".") or p == "qa" or ":" in p for p in parts):
        return None
    target = (project / relative).resolve()
    if (
        not target.is_relative_to(project)
        or not target.is_file()
        or target.suffix.lower() not in STATIC_EXTENSIONS
        or target.stat().st_size > 5_000_000
    ):
        return None
    return target


@asynccontextmanager
async def local_server(project: Path) -> AsyncIterator[str]:
    prefix = "/"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            expected = f"127.0.0.1:{self.server.server_port}"  # type: ignore[attr-defined]
            if self.headers.get("Host") != expected:
                self.send_error(403)
                return
            try:
                target = served_file(project, self.path, prefix)
                if target is None and self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if target is None:
                    self.send_error(404)
                    return
                with target.open("rb") as source:
                    body = source.read(5_000_001)
                if len(body) > 5_000_000:
                    self.send_error(413)
                    return
                self.send_response(200)
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-DNS-Prefetch-Control", "off")
                self.send_header(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=()"
                )
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (OSError, ValueError):
                self.send_error(404)

        do_HEAD = do_GET

        def do_POST(self) -> None:
            self.send_error(405)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{prefix}"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.to_thread(thread.join, 2)


def allowed_request(url: str, method: str, origin_url: str) -> bool:
    parsed, origin = urlsplit(url), urlsplit(origin_url)
    return (
        method in {"GET", "HEAD"}
        and parsed.scheme == "http"
        and parsed.netloc == origin.netloc
        and parsed.path.startswith(origin.path)
        and not parsed.username
        and not parsed.password
    )


class Observations:
    def __init__(self, url: str) -> None:
        self.url = url
        self.errors: list[str] = []
        self.failed: list[dict[str, Any]] = []
        self.requests = 0

    async def guard(self, route: Route) -> None:
        request = route.request
        self.requests += 1
        if self.requests <= 300 and allowed_request(request.url, request.method, self.url):
            await route.continue_()
        else:
            if len(self.failed) < 50:
                self.failed.append(
                    {"url": request.url[:500], "reason": "blocked by local QA policy"}
                )
            await route.abort("blockedbyclient")

    def error(self, error: Any) -> None:
        if len(self.errors) < 50:
            self.errors.append(str(error)[:2000])

    def console(self, message: Any) -> None:
        if message.type == "error":
            self.error(message.text)

    def request_failed(self, request: Any) -> None:
        if len(self.failed) < 50:
            self.failed.append({"url": request.url[:500], "reason": request.failure})

    def response(self, response: Any) -> None:
        if response.status >= 400 and len(self.failed) < 50:
            self.failed.append({"url": response.url[:500], "status": response.status})


class BrowserTools:
    def __init__(self, workspace: Workspace, projects: Any = None):
        self.projects = projects
        self._origin: str | None = None
        self.workspace = workspace
        self.lock = asyncio.Lock()

    async def readiness(self) -> dict[str, Any]:
        async with self.lock:
            try:
                return await self._isolated_capture(self.workspace.root, None)
            except Exception as exc:
                return {
                    "status": "UNAVAILABLE",
                    "playwright_python_installed": async_playwright is not None,
                    "chromium_installed": False,
                    "chromium_executable": None,
                    "launch_test": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    async def _readiness(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "status": "UNAVAILABLE",
            "playwright_python_installed": async_playwright is not None,
            "chromium_installed": False,
            "chromium_executable": None,
            "launch_test": False,
            "error": None,
        }
        if async_playwright is None:
            report["error"] = "Playwright Python package is not installed"
            return report
        try:
            async with async_playwright() as playwright:
                executable = playwright.chromium.executable_path
                report["chromium_executable"] = executable
                report["chromium_installed"] = await asyncio.to_thread(Path(executable).is_file)
                browser = await playwright.chromium.launch(
                    headless=True,
                    channel="chromium",
                    chromium_sandbox=True,
                    args=["--disable-background-networking", "--disable-extensions"],
                )
                await browser.close()
                report.update(status="READY", launch_test=True)
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    def artifact(self, project: Path, name: str) -> Path:
        target = self.workspace.resolve(self.workspace.relative(project) + "/qa/" + name)
        if not target.is_relative_to(project):
            raise ToolExecutionError("QA artifacts must remain inside the selected project")
        return target

    async def capture(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, BrowserArgs)
        project = self.workspace.resolve(args.project)
        flask = (project / "project.json").is_file()
        if flask:
            _, manifest = read_manifest(self.workspace, args.project)
            if not self.projects:
                raise ToolExecutionError("Flask project toolchain is not configured")
            args = args.model_copy(update={"routes": args.routes or manifest.routes})
        elif not project.is_dir() or served_file(project, "/index.html", "/") is None:
            raise ToolExecutionError(
                "browser project must contain confined index.html or project.json"
            )
        for name in ("desktop.png", "mobile.png", "report.json"):
            self.artifact(project, name)
        async with self.lock:
            try:
                if flask:
                    serving = await self.projects.operation(
                        "serve", ProjectArgs(project=args.project)
                    )
                    self._origin = serving["url"]
                return await self._isolated_capture(project, args)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ToolExecutionError(
                    f"local browser QA failed: {type(exc).__name__}: {exc}; "
                    "no visual success recorded"
                ) from exc
            finally:
                self._origin = None
                if flask:
                    await self.projects.operation("stop", ProjectArgs(project=args.project))

    @asynccontextmanager
    async def serving(self, project: Path) -> AsyncIterator[str]:
        if self._origin:
            yield self._origin
        else:
            async with local_server(project) as url:
                yield url

    async def _isolated_capture(self, project: Path, args: BrowserArgs | None) -> dict[str, Any]:
        """Own the subprocess loop; Windows ASGI Selector loops cannot launch Playwright.

        Cancellation waits for browser/server cleanup before releasing the capture lock.
        No global event-loop policy changes and no detached writes after cancellation.
        """
        cancelled = threading.Event()
        completed: Future[dict[str, Any]] = Future()

        async def run() -> dict[str, Any]:
            capture = asyncio.create_task(
                self._capture(project, args) if args else self._readiness()
            )

            async def watch() -> None:
                while not cancelled.is_set():  # noqa: ASYNC110 - cross-thread cancellation signal
                    await asyncio.sleep(0.05)
                capture.cancel()

            watcher = asyncio.create_task(watch())
            try:
                async with asyncio.timeout(
                    180 if args and args.routes and len(args.routes) > 1 else 45
                ):
                    return await capture
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        def worker() -> None:
            try:
                factory = asyncio.new_event_loop
                if sys.platform == "win32":
                    # ProactorEventLoop is Windows-only and therefore absent from
                    # asyncio's type surface on Linux CI. Runtime lookup preserves
                    # the Windows subprocess behavior without a platform-stub error.
                    factory = getattr(asyncio, "ProactorEventLoop")
                with asyncio.Runner(loop_factory=factory) as runner:
                    result = runner.run(run())
                completed.set_result(result)
            except BaseException as exc:
                completed.set_exception(exc)

        thread = threading.Thread(target=worker, name="local-visual-qa", daemon=True)
        thread.start()
        wrapped = asyncio.wrap_future(completed)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            cancelled.set()
            while not wrapped.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve failures without replacing the caller's cancellation.
            if not wrapped.cancelled():
                wrapped.exception()
            raise

    async def _capture(self, project: Path, args: BrowserArgs) -> dict[str, Any]:
        previews: list[dict[str, Any]] = []
        images: list[str] = []
        ready = await self._readiness()
        if ready["status"] != "READY":
            return {
                "status": "failed",
                "error_code": "browser_unavailable",
                "error": ready["error"],
                "browser_readiness": ready,
                "screenshots_generated": False,
                "setup": "Operator only: python -m playwright install chromium",
            }
        assert async_playwright is not None
        async with self.serving(project) as url, async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                channel="chromium",
                chromium_sandbox=True,
                args=[
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                routes = args.routes or ["/"]
                captures = [
                    (index, route, name, viewport)
                    for index, route in enumerate(routes)
                    for name, viewport in VIEWPORTS.items()
                ]
                for route_index, route, name, viewport in captures:
                    if args.viewport != "both" and args.viewport != name:
                        continue
                    context = await browser.new_context(
                        viewport={"width": viewport["width"], "height": viewport["height"]},
                        service_workers="block",
                        accept_downloads=False,
                        permissions=[],
                    )
                    observed = Observations(url)
                    errors, failed = observed.errors, observed.failed

                    await context.route("**/*", observed.guard)
                    await context.route_web_socket("**/*", lambda ws: ws.close())
                    await context.add_init_script("""
                        for (const name of ['RTCPeerConnection',
                            'webkitRTCPeerConnection', 'WebTransport']) {
                            Object.defineProperty(globalThis, name,
                                {value: undefined, configurable: false});
                        }
                        window.open = () => null;
                    """)
                    page = await context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", observed.error)
                    page.on("console", observed.console)
                    page.on("requestfailed", observed.request_failed)
                    page.on("response", observed.response)
                    loaded = False
                    http_status = None
                    page_url = url.rstrip("/") + route
                    try:
                        response = await page.goto(
                            page_url, wait_until="domcontentloaded", timeout=10000
                        )
                        http_status = response.status if response else None
                        loaded = True
                        await page.wait_for_timeout(500)
                    except Exception as exc:
                        errors.append(f"Page load failed: {type(exc).__name__}: {exc}"[:2000])
                    dimensions = await page.evaluate("""() => ({
                        width: document.documentElement.scrollWidth,
                        height: document.documentElement.scrollHeight,
                        viewport_width: innerWidth, viewport_height: innerHeight,
                        horizontal_overflow: document.documentElement.scrollWidth > innerWidth,
                        visible_text: (document.body?.innerText || '').slice(0, 4000)
                    })""")
                    data = await page.screenshot(
                        type="png", full_page=False, animations="disabled", timeout=8000
                    )
                    filename = (
                        f"{name}.png" if route == "/" else f"route-{route_index:02d}-{name}.png"
                    )
                    relative = self.workspace.relative(project) + "/qa/" + filename
                    target = self.artifact(project, filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.artifact(project, filename).write_bytes(data)
                    images.append(base64.b64encode(data).decode())
                    previews.append(
                        {
                            "url": page_url,
                            "route": route,
                            "http_status": http_status,
                            "render_success": loaded
                            and http_status is not None
                            and http_status < 400,
                            "viewport": viewport,
                            "device": name,
                            "title": await page.title(),
                            "dom_loaded": loaded,
                            "console_errors": errors,
                            "failed_resources": failed,
                            "horizontal_overflow": dimensions["horizontal_overflow"],
                            "document_dimensions": dimensions,
                            "screenshot": relative,
                            "screenshot_sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )
                    await context.close()
            finally:
                await browser.close()
        report = {
            "project": args.project,
            "captured_at": datetime.now(UTC).isoformat(),
            "timestamp": datetime.now(UTC).isoformat(),
            "browser_ready": True,
            "desktop_rendered": any(p["device"] == "desktop" and p["dom_loaded"] for p in previews),
            "mobile_rendered": any(p["device"] == "mobile" and p["dom_loaded"] for p in previews),
            "console_errors": {p["device"]: p["console_errors"] for p in previews},
            "failed_resources": {p["device"]: p["failed_resources"] for p in previews},
            "horizontal_overflow": {p["device"]: p["horizontal_overflow"] for p in previews},
            "scroll_height": {p["device"]: p["document_dimensions"]["height"] for p in previews},
            "viewport": {p["device"]: p["viewport"] for p in previews},
            "routes": args.routes or ["/"],
            "previews": previews,
            "screenshots_generated": bool(previews),
            "screenshots": [p["screenshot"] for p in previews],
            "server_stopped": True,
            "scope": (
                "Rendered initial page; no interaction testing. "
                "Screenshots are evidence, not a correctness verdict."
            ),
        }
        report_path = self.workspace.relative(project) + "/qa/report.json"
        self.artifact(project, "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        return {**report, "report_path": report_path, "_images": images}
