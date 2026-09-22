"""FastAPI application factory.

This process must not run as root. It has no privileged executor, no sudo, and
no way to obtain either: the only handle it holds into the privileged domain is
``app.privileged_bridge``, which can create a request and read its status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import control
from app.api import privileged as privileged_routes
from app.api import tasks as task_routes
from app.api.auth import ApiAuthenticator, ApiCaller, current_caller
from app.api.auth import router as auth_router
from app.api.deps import get_runtime
from app.api.schemas import HealthResponse
from app.config import Settings, load_settings
from app.container import Runtime, build_runtime
from app.observability.logging import configure_logging


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    resolved = settings or load_settings()
    configure_logging(resolved.log_level)
    built = runtime or build_runtime(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await built.start()
        yield
        await built.aclose()

    application = FastAPI(
        title="Agent Runtime",
        version="0.1.0",
        summary="Cloud LLMs may request authority; they never possess it.",
        lifespan=lifespan,
    )
    application.state.runtime = built
    application.state.settings = resolved
    application.state.authenticator = ApiAuthenticator(resolved.api_principals)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default error bodies echo rejected inputs, including secrets.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": e["loc"], "type": e["type"], "msg": "invalid value"}
                    for e in exc.errors()
                ]
            },
        )

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health(rt: Runtime = Depends(get_runtime)) -> HealthResponse:
        # Reports which provider is configured, never any credential.
        return HealthResponse(
            status="ok",
            provider=rt.model.name,
            model=rt.model.model,
            privileged_api_enabled=rt.settings.privileged_api_enabled,
            tools=rt.registry.names(),
        )

    @application.get("/tools", tags=["tools"])
    async def list_tools(
        rt: Runtime = Depends(get_runtime),
        _: ApiCaller = Depends(current_caller),
    ) -> list[dict[str, object]]:
        return rt.registry.describe()

    application.include_router(auth_router)
    application.include_router(task_routes.router)
    application.include_router(control.router)

    # Not mounted unless explicitly enabled. When it is absent, the approval
    # endpoints do not exist at all — approval is CLI-only.
    if resolved.privileged_api_enabled:
        application.include_router(privileged_routes.router)

    ui = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if ui.is_dir():
        application.mount("/dashboard", StaticFiles(directory=ui, html=True), name="dashboard")

    return application


app = create_app  # uvicorn --factory app.main:app
