"""Review committed and local changes of one pinned, authorized Git worktree."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from .. import _worktrees
from .._execution import GitExecution
from ..commands import status
from ..facades import _visibility
from ..facades._refs import resolve_local_ref
from ..facades.compare_project_refs import CompareProjectRefsTool
from ..facades.get_project_diff import GetProjectDiffTool
from .adapter import WorktrunkAdapter

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


async def review(
    settings: ServerSettings,
    *,
    branch: str,
    expected_head: str,
    include_patch: bool = False,
    base_branch: str = "main",
) -> dict[str, object]:
    """Keep review policy in shared Git diff commands, never use wt merge or stage."""
    _worktrees.require_permission(settings)
    repo = GitExecution(settings)
    _, base_head = await resolve_local_ref(repo, base_branch)
    _, branch_head = await resolve_local_ref(repo, branch)
    if branch_head != expected_head:
        raise ToolError("The requested Worktree branch HEAD moved. Reobserve before reviewing.")
    observed = await WorktrunkAdapter(settings).inspect(branch)
    if observed["state"] not in ("ready", "dirty") or observed["head"] != expected_head:
        raise ToolError("The requested Worktree is absent, inaccessible or no longer at the expected HEAD.")
    relative = observed["path"]
    if not isinstance(relative, str):
        raise ToolError("The Worktree inspection did not provide an authorized checkout path.")
    target = _worktrees.scoped_settings(settings, settings.policy.root / relative)
    target_git = GitExecution(target)

    comparison = await CompareProjectRefsTool(settings).execute(base_branch, branch, base_head, expected_head)
    staged = await GetProjectDiffTool(target).execute(staged=True)
    unstaged = await GetProjectDiffTool(target).execute(staged=False)
    checked_branch, checked_head, changes = await status.read_porcelain_v2(target_git)
    visible: list[status.StatusEntry] = []
    excluded = False
    for entry in changes:
        paths = (entry["path"],) if entry["original_path"] is None else (entry["path"], entry["original_path"])
        if all(_visibility.readable(target_git, path) for path in paths):
            visible.append(entry)
        else:
            excluded = True

    after = await WorktrunkAdapter(settings).inspect(branch)
    _, latest_base = await resolve_local_ref(repo, base_branch)
    if (
        after["state"] not in ("ready", "dirty")
        or after["head"] != expected_head
        or after["path"] != relative
        or checked_head != expected_head
        or checked_branch != branch
        or latest_base != base_head
    ):
        raise ToolError("The Worktree or base branch changed during review; discard the observation.")
    complete = comparison["complete"] and staged["complete"] and unstaged["complete"] and not excluded
    patches = {
        "committed": comparison["patch"] if include_patch else "",
        "staged": staged["patch"] if include_patch else "",
        "unstaged": unstaged["patch"] if include_patch else "",
    }
    if include_patch and sum(len(value.encode("utf-8")) for value in patches.values()) > 262144:
        raise ToolError("The combined review patch exceeds 256 KiB. Use narrower existing diff tools.")
    return {
        "project": str(repo.root),
        "branch": branch,
        "base_branch": base_branch,
        "base_head": base_head,
        "head": branch_head,
        "path": relative,
        "state": after["state"],
        "committed_changes": comparison["changes"],
        "base_only_commits": comparison["base_only_commits"],
        "branch_only_commits": comparison["target_only_commits"],
        "staged": sum(entry["index_status"] not in (".", "?", "!") for entry in visible),
        "unstaged": sum(entry["worktree_status"] not in (".", "?", "!") for entry in visible),
        "untracked": sum(entry["kind"] == "untracked" for entry in visible),
        "conflicted": any(entry["kind"] == "unmerged" for entry in visible),
        "patches": patches,
        "complete": bool(complete),
        "reason": None if complete else "restricted_or_unavailable_paths",
        "observed_at": datetime.now(UTC).isoformat(),
    }
