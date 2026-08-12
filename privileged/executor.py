"""The only place in the codebase that runs a system command.

Rules enforced here, not by convention:

* ``shell=False`` always — there is no string that gets handed to a shell;
* the argument vector is *built* from a validated intent by a pure function,
  never parsed out of text;
* executables are absolute paths chosen from a fixed candidate list;
* every call has a timeout and captured, truncated output.

Model output does not appear anywhere in this module.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

from privileged.schemas import (
    ExecutionResult,
    PrivilegedAction,
    PrivilegedIntent,
    ReadServiceLogsIntent,
)

MAX_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 30

#: Absolute candidates, tried in order. Nothing is resolved through ``PATH`` at
#: call time, so a poisoned environment cannot redirect the binary.
_BINARY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
    "journalctl": ("/usr/bin/journalctl", "/bin/journalctl"),
}


class CommandNotAvailable(Exception):
    """The required binary is not present on this host."""


def resolve_binary(name: str, overrides: dict[str, str] | None = None) -> str:
    if overrides and name in overrides:
        return overrides[name]
    for candidate in _BINARY_CANDIDATES.get(name, ()):
        if Path(candidate).exists():
            return candidate
    # Last resort for non-systemd hosts; still an absolute path.
    found = shutil.which(name)
    if found:
        return found
    raise CommandNotAvailable(f"{name} is not available on this host")


class CommandRunner(Protocol):
    async def run(self, argv: list[str], *, timeout: int) -> tuple[int, str, str]: ...


class SubprocessCommandRunner:
    async def run(self, argv: list[str], *, timeout: int) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "", f"command timed out after {timeout}s"
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


class PrivilegedExecutor:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        binaries: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._binaries = binaries
        self._timeout = timeout

    def build_argv(self, intent: PrivilegedIntent) -> list[str]:
        """Map a validated intent to an argument vector.

        Every element is either a constant from this function or the validated
        service name. There is no path by which arbitrary text becomes an
        argument.
        """
        service = intent.service
        if intent.action is PrivilegedAction.RESTART_SERVICE:
            return [resolve_binary("systemctl", self._binaries), "restart", service]
        if intent.action is PrivilegedAction.READ_SERVICE_STATUS:
            return [
                resolve_binary("systemctl", self._binaries),
                "status",
                service,
                "--no-pager",
            ]
        if intent.action is PrivilegedAction.READ_SERVICE_LOGS:
            assert isinstance(intent, ReadServiceLogsIntent)
            return [
                resolve_binary("journalctl", self._binaries),
                "-u",
                service,
                "-n",
                str(intent.lines),
                "--no-pager",
            ]
        raise CommandNotAvailable(  # pragma: no cover - closed enum
            f"no command builder for {intent.action!r}"
        )

    async def execute(self, intent: PrivilegedIntent) -> ExecutionResult:
        argv = self.build_argv(intent)
        exit_code, stdout, stderr = await self._runner.run(argv, timeout=self._timeout)
        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout[-MAX_OUTPUT_CHARS:],
            stderr=stderr[-MAX_OUTPUT_CHARS:],
            argv=argv,
        )
