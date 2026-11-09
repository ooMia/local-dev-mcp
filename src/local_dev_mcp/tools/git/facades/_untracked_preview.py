"""Bounded, explicitly scoped text previews for nonignored untracked Git paths."""

import difflib
import os
import stat
from pathlib import Path

from .._execution import GitExecution
from ..commands import status
from . import _visibility

MAX_FILES = 32
MAX_FILE_BYTES = 32768
MAX_TOTAL_BYTES = 131072


def _read_regular_text(path: Path) -> str | None:
    """Read a small regular file without following a final symlink."""
    descriptor: int | None = None
    try:
        if path.is_symlink():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            return None
        data = os.read(descriptor, MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or b"\x00" in data:
            return None
        return data.decode("utf-8")
    except OSError, UnicodeError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _text_patch(path: str, contents: str) -> str:
    """A text-only unified preview, not an exact Git staging patch."""
    lines = list(
        difflib.unified_diff(
            [], contents.splitlines(keepends=True), fromfile="/dev/null", tofile=f"b/{path}", lineterm="\n"
        )
    )
    if lines:
        patch = "diff --git a/" + path + " b/" + path + "\nnew file mode 100644\n" + "".join(lines)
        return patch + ("" if patch.endswith("\n") else "\n")
    return "diff --git a/" + path + " b/" + path + "\nnew file mode 100644\n"


async def preview_untracked(execution: GitExecution, scopes: list[str]) -> tuple[str, bool]:
    """Return a bounded preview and whether all matching files were represented."""
    _, _, entries = await status.read_porcelain_v2(execution)
    selected = [
        entry["path"]
        for entry in entries
        if entry["kind"] == "untracked" and any(Path(entry["path"]).is_relative_to(Path(scope)) for scope in scopes)
    ]
    incomplete = len(selected) > MAX_FILES
    result: list[str] = []
    total_bytes = 0
    for path in selected[:MAX_FILES]:
        if not _visibility.readable(execution, path) or any(ord(char) < 32 for char in path):
            incomplete = True
            continue
        contents = _read_regular_text(execution.root / path)
        if contents is None:
            incomplete = True
            continue
        fragment = _text_patch(path, contents)
        length = len(fragment.encode("utf-8"))
        if length + total_bytes > MAX_TOTAL_BYTES:
            incomplete = True
            continue
        total_bytes += length
        result.append(fragment)
    return "".join(result), not incomplete
