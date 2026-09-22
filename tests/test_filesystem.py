"""Workspace confinement."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import ToolExecutionError
from app.tools.filesystem import (
    FilesystemTools,
    ListArgs,
    ReadArgs,
    Workspace,
    WriteArgs,
)


async def test_read_inside_the_workspace(workspace: Workspace) -> None:
    tools = FilesystemTools(workspace)
    result = await tools.read(ReadArgs(path="README.md"))
    # Byte-offset pagination preserves the file's actual newline bytes.
    assert result["content"] == (workspace.root / "README.md").read_bytes().decode("utf-8")
    assert result["complete"] is True
    assert result["path"] == "README.md"


async def test_write_then_read_inside_the_workspace(workspace: Workspace) -> None:
    tools = FilesystemTools(workspace)
    await tools.write(WriteArgs(path="notes/summary.md", content="done"))
    assert (workspace.root / "notes" / "summary.md").read_text() == "done"
    result = await tools.read(ReadArgs(path="notes/summary.md"))
    assert result["content"] == "done"


async def test_list_inside_the_workspace(workspace: Workspace) -> None:
    tools = FilesystemTools(workspace)
    result = await tools.list_dir(ListArgs(path="."))
    names = {entry["name"] for entry in result["entries"]}
    assert {"README.md", "src"} <= names


@pytest.mark.parametrize(
    "path",
    [
        "../../../../etc/passwd",
        "../outside.txt",
        "src/../../escape.txt",
        "./../../etc/hosts",
    ],
)
def test_relative_traversal_is_rejected(workspace: Workspace, path: str) -> None:
    with pytest.raises(ToolExecutionError, match="escapes the workspace"):
        workspace.resolve(path)


@pytest.mark.parametrize("path", ["/etc/passwd", "/tmp/evil.txt", "~/.ssh/id_rsa"])
def test_absolute_and_home_paths_are_rejected(workspace: Workspace, path: str) -> None:
    with pytest.raises(ToolExecutionError, match="not permitted"):
        workspace.resolve(path)


def test_symlink_out_of_the_workspace_is_rejected(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Containment is checked after resolution, so a symlink cannot smuggle."""
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    try:
        (workspace.root / "link.txt").symlink_to(secret)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise

    with pytest.raises(ToolExecutionError, match="escapes the workspace"):
        workspace.resolve("link.txt")


def test_empty_and_null_byte_paths_are_rejected(workspace: Workspace) -> None:
    with pytest.raises(ToolExecutionError):
        workspace.resolve("")
    with pytest.raises(ToolExecutionError, match="null bytes"):
        workspace.resolve("ok\x00.txt")


async def test_traversal_through_the_tool_handler_is_rejected(
    workspace: Workspace,
) -> None:
    """Not just the helper — the handler path refuses too."""
    tools = FilesystemTools(workspace)
    with pytest.raises(ToolExecutionError):
        await tools.read(ReadArgs(path="../../../../etc/passwd"))
    with pytest.raises(ToolExecutionError):
        await tools.write(WriteArgs(path="/etc/cron.d/pwn", content="x"))


async def test_reading_a_missing_file_is_a_clean_error(workspace: Workspace) -> None:
    tools = FilesystemTools(workspace)
    with pytest.raises(ToolExecutionError, match="not a file"):
        await tools.read(ReadArgs(path="nope.md"))
