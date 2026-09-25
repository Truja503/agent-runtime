"""Manifest input is data, never a process invocation or install option."""

import json

import pytest
from pydantic import ValidationError

from app.errors import ToolExecutionError
from app.tools.filesystem import Workspace
from app.tools.project_manifest import ProjectArgs, ProjectManifest, read_manifest


def test_exact_allowed_dependencies_and_five_routes() -> None:
    manifest = ProjectManifest(
        python={"Flask": "3.1.2", "Flask-SQLAlchemy": "3.1.1", "pytest": "8.4.2"},
        npm={"vite": "6.4.3", "gsap": "3.13.0", "lenis": "1.3.11", "three": "0.180.0"},
        routes=["/", "/work", "/work/example-project", "/journal", "/studio"],
    )
    manifest.check_allowed()
    assert manifest.python["flask"] == "3.1.2"
    assert len(manifest.routes) == 5


@pytest.mark.parametrize(
    "version",
    [
        "latest",
        "^3.0.0",
        ">=3.0.0",
        "3.0.*",
        "3.0.0 --user",
        "https://example.org/pkg.whl",
        "file:../pkg",
        "git+https://x",
    ],
)
def test_install_specifiers_are_not_commands(version: str) -> None:
    with pytest.raises(ValidationError):
        ProjectManifest(python={"Flask": version})
    with pytest.raises(ValidationError):
        ProjectManifest(npm={"vite": version})


@pytest.mark.parametrize(
    "name",
    [
        "--target",
        "../local",
        "/absolute",
        "C:\\path",
        "a;whoami",
        "https://example.org",
        "git+ssh://x",
        "foo[extra]",
    ],
)
def test_package_name_rejection(name: str) -> None:
    with pytest.raises(ValidationError):
        ProjectManifest(python={name: "1.0.0"})
    with pytest.raises(ValidationError):
        ProjectManifest(npm={name: "1.0.0"})


def test_unknown_requires_operator_allowlist() -> None:
    manifest = ProjectManifest(python={"unknown-package": "1.0.0"})
    with pytest.raises(ToolExecutionError, match="operator package allowlist"):
        manifest.check_allowed()
    manifest.check_allowed(python=frozenset({"unknown-package"}))


@pytest.mark.parametrize(
    "route",
    [
        "//localhost",
        "https://example.org",
        "/../x",
        "/%2e%2e/x",
        "/x?url=http://localhost",
        "/x#fragment",
        "/x\\y",
    ],
)
def test_routes_cannot_change_origin(route: str) -> None:
    with pytest.raises(ValidationError):
        ProjectManifest(routes=[route])


def test_no_manifest_command_or_executable() -> None:
    for field in ("command", "executable", "scripts", "flags", "entrypoint"):
        with pytest.raises(ValidationError):
            ProjectManifest.model_validate({field: "arbitrary"})
        with pytest.raises(ValidationError):
            ProjectArgs.model_validate({"project": "site", field: "arbitrary"})


def test_duplicate_names_and_keys_rejected(workspace: Workspace) -> None:
    with pytest.raises(ValidationError):
        ProjectManifest(python={"Flask_SQLAlchemy": "3.1.1", "flask-sqlalchemy": "3.1.0"})
    project = workspace.root / "site"
    project.mkdir()
    (project / "project.json").write_text('{"framework":"flask","framework":"flask"}')
    with pytest.raises(ToolExecutionError):
        read_manifest(workspace, "site")


def test_confined_manifest(workspace: Workspace) -> None:
    project = workspace.root / "site"
    project.mkdir()
    (project / "project.json").write_text(json.dumps({"python": {"Flask": "3.1.2"}}))
    root, manifest = read_manifest(workspace, "site")
    assert root == project and manifest.python == {"flask": "3.1.2"}
    for path in (".", "../escape", "/absolute", "C:\\host"):
        with pytest.raises(ToolExecutionError):
            read_manifest(workspace, path)
