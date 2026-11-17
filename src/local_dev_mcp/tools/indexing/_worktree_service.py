"""Worktree-scoped Trail status and refresh with server-local per-root serialization."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastmcp.exceptions import ToolError

from ._manager import IndexReport
from ._process_lock import coordinated_trail_index
from ._targets import resolve_index_target

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


_LOCK_TIMEOUT_SECONDS = 360


class WorktreeIndexReport(IndexReport):
    branch: str
    head: str
    worktree_path: str


class WorktreeIndexService:
    """Serialize calls to one registered checkout while allowing independent roots.

    In-process root locks and cooperative OS file locks cover independent MCP
    instances, but not unmodified standalone Trail CLI processes. Revalidate
    Git identity within each critical section and again
    after Trail completes. No cached IndexManager outlives a request, and
    Worktree automatic refresh is explicitly disabled by the target resolver.
    """

    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self._locks: dict[Path, asyncio.Lock] = {}

    async def execute(
        self,
        *,
        branch: str,
        expected_head: str,
        action: Literal["status", "refresh"],
    ) -> WorktreeIndexReport:
        write = action == "refresh"
        # Resolve before entering the lock to obtain a verified root, then
        # resolve again under the lock to reject a concurrent Git transition.
        initial = await resolve_index_target(
            self.settings, kind="worktree", branch=branch, expected_head=expected_head, write=write
        )
        root = initial.policy.root
        lock = self._locks.setdefault(root, asyncio.Lock())
        try:
            async with asyncio.timeout(_LOCK_TIMEOUT_SECONDS):
                async with lock, coordinated_trail_index(self.settings, root):
                    selected = await resolve_index_target(
                        self.settings, kind="worktree", branch=branch, expected_head=expected_head, write=write
                    )
                    if selected.policy.root != root:
                        raise ToolError("Worktree registration changed during index selection.")
                    try:
                        report = await (selected.index.refresh() if write else selected.index.status())
                    finally:
                        await selected.index.close()

                    # Even if Trail says Fresh, never return a successful report for
                    # an unexpectedly dirty, moved or unregistered target.
                    after = await resolve_index_target(
                        self.settings, kind="worktree", branch=branch, expected_head=expected_head, write=False
                    )
                    if after.policy.root != root:
                        raise ToolError("Worktree registration changed during index execution.")
        except TimeoutError as error:
            raise ToolError("Worktree index operation exceeded its time budget.") from error

        return {
            **report,
            "project": str(self.settings.policy.root),
            "branch": branch,
            "head": expected_head,
            "worktree_path": root.relative_to(self.settings.policy.root).as_posix(),
        }
