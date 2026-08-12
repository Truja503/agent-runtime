"""Operator CLI — the human approval mechanism.

Run this as a *different process* from the API, and in a real deployment as a
different Unix user with write access to the privileged database. The API
process never invokes it.

    python -m privileged.cli hash-secret
    python -m privileged.cli list
    python -m privileged.cli show <request-id>
    python -m privileged.cli approve <request-id> --operator alice
    python -m privileged.cli deny <request-id> --operator alice --reason "not now"

Secrets are read from a prompt, never from argv, so they do not appear in
``ps`` output or shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from privileged.auth import OperatorAuthenticator, hash_secret
from privileged.executor import PrivilegedExecutor
from privileged.local_llm import LocalModelIntentParser, RuleBasedIntentParser
from privileged.policy import ExecutionRefused, IntentRejected
from privileged.schemas import PrivilegedRequest
from privileged.service import NotAuthenticated, PrivilegedRequestService
from privileged.store import SQLitePrivilegedRequestStore


def _load_env_file(path: Path) -> None:
    """Minimal .env reader so the CLI matches the API's configuration.

    Deliberately does not import ``app`` — the privileged package must stay
    independently deployable.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _operators() -> dict[str, str]:
    operators: dict[str, str] = {}
    for entry in os.getenv("PRIVILEGED_OPERATORS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        operator_id, _, secret_hash = entry.partition(":")
        if operator_id.strip() and secret_hash.strip():
            operators[operator_id.strip()] = secret_hash.strip()
    return operators


def build_service() -> PrivilegedRequestService:
    _load_env_file(Path(".env"))
    store = SQLitePrivilegedRequestStore(
        Path(os.getenv("PRIVILEGED_DATABASE_PATH", "./data/privileged.db"))
    )
    parser: RuleBasedIntentParser | LocalModelIntentParser
    if os.getenv("PRIVILEGED_PARSER", "rules") == "local":
        parser = LocalModelIntentParser(
            base_url=os.getenv("PRIVILEGED_MODEL_BASE_URL", "http://127.0.0.1:8080/v1"),
            api_key=os.getenv("PRIVILEGED_MODEL_API_KEY", "not-needed"),
            model=os.getenv("PRIVILEGED_MODEL_NAME", "local-model"),
        )
    else:
        parser = RuleBasedIntentParser()
    return PrivilegedRequestService(
        store=store,
        authenticator=OperatorAuthenticator(_operators()),
        executor=PrivilegedExecutor(),
        parser=parser,
        approval_ttl_seconds=int(os.getenv("PRIVILEGED_APPROVAL_TTL_SECONDS", "900")),
    )


def _render(request: PrivilegedRequest) -> str:
    intent = request.intent
    lines = [
        f"request_id  : {request.request_id}",
        f"status      : {request.status.value}",
        f"requested_by: {request.requested_by}",
        f"task_id     : {request.task_id or '-'}",
        f"created_at  : {request.created_at.isoformat()}",
        f"expires_at  : {request.expires_at.isoformat() if request.expires_at else '-'}",
        f"text        : {request.request_text}",
        f"action      : {intent.action.value if intent else '(not parsed)'}",
        f"service     : {intent.service if intent else '-'}",
    ]
    if request.reason:
        lines.append(f"reason      : {request.reason}")
    if request.approved_by:
        lines.append(f"decided_by  : {request.approved_by}")
    if request.result:
        lines.append(f"exit_code   : {request.result.exit_code}")
        lines.append(f"argv        : {' '.join(request.result.argv)}")
    return "\n".join(lines)


async def _list() -> int:
    service = build_service()
    pending = await service.list_pending()
    if not pending:
        print("no requests awaiting approval")
        return 0
    for request in pending:
        print(_render(request))
        print("-" * 60)
    return 0


async def _show(request_id: str) -> int:
    service = build_service()
    request = await service.get_request(request_id)
    if request is None:
        print(f"no such request: {request_id}", file=sys.stderr)
        return 1
    print(_render(request))
    return 0


async def _decide(request_id: str, operator: str, *, approve: bool, reason: str) -> int:
    service = build_service()
    secret = getpass.getpass(f"secret for operator {operator!r}: ")
    try:
        if approve:
            request = await service.approve_and_execute(
                request_id=request_id, operator_id=operator, secret=secret
            )
        else:
            request = await service.deny(
                request_id=request_id, operator_id=operator, secret=secret, reason=reason
            )
    except NotAuthenticated:
        print("authentication failed", file=sys.stderr)
        return 2
    except (ExecutionRefused, IntentRejected) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(_render(request))
    return 0 if request.status.value in {"executed", "denied"} else 4


def _hash_secret() -> int:
    secret = getpass.getpass("new operator secret: ")
    confirm = getpass.getpass("confirm: ")
    if secret != confirm:
        print("secrets do not match", file=sys.stderr)
        return 1
    if len(secret) < 12:
        print("use at least 12 characters", file=sys.stderr)
        return 1
    print(hash_secret(secret))
    print(
        "\nAdd it to .env as:  PRIVILEGED_OPERATORS=<operator_id>:<the hash above>",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="privileged-ctl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hash-secret", help="hash a new operator secret")
    sub.add_parser("list", help="list requests awaiting approval")

    show = sub.add_parser("show", help="show one request")
    show.add_argument("request_id")

    approve = sub.add_parser("approve", help="approve and execute a request")
    approve.add_argument("request_id")
    approve.add_argument("--operator", required=True)

    deny = sub.add_parser("deny", help="deny a request")
    deny.add_argument("request_id")
    deny.add_argument("--operator", required=True)
    deny.add_argument("--reason", default="")

    args = parser.parse_args(argv)

    if args.command == "hash-secret":
        return _hash_secret()
    if args.command == "list":
        return asyncio.run(_list())
    if args.command == "show":
        return asyncio.run(_show(args.request_id))
    if args.command == "approve":
        return asyncio.run(
            _decide(args.request_id, args.operator, approve=True, reason="")
        )
    if args.command == "deny":
        return asyncio.run(
            _decide(args.request_id, args.operator, approve=False, reason=args.reason)
        )
    return 1  # pragma: no cover - argparse enforces the choices


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
