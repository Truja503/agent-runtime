"""Shared fixtures.

Everything here builds a *real* runtime — real broker, real policy engine, real
privileged service — with only the outside world faked: no network, no
subprocesses, no disk outside a temp directory. The security tests are only
meaningful if they exercise the real path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import ProviderKind, Settings
from app.container import Runtime, build_runtime
from app.models.scripted import ScriptedModelProvider
from app.observability.store import InMemoryEventStore
from app.policy.permissions import AgentRole, Principal
from app.tasks.store import InMemoryTaskStore
from app.tools.builtin import build_registry
from app.tools.filesystem import Workspace
from privileged.audit import ListAuditSink
from privileged.auth import OperatorAuthenticator, hash_secret
from privileged.executor import PrivilegedExecutor
from privileged.service import PrivilegedRequestService
from privileged.store import InMemoryPrivilegedRequestStore

OPERATOR_ID = "alice"
OPERATOR_SECRET = "correct-horse-battery-staple"
API_TOKEN = "test-operator-token"
VIEWER_TOKEN = "test-viewer-token"


class RecordingCommandRunner:
    """Stands in for a subprocess. Records argv; never spawns anything."""

    def __init__(self, exit_code: int = 0, stdout: str = "ok", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def run(self, argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self._exit_code, self._stdout, self._stderr


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    workspace = Workspace(root)
    (root / "README.md").write_text("# workspace\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return workspace


@pytest.fixture
def settings(tmp_path: Path, workspace: Workspace) -> Settings:
    return Settings(
        _env_file=None,
        model_profiles_path=tmp_path / "models.json",
        project_toolchain_image="",
        model_provider=ProviderKind.SCRIPTED,
        workspace_root=workspace.root,
        database_path=tmp_path / "runtime.db",
        privileged_database_path=tmp_path / "privileged.db",
        api_tokens=f"tester:operator:{API_TOKEN},watcher:viewer:{VIEWER_TOKEN}",
        privileged_operators=f"{OPERATOR_ID}:{hash_secret(OPERATOR_SECRET)}",
        privileged_api_enabled=False,
        internet_access_enabled=False,
    )


@pytest.fixture
def audit() -> ListAuditSink:
    return ListAuditSink()


@pytest.fixture
def privileged_runner() -> RecordingCommandRunner:
    return RecordingCommandRunner()


@pytest.fixture
def privileged_service(
    audit: ListAuditSink, privileged_runner: RecordingCommandRunner
) -> PrivilegedRequestService:
    return PrivilegedRequestService(
        store=InMemoryPrivilegedRequestStore(),
        authenticator=OperatorAuthenticator({OPERATOR_ID: hash_secret(OPERATOR_SECRET)}),
        executor=PrivilegedExecutor(
            runner=privileged_runner,
            # Pin the binaries so the test does not depend on the host having
            # systemd installed.
            binaries={"systemctl": "/usr/bin/systemctl", "journalctl": "/usr/bin/journalctl"},
        ),
        audit=audit,
    )


@pytest.fixture
def test_runner() -> RecordingCommandRunner:
    return RecordingCommandRunner(stdout="3 passed")


@pytest.fixture
def registry(workspace: Workspace, test_runner: RecordingCommandRunner) -> Any:
    return build_registry(
        workspace=workspace,
        test_runner=test_runner,
        test_suites={"default": ["/usr/bin/true"]},
    )


@pytest.fixture
def scripted_model() -> ScriptedModelProvider:
    return ScriptedModelProvider()


@pytest.fixture
def runtime(
    settings: Settings,
    registry: Any,
    scripted_model: ScriptedModelProvider,
    privileged_service: PrivilegedRequestService,
) -> Iterator[Runtime]:
    built = build_runtime(
        settings,
        model=scripted_model,
        task_store=InMemoryTaskStore(),
        event_sinks=[InMemoryEventStore()],
        registry=registry,
        privileged_service=privileged_service,
    )
    yield built


def principal_for(
    role: AgentRole, *, tools: set[str], kind: ProviderKind = ProviderKind.ANTHROPIC
) -> Principal:
    """A principal backed by a cloud model unless told otherwise.

    Cloud is the default in tests on purpose: the interesting question is what
    a cloud-backed agent can do.
    """
    return Principal(
        name=role.value,
        role=role,
        model_kind=kind,
        allowed_tools=frozenset(tools),
        task_id="task-under-test",
    )
