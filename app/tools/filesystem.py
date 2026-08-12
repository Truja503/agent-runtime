"""Workspace-confined filesystem tools.

Confinement is enforced here, in deterministic code, because the agent asking
for the path is untrusted by construction. ``../../../../etc/passwd`` and
``/etc/passwd`` both fail before any file is opened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.errors import ToolExecutionError

#: Caps so a single call cannot exhaust memory or fill the context window.
MAX_READ_CHARS = 100_000
MAX_WRITE_CHARS = 100_000
MAX_LIST_ENTRIES = 500


class Workspace:
    """A directory that filesystem tools may not leave."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        """Map a caller-supplied path onto a real path inside the workspace.

        Raises :class:`ToolExecutionError` for anything that would escape,
        including absolute paths, ``~`` expansion, ``..`` traversal, and
        symlinks pointing outside the root.
        """
        if not relative or not relative.strip():
            raise ToolExecutionError("path must not be empty")
        if "\x00" in relative:
            raise ToolExecutionError("path must not contain null bytes")

        candidate = Path(relative)
        if candidate.is_absolute() or relative.startswith("~"):
            raise ToolExecutionError(
                f"absolute paths are not permitted: {relative!r}"
            )

        # resolve() collapses `..` and follows symlinks, so the containment
        # check below sees the real destination rather than the literal string.
        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolExecutionError(
                f"path escapes the workspace: {relative!r}"
            )
        return resolved

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path != self.root else "."


class ReadArgs(BaseModel):
    path: str = Field(max_length=1024)


class WriteArgs(BaseModel):
    path: str = Field(max_length=1024)
    content: str = Field(max_length=MAX_WRITE_CHARS)


class ListArgs(BaseModel):
    path: str = Field(default=".", max_length=1024)


class FilesystemTools:
    """Handlers bound to one workspace. Constructed by the container, not by agents."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def read(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ReadArgs)
        target = self._workspace.resolve(args.path)
        if not target.is_file():
            raise ToolExecutionError(f"not a file: {args.path!r}")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolExecutionError(f"could not read {args.path!r}: {exc.strerror}") from None
        truncated = len(text) > MAX_READ_CHARS
        return {
            "path": self._workspace.relative(target),
            "content": text[:MAX_READ_CHARS],
            "truncated": truncated,
            "bytes": target.stat().st_size,
        }

    async def write(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, WriteArgs)
        target = self._workspace.resolve(args.path)
        if target.is_dir():
            raise ToolExecutionError(f"refusing to overwrite a directory: {args.path!r}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"could not write {args.path!r}: {exc.strerror}") from None
        return {
            "path": self._workspace.relative(target),
            "bytes_written": len(args.content.encode("utf-8")),
        }

    async def list_dir(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ListArgs)
        target = self._workspace.resolve(args.path)
        if not target.is_dir():
            raise ToolExecutionError(f"not a directory: {args.path!r}")
        entries = []
        for child in sorted(target.iterdir())[:MAX_LIST_ENTRIES]:
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                }
            )
        return {"path": self._workspace.relative(target), "entries": entries}
