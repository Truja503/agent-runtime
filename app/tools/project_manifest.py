"""Data-only project declarations. No manifest field is an executable or command."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.errors import ToolExecutionError
from app.tools.filesystem import Workspace

PYTHON_PACKAGES = frozenset({"flask", "flask-sqlalchemy", "pytest"})
NPM_PACKAGES = frozenset({"gsap", "lenis", "three", "vite"})
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
PYTHON_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,3}\Z")
PYTHON_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?\Z")
NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9_-]*/)?[a-z0-9][a-z0-9._-]{0,99}\Z")


def package_name(name: str, ecosystem: str) -> str:
    pattern = PYTHON_NAME if ecosystem == "python" else NPM_NAME
    if not pattern.fullmatch(name):
        raise ValueError("invalid package name; URLs, paths and flags are not allowed")
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "python" else name


def route_path(value: str) -> str:
    # Deliberately no encoding, query strings, fragments, backslashes or dot segments.
    if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*", value):
        raise ValueError("route must be a plain origin-relative path")
    return value


class ProjectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=1, max_length=1024)


class ProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    framework: Literal["flask"] = "flask"
    frontend: Literal["none", "vite"] = "none"
    python: dict[str, str] = Field(default_factory=dict, max_length=100)
    npm: dict[str, str] = Field(default_factory=dict, max_length=100)
    routes: list[str] = Field(default_factory=lambda: ["/"], min_length=1, max_length=20)

    @field_validator("python", "npm")
    @classmethod
    def exact_dependencies(cls, value: dict[str, str], info: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, version in value.items():
            canonical = package_name(name, info.field_name)
            if canonical in result:
                raise ValueError("duplicate normalized package name")
            pattern = PYTHON_VERSION if info.field_name == "python" else VERSION
            if not pattern.fullmatch(version):
                raise ValueError("dependencies require exact numeric release versions")
            result[canonical] = version
        return result

    @field_validator("routes")
    @classmethod
    def confined_routes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(route_path(route) for route in value))

    def check_allowed(
        self,
        python: frozenset[str] = PYTHON_PACKAGES,
        npm: frozenset[str] = NPM_PACKAGES,
    ) -> None:
        for ecosystem, selected, allowed in (
            ("python", self.python, python),
            ("npm", self.npm, npm),
        ):
            unknown = sorted(set(selected) - allowed)
            if unknown:
                raise ToolExecutionError(
                    f"operator package allowlist required: {ecosystem} {unknown}"
                )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest field")
        result[key] = value
    return result


def read_manifest(workspace: Workspace, project: str) -> tuple[Path, ProjectManifest]:
    root = workspace.resolve(project)
    if root == workspace.root or not root.is_dir():
        raise ToolExecutionError("project must be an existing directory below the workspace")
    manifest = workspace.resolve(workspace.relative(root / "project.json"))
    if not manifest.is_relative_to(root) or not manifest.is_file():
        raise ToolExecutionError("missing confined project.json")
    if manifest.stat().st_size > 32768:
        raise ToolExecutionError("project.json exceeds 32 KiB")
    try:
        raw = manifest.read_bytes()
        if len(raw) > 32768:
            raise ValueError("project.json exceeds 32 KiB")
        return root, ProjectManifest.model_validate(
            json.loads(raw, object_pairs_hook=_unique_object)
        )
    except ValidationError as exc:
        errors = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[:8]
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'project.json'}: {error['msg']}"
            for error in errors
        )
        raise ToolExecutionError(f"invalid project manifest: {details}") from None
    except (ValueError, OSError) as exc:
        raise ToolExecutionError(f"invalid project manifest: {type(exc).__name__}") from None
