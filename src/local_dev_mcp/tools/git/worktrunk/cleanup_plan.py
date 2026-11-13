"""Conservative, read-only cleanup proposal from Worktrunk prune dry-run."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from .. import _worktrees
from .._execution import GitExecution
from ..facades._refs import resolve_local_ref
from .adapter import WorktrunkAdapter

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


async def cleanup_plan(settings: ServerSettings, *, min_age_days: int = 1) -> dict[str, object]:
    """Only recommend clean, authorized, locally ancestor-integrated checkouts."""
    if not 0 <= min_age_days <= 365:
        raise ToolError("min_age_days must be between 0 and 365.")
    _worktrees.require_permission(settings)
    repo = GitExecution(settings)
    _, main_head = await resolve_local_ref(repo, "main")

    def report(
        candidates: list[dict[str, object]], skipped: int, *, complete: bool, reason: str | None
    ) -> dict[str, object]:
        return {
            "project": str(repo.root),
            "target_branch": "main",
            "target_head": main_head,
            "min_age_days": min_age_days,
            "candidates": candidates,
            "omitted_count": skipped,
            "complete": complete,
            "reason": reason,
            "write_requested": False,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    if "wt" not in settings.policy.allowed_commands:
        return report([], 0, complete=False, reason="worktrunk_not_authorized")
    if shutil.which("wt", path=dict(settings.environment).get("PATH")) is None:
        return report([], 0, complete=False, reason="worktrunk_unavailable")

    adapter = WorktrunkAdapter(settings)
    try:
        preview = await adapter.preview_prune(min_age_days)
    except ToolError:
        return report([], 0, complete=False, reason="prune_preview_unavailable")
    seen: set[str] = set()
    selected: list[dict[str, object]] = []
    skipped = 0
    for item in preview:
        branch = item.get("branch")
        if not isinstance(branch, str) or branch in seen or branch == "main":
            skipped += 1
            continue
        seen.add(branch)
        try:
            inspected = await adapter.inspect(branch)
        except ToolError:
            skipped += 1
            continue
        if inspected["state"] != "ready" or inspected["registered"] is not True:
            skipped += 1
            continue
        head = inspected["head"]
        path = inspected["path"]
        # A dry-run row is only a hint: bind it to Git's exact registered checkout,
        # not merely to a branch that happens to be clean and integrated.
        if (
            not isinstance(head, str)
            or not isinstance(path, str)
            or path == "."
            or item.get("path") != str(repo.root / path)
        ):
            skipped += 1
            continue
        result = await repo.run(
            "merge-base",
            "--is-ancestor",
            head,
            main_head,
            operation="Validate locally integrated Worktree",
            allow_failure=True,
        )
        if result.code != 0:
            skipped += 1
            continue  # Squash/content-equivalence is not sufficient for automatic eligibility.
        branch_deleted = item.get("branch_deleted")
        selected.append(
            {
                "branch": branch,
                "head": head,
                "path": path,
                "branch_deleted_by_worktrunk": branch_deleted if isinstance(branch_deleted, bool) else None,
                "reason": "ancestor_of_main",
                "action": "review_before_removal",
            }
        )
    _, latest_main = await resolve_local_ref(repo, "main")
    if latest_main != main_head:
        raise ToolError("Main moved during cleanup preview; discard the candidates.")
    for candidate in selected:
        branch = candidate["branch"]
        if not isinstance(branch, str):
            raise ToolError("Invalid cleanup candidate.")
        now = await adapter.inspect(branch)
        if (
            now["state"] != "ready"
            or now["registered"] is not True
            or now["head"] != candidate["head"]
            or now["path"] != candidate["path"]
        ):
            raise ToolError("A candidate changed during cleanup preview; reobserve before acting.")
    # The final checkout status checks can themselves race an independent main update.
    _, final_main = await resolve_local_ref(repo, "main")
    if final_main != main_head:
        raise ToolError("Main moved during cleanup preview; discard the candidates.")
    return report(selected, skipped, complete=True, reason=None)
