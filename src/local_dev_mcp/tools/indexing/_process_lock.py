"""Cooperative, crash-released OS locks for one Git-registered Trail graph.

The lock is stored in the Git common directory, never inside the checkout.
Independent LOCAL_DEV_MCP instances agree on a stable root-derived filename.
Unmodified standalone Trail CLI processes do not participate in this protocol.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ..git._execution import GitExecution
from ..git.commands.worktree import common_directory

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

_POLL_SECONDS = 0.1


def _try_acquire(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN) or getattr(error, "winerror", None) in (32, 33):
                return False
            raise
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@asynccontextmanager
async def coordinated_trail_index(settings: ServerSettings, root: Path) -> AsyncGenerator[None]:
    """Serialize a single graph across cooperating processes.

    Nonblocking lock attempts keep asyncio cancellation safe. The open file
    descriptor owns the advisory lock and OS cleanup releases it after crashes;
    do not unlink the file, which could split contenders across two inodes.
    The caller owns the acquisition deadline through its cancellation scope.
    """
    if (
        root == settings.policy.root
        or not root.is_relative_to(settings.policy.root)
        or await asyncio.to_thread(root.resolve) != root
    ):
        raise ToolError("Worktree index target is outside the authorized linked checkout scope.")

    common = await common_directory(GitExecution(settings))
    if not common.is_dir() or not common.is_relative_to(settings.policy.root):
        raise ToolError("Git common directory is outside the authorized project or unavailable.")
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:32]
    lockfile = common / f"local-dev-mcp-trail-{digest}.lock"
    fd: int | None = None
    held = False

    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if lockfile.is_symlink():
            raise OSError("unsafe_lock_file")
        fd = os.open(lockfile, flags, 0o600)
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("unsafe_lock_file")
        if info.st_size == 0:
            os.write(fd, b"\0")
        while True:
            try:
                held = _try_acquire(fd)
            except OSError as error:
                raise ToolError("Trail process lock could not be acquired safely.") from error
            if held:
                break
            await asyncio.sleep(_POLL_SECONDS)
        yield
    except OSError as error:
        raise ToolError("Trail process lock is unavailable; no Trail command was run.") from error
    finally:
        if fd is not None:
            try:
                if held:
                    _release(fd)
            finally:
                os.close(fd)
