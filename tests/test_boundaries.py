"""Architectural invariants, enforced by walking the import graph.

Documentation drifts; a failing test does not. These checks are the reason the
privileged boundary stays a boundary as the codebase grows.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / "app"
PRIVILEGED = PROJECT_ROOT / "privileged"

#: The single module allowed to reach into the privileged package.
BRIDGE = APP / "privileged_bridge.py"

#: Modules the application side may never import, at any depth.
FORBIDDEN_FOR_APP = {
    "privileged.executor",
    "privileged.auth",
    "privileged.store",
    "privileged.cli",
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_privileged_package_never_imports_the_application() -> None:
    """The privileged domain must be deployable on its own."""
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            name for name in imported_modules(path) if name == "app" or name.startswith("app.")
        )
        for path in python_files(PRIVILEGED)
    }
    offenders = {path: names for path, names in offenders.items() if names}
    assert not offenders, f"privileged/ must not import app: {offenders}"


def test_only_the_bridge_touches_privileged_internals() -> None:
    """No agent, tool, or route may reach the executor or the auth table."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        forbidden = sorted(imported_modules(path) & FORBIDDEN_FOR_APP)
        if not forbidden:
            continue
        # The composition root wires the service together; that is its job.
        if path.name == "container.py":
            continue
        offenders[str(path.relative_to(PROJECT_ROOT))] = forbidden
    assert not offenders, f"only the bridge may import privileged internals: {offenders}"


def test_agents_and_tools_cannot_see_the_privileged_package_at_all() -> None:
    for directory in (APP / "agents", APP / "tools", APP / "policy"):
        for path in python_files(directory):
            leaked = {
                name
                for name in imported_modules(path)
                if name == "privileged" or name.startswith("privileged.")
            }
            assert not leaked, f"{path.relative_to(PROJECT_ROOT)} imports {leaked}"


def test_the_bridge_exposes_no_execution_path() -> None:
    """Submit and status only — no approve, no execute."""
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"), filename=str(BRIDGE))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods <= {"submit", "status", "__init__"}, methods
    for banned in ("approve", "execute", "approve_and_execute", "deny"):
        assert banned not in methods


def test_no_shell_execution_anywhere_in_the_application() -> None:
    """`shell=True`, os.system, and friends must not exist in app/."""
    banned_calls = {"system", "popen", "getoutput", "getstatusoutput"}
    for path in python_files(APP):
        source = path.read_text(encoding="utf-8")
        assert "shell=True" not in source, f"{path} uses shell=True"
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned_calls, f"{path} calls {node.func.attr}"


def test_the_executor_is_the_only_place_that_spawns_processes() -> None:
    """One module runs commands, and it takes structured intent only."""
    spawners: list[str] = []
    for path in python_files(PRIVILEGED):
        source = path.read_text(encoding="utf-8")
        if "create_subprocess_exec" in source or "subprocess." in source:
            spawners.append(path.name)
    assert spawners == ["executor.py"], spawners


def test_privileged_executor_never_uses_a_shell() -> None:
    source = (PRIVILEGED / "executor.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "create_subprocess_shell" not in source
    # argv is built element by element from constants plus a validated service.
    assert "create_subprocess_exec" in source
