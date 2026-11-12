"""Worktrunk-owned preparation of an existing main branch in a separate checkout."""

from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ....settings import protected_name
from .. import _worktrees
from .._execution import GitExecution, text_output
from .._remote import valid_sha
from ..commands import status, worktree
from ..facades._refs import resolve_local_ref
from .adapter import WorktrunkAdapter, require_worktrunk_access

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


def _main_path(settings: ServerSettings) -> Path:
    path = settings.policy.root / ".lab" / "wt" / "main"
    for candidate in (path.parent.parent, path.parent, path):
        if candidate.is_symlink():
            raise ToolError("Main Worktrunk paths cannot traverse symlinks.")
        if candidate != path and candidate.exists() and not candidate.is_dir():
            raise ToolError("Main Worktrunk parent must be a directory.")
        try:
            settings.policy.require_path_access(candidate, write=True)
        except PermissionError as error:
            raise ToolError("Main Worktrunk path is outside the authorized workspace.") from error
    return path


async def prepare_main(
    settings: ServerSettings,
    original_branch: str,
    original_head: str,
    expected_main_head: str,
) -> dict[str, str]:
    """Create main checkout only when absent; leave original worktree and refs untouched."""
    require_worktrunk_access(settings, write=True)
    repo = GitExecution(settings)
    fingerprint = await _worktrees.original_snapshot(settings, original_branch, original_head, allow_dirty=True)
    _, main_head = await resolve_local_ref(repo, "main")
    if main_head != valid_sha(expected_main_head):
        raise ToolError("Main changed; no Worktrunk checkout attempted.")

    path = _main_path(settings)
    if path.exists() or path.is_symlink():
        raise ToolError("Main Worktrunk path is occupied; no files changed.")
    records = await worktree.list_registered(repo)
    if any(row.branch == "refs/heads/main" or row.path == path for row in records):
        raise ToolError("Main is already checked out or the Worktrunk path is registered.")
    await worktree.require_ignored(repo, path.relative_to(settings.policy.root).as_posix() + "/")
    tree = await repo.run("ls-tree", "-r", "--name-only", "-z", main_head, operation="Inspect main checkout files")
    for name in filter(None, text_output(tree.output).split("\0")):
        if any(protected_name(part) for part in name.split("/")):
            raise ToolError("Main tree contains protected paths.")
        if not settings.policy.allows_path(path / name):
            raise ToolError("Main tree extends beyond the authorized checkout scope.")
    await WorktrunkAdapter(settings).switch_main_checkout(path)
    await _worktrees.confirm_original(settings, original_branch, original_head, fingerprint, allow_dirty=True)
    after = await worktree.list_registered(repo)
    exact = [row for row in after if row.path == path and row.branch == "refs/heads/main" and not row.unavailable]
    if len(exact) != 1:
        raise ToolError("Main checkout may have been created; inspect Git registration before retrying.")
    common = await worktree.common_directory(repo)
    if not _worktrees.bound_git_pointer(path, common):
        raise ToolError("Main checkout Git pointer is not bound to the selected repository.")
    target = GitExecution(_worktrees.scoped_settings(settings, path))
    if await worktree.common_directory(target) != common:
        raise ToolError("Main checkout uses an unexpected repository.")
    branch, checkout_head, dirty = await status.read_porcelain_v2(target)
    _, latest_main = await resolve_local_ref(repo, "main")
    if branch != "main" or checkout_head != main_head or latest_main != main_head or dirty:
        raise ToolError("Main checkout changed during preparation; inspect both worktrees.")
    return {"branch": "main", "head": main_head, "path": path.relative_to(settings.policy.root).as_posix()}
