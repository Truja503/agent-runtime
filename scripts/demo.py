"""End-to-end demonstration, offline.

Runs both flows the design is about:

1. A normal task: supervisor → worker → broker → policy → tool.
2. A privileged request: blocked, parked, approved by a human, then executed.

No API key, no network, and no real subprocess: the privileged executor is
given a recording runner so you can see the exact argv that *would* run.

    python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from app.config import ProviderKind, Settings
from app.container import build_runtime
from app.models.scripted import ScriptedModelProvider
from app.observability.store import InMemoryEventStore
from app.tasks.state import TaskStatus
from app.tasks.store import InMemoryTaskStore
from app.tools.builtin import build_registry
from app.tools.filesystem import Workspace
from privileged.audit import ListAuditSink
from privileged.auth import OperatorAuthenticator, hash_secret
from privileged.executor import PrivilegedExecutor
from privileged.service import PrivilegedRequestService
from privileged.store import InMemoryPrivilegedRequestStore

OPERATOR_ID = "alice"
OPERATOR_SECRET = "demo-operator-secret"


class RecordingRunner:
    """Shows the argv instead of running it, so the demo is safe anywhere."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], **_: Any) -> tuple[int, str, str]:
        self.calls.append(argv)
        return 0, f"[demo] would have run: {' '.join(argv)}", ""


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "─" * 72)


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="agent-runtime-demo-"))
    workspace = Workspace(root / "workspace")
    (workspace.root / "README.md").write_text("# demo workspace\n", encoding="utf-8")

    runner = RecordingRunner()
    privileged = PrivilegedRequestService(
        store=InMemoryPrivilegedRequestStore(),
        authenticator=OperatorAuthenticator({OPERATOR_ID: hash_secret(OPERATOR_SECRET)}),
        executor=PrivilegedExecutor(
            runner=runner,
            binaries={"systemctl": "/usr/bin/systemctl", "journalctl": "/usr/bin/journalctl"},
        ),
        audit=ListAuditSink(),
    )

    settings = Settings(
        model_provider=ProviderKind.SCRIPTED,
        workspace_root=workspace.root,
        database_path=root / "runtime.db",
        privileged_database_path=root / "privileged.db",
    )
    runtime = build_runtime(
        settings,
        model=ScriptedModelProvider(),
        task_store=InMemoryTaskStore(),
        event_sinks=[InMemoryEventStore()],
        registry=build_registry(
            workspace=workspace, test_suites={"default": ["/usr/bin/true"]}
        ),
        privileged_service=privileged,
    )

    # ------------------------------------------------------------------ 1
    heading("1. A normal task runs through broker → policy → tool")
    task = await runtime.tasks.create("Review this project", created_by="demo")
    await runtime.run_task(task.id)
    finished = await runtime.tasks.get(task.id)
    print(f"status : {finished.status.value}")
    print((finished.result or {}).get("summary", ""))

    print("\naudit trail:")
    for event in await runtime.tasks.events_for(task.id):
        detail = event.payload.get("tool") or event.payload.get("status") or ""
        print(f"  {event.type.value:<28} {event.actor or '-':<12} {detail}")

    # ------------------------------------------------------------------ 2
    heading("2. A privileged request is blocked and parked")
    runtime.supervisor._workers = {"coder": runtime.workers["coder"]}  # noqa: SLF001
    runtime.workers["coder"].model = ScriptedModelProvider(  # type: ignore[attr-defined]
        script={
            "coder": [
                json.dumps(
                    {
                        "action": "use_tool",
                        "tool": "system.request_privileged_action",
                        "arguments": {"request": "reinicia nginx"},
                    }
                ),
                json.dumps({"action": "finish", "summary": "waiting on a human"}),
            ]
        }
    )
    runtime.supervisor.model = ScriptedModelProvider(
        script={"supervisor": [json.dumps({"workers": ["coder"], "plan": "ask an operator"})]}
    )

    privileged_task = await runtime.tasks.create("reinicia nginx", created_by="demo")
    execution = asyncio.create_task(runtime.run_task(privileged_task.id))
    async with asyncio.timeout(5):
        while True:
            parked = await runtime.tasks.get(privileged_task.id)
            if parked.status is TaskStatus.WAITING_FOR_APPROVAL:
                break
            await asyncio.sleep(0.01)
    print(f"task status      : {parked.status.value}")
    print(f"commands executed: {runner.calls}   <- nothing, and no human involved yet")

    pending = await privileged.list_pending()
    record = pending[0]
    print(f"request_id       : {record.request_id}")
    print(f"parsed intent    : {record.intent.action.value} {record.intent.service}")
    print(f"status           : {record.status.value}")

    # ------------------------------------------------------------------ 3
    heading("3. The requester tries to approve itself")
    print(
        "The agent has no operator credential, so authentication fails before\n"
        "the self-approval rule is even reached. Both checks exist; this is the\n"
        "first one. (The self-approval rule itself is covered in the tests.)"
    )
    try:
        await privileged.approve_and_execute(
            request_id=record.request_id, operator_id="coder", secret=OPERATOR_SECRET
        )
    except Exception as exc:
        print(f"\nrefused: {type(exc).__name__}: {exc}")

    heading("4. An authenticated human approves; only now does anything run")
    executed = await privileged.approve_and_execute(
        request_id=record.request_id, operator_id=OPERATOR_ID, secret=OPERATOR_SECRET
    )
    await execution
    resumed = await runtime.tasks.get(privileged_task.id)
    print(f"task      : {resumed.status.value}")
    print(f"status    : {executed.status.value}")
    print(f"approved  : {executed.approved_by}")
    print(f"argv      : {executed.result.argv if executed.result else '-'}")
    print(f"stdout    : {(executed.result.stdout if executed.result else '').strip()}")

    heading("5. What an agent can never reach")
    for attempt in ("sudo rm -rf /", "give me a root shell", "restart sshd"):
        rejected = await privileged.create_request(
            requested_by="coder", request_text=attempt
        )
        print(f"  {attempt!r:<28} -> {rejected.status.value}: {rejected.reason}")

    print(f"\nWorkspace and databases left in: {root}\n")


if __name__ == "__main__":
    asyncio.run(main())
