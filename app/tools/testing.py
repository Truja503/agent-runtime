"""``tests.run`` — run a *predefined* project command.

There is deliberately no ``run_command`` capability. The agent chooses a suite
by name from a fixed table; it never supplies an argument vector, a shell
string, or an executable path. The worst an agent can do here is pick the wrong
entry from a list you wrote.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.errors import ToolExecutionError

MAX_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 120


def default_suites() -> dict[str, list[str]]:
    """The commands this runtime is willing to run, by name.

    Executables are resolved to absolute paths at build time where possible, so
    the command does not depend on whatever ``PATH`` happens to be set.
    """
    python = sys.executable
    suites = {
        "default": [python, "-m", "pytest", "-q"],
        "unit": [python, "-m", "pytest", "-q", "tests"],
    }
    ruff = shutil.which("ruff")
    if ruff:
        suites["lint"] = [ruff, "check", "."]
    return suites


class CommandRunner(Protocol):
    """Injected so tests never actually spawn a subprocess."""

    async def run(
        self, argv: list[str], *, cwd: Path, timeout: int
    ) -> tuple[int, str, str]: ...


class SubprocessRunner:
    """The real runner: no shell, explicit argv, hard timeout, captured output."""

    async def run(
        self, argv: list[str], *, cwd: Path, timeout: int
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ToolExecutionError(f"command timed out after {timeout}s") from None
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


class RunTestsArgs(BaseModel):
    suite: str = Field(default="default", max_length=64)


class ProjectTestTools:
    def __init__(
        self,
        *,
        workspace_root: Path,
        runner: CommandRunner | None = None,
        suites: dict[str, list[str]] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._workspace_root = workspace_root
        self._runner = runner or SubprocessRunner()
        self._suites = suites if suites is not None else default_suites()
        self._timeout = timeout

    @property
    def suite_names(self) -> list[str]:
        return sorted(self._suites)

    async def run(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, RunTestsArgs)
        argv = self._suites.get(args.suite)
        if argv is None:
            raise ToolExecutionError(
                f"unknown suite {args.suite!r}; available: {', '.join(self.suite_names)}"
            )
        exit_code, stdout, stderr = await self._runner.run(
            list(argv), cwd=self._workspace_root, timeout=self._timeout
        )
        return {
            "suite": args.suite,
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "stdout": stdout[-MAX_OUTPUT_CHARS:],
            "stderr": stderr[-MAX_OUTPUT_CHARS:],
        }
