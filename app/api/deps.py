"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from app.container import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime
