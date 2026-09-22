"""Workspace-confined filesystem tools.

Confinement is enforced here, in deterministic code, because the agent asking
for the path is untrusted by construction. ``../../../../etc/passwd`` and
``/etc/passwd`` both fail before any file is opened.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import BaseModel, Field

from app.errors import ToolExecutionError

#: Caps so a single call cannot exhaust memory or fill the context window.
MAX_READ_CHARS = 100_000  # legacy export
MAX_READ_BYTES = 24_000
DEFAULT_READ_BYTES = 12_000
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
        if (
            candidate.is_absolute()
            or relative.startswith(("~", "/", "\\"))
            or PureWindowsPath(relative).drive
        ):
            raise ToolExecutionError(f"absolute paths are not permitted: {relative!r}")

        # resolve() collapses `..` and follows symlinks, so the containment
        # check below sees the real destination rather than the literal string.
        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolExecutionError(f"path escapes the workspace: {relative!r}")
        return resolved

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."


class ReadArgs(BaseModel):
    path: str = Field(max_length=1024)
    offset: int = Field(default=0, ge=0, le=1_000_000_000)
    max_bytes: int = Field(default=DEFAULT_READ_BYTES, ge=4, le=MAX_READ_BYTES)


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
            with target.open("rb") as source:
                stat = os.fstat(source.fileno())
                total = stat.st_size
                if args.offset > total:
                    raise ToolExecutionError("offset exceeds file size; restart reading at zero")
                source.seek(args.offset)
                raw = source.read(args.max_bytes)
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                text = decoder.decode(raw, final=args.offset + len(raw) >= total)
                buffered, _ = decoder.getstate()
                returned = len(raw) - len(buffered)
                end = args.offset + returned
                after = os.fstat(source.fileno())
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ToolExecutionError("file changed while reading; restart at offset zero")
        except OSError as exc:
            raise ToolExecutionError(f"could not read {args.path!r}: {exc.strerror}") from None
        complete = end == total
        return {
            "path": self._workspace.relative(target),
            "content": text,
            "complete": complete,
            "offset": args.offset,
            "bytes_returned": returned,
            "total_bytes": total,
            "next_offset": None if complete else end,
            "version": f"{stat.st_ino}:{stat.st_mtime_ns}:{total}",
            "truncated": not complete,
            "bytes": total,
        }

    async def write(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, WriteArgs)
        target = self._workspace.resolve(args.path)
        if target.is_dir():
            raise ToolExecutionError(f"refusing to overwrite a directory: {args.path!r}")
        try:
            content = args.content.encode("utf-8")
            changed = (
                not target.exists()
                or target.stat().st_size != len(content)
                or target.read_bytes() != content
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.content, encoding="utf-8", newline="")
        except OSError as exc:
            raise ToolExecutionError(f"could not write {args.path!r}: {exc.strerror}") from None
        return {
            "path": self._workspace.relative(target),
            "bytes_written": len(args.content.encode("utf-8")),
            "changed": changed,
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
