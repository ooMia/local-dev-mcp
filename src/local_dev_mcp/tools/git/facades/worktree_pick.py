"""Guarded Git cherry-pick and signed commit in an authorized linked Worktrunk checkout.

The controller may be dirty. Its exact contents are fingerprinted before and
after each operation; only the registered target checkout may be modified.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktrees
from .._execution import GitExecution, text_output
from .._prepare import commit_readiness, git_readiness, prepare_git_commit
from ..commands import revisions, status
from ..worktrunk import WorktrunkAdapter
from . import _repository
from ._refs import BranchName, ExpectedHead, resolve_local_ref
from .cherry_pick_project_commit import CherryPickProjectCommitTool, Fingerprint, FullSHA, PickResult
from .commit_project_changes import CommitMessage, CommitProjectChangesTool, ProjectCommit, commit_commands
from .get_project_pick_state import GetProjectPickStateTool, ProjectPickState

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class WorktreePickState(ProjectPickState):
    worktree_path: str


class WorktreePickResult(PickResult):
    branch: str
    worktree_path: str


class WorktreePickCommit(ProjectCommit):
    branch: str
    worktree_path: str


async def _selected(
    settings: ServerSettings, branch: str, expected_head: str, *, write: bool
) -> tuple[ServerSettings, str]:
    """Resolve and pin a Git-registered target, never a caller-supplied path."""
    _worktrees.require_permission(settings, write=write, backend_commands=("wt",) if write else ())
    observed = await WorktrunkAdapter(settings).inspect(branch)
    relative = observed.get("path")
    if (
        observed.get("state") not in ("ready", "dirty")
        or observed.get("head") != expected_head
        or observed.get("branch_head") != expected_head
        or not isinstance(relative, str)
    ):
        raise ToolError("The selected linked checkout is unavailable, unregistered or moved.")
    path = settings.policy.root / relative
    if write:
        try:
            settings.policy.require_path_access(path, write=True)
        except PermissionError as error:
            raise ToolError(str(error)) from None
    target = _worktrees.scoped_settings(settings, path)
    target_repo = GitExecution(target)
    await _repository.verify_root(target_repo)
    actual_branch, actual_head, _ = await status.read_porcelain_v2(target_repo)
    _, ref_head = await resolve_local_ref(GitExecution(settings), branch)
    if actual_branch != branch or actual_head != expected_head or ref_head != expected_head:
        raise ToolError("Worktree branch or HEAD changed; reobserve the selected checkout.")
    return target, relative


@dataclass(frozen=True)
class GetProjectWorktreePickStateTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
    ) -> WorktreePickState:
        snapshot = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head, allow_dirty=True)
        try:
            target, relative = await _selected(self.settings, branch, expected_branch_head, write=False)
            result = await GetProjectPickStateTool(target).execute()
            if result["branch"] != branch or result["head"] != expected_branch_head:
                raise ToolError("Selected checkout moved during pick state inspection.")
            return {**result, "project": str(self.settings.policy.root), "worktree_path": relative}
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, snapshot, allow_dirty=True)


@dataclass(frozen=True)
class CherryPickProjectWorktreeCommitTool:
    settings: ServerSettings

    async def execute(
        self,
        commit: FullSHA,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_index_fingerprint: Fingerprint,
        allow_staged: bool = False,
    ) -> WorktreePickResult:
        _worktrees.require_permission(self.settings, write=True, backend_commands=("wt",))
        snapshot = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head, allow_dirty=True)
        try:
            target, relative = await _selected(self.settings, branch, expected_branch_head, write=True)
            result = await CherryPickProjectCommitTool(target).execute(
                commit, branch, expected_branch_head, expected_index_fingerprint, allow_staged
            )
            # Allow a failed/conflicted pick to remain observable: never reset/abort.
            await _selected(self.settings, branch, expected_branch_head, write=False)
            return {
                **result,
                "project": str(self.settings.policy.root),
                "branch": branch,
                "worktree_path": relative,
            }
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, snapshot, allow_dirty=True)


@dataclass(frozen=True)
class CommitProjectWorktreePickTool:
    settings: ServerSettings

    async def execute(
        self,
        message: CommitMessage,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_index_fingerprint: Fingerprint,
        expected_staged_paths: _repository.ProjectPaths,
    ) -> WorktreePickCommit:
        _worktrees.require_permission(self.settings, write=True, backend_commands=("wt",))
        snapshot = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head, allow_dirty=True)
        try:
            target, relative = await _selected(self.settings, branch, expected_branch_head, write=True)
            repo = GitExecution(target)
            await _repository.require_writable_state(repo)
            _, _, entries = await status.read_porcelain_v2(repo)
            if any(entry["worktree_status"] not in (".", "?") for entry in entries):
                raise ToolError("Unstaged changes or conflicts prevent a reviewed worktree commit.")
            if any(entry["kind"] == "untracked" for entry in entries):
                raise ToolError("Untracked files prevent a reviewed worktree commit.")
            if await revisions.index_fingerprint(repo) != expected_index_fingerprint:
                raise ToolError("Target index changed; re-read the selected Worktree pick state.")
            # Pin exactly the staged tree before hooks or signing run. Hooks may mutate
            # the index; a created commit is never silently accepted if its bytes drift.
            reviewed_tree = text_output(
                (await repo.run("write-tree", operation="Snapshot reviewed target index", write=True)).output
            ).strip()
            prepared = await prepare_git_commit(target)
            result = await CommitProjectChangesTool(target).commit(
                message,
                prepared,
                expected_branch=branch,
                expected_head=expected_branch_head,
                expected_index_fingerprint=expected_index_fingerprint,
                expected_staged_paths=expected_staged_paths,
            )
            after = result["head_after"]
            if not result["head_observed"] or after is None:
                raise ToolError("Commit may have succeeded; inspect the target HEAD before retrying.")
            await revisions.require_parent(repo, after, expected_branch_head)
            if await revisions.tree(repo, after) != reviewed_tree:
                raise ToolError(
                    "Commit created with different Git tree than reviewed staging; inspect before retrying."
                )
            if result["signing_requested"] and result["signature_verified"] is not True:
                raise ToolError(
                    "Commit created but its configured signature was not verified; inspect before retrying."
                )
            changes = await revisions.changed_paths(repo, expected_branch_head, after)
            changed = {name for entry in changes for name in (entry.path, entry.original_path) if name is not None}
            if changed != set(expected_staged_paths):
                raise ToolError("Committed paths differ from reviewed staging; inspect before proceeding.")
            await _selected(self.settings, branch, after, write=True)
            _, current_head, remaining = await status.read_porcelain_v2(repo)
            if current_head != after or remaining:
                raise ToolError("Commit created, but target is not clean; inspect before proceeding.")
            return {
                **result,
                "project": str(self.settings.policy.root),
                "branch": branch,
                "worktree_path": relative,
            }
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, snapshot, allow_dirty=True)


GET_PROJECT_WORKTREE_PICK_STATE: Final = ToolDefinition(
    name="get_project_worktree_pick_state",
    description=(
        "Read exact branch, HEAD and index fingerprint from one Git-registered linked Worktrunk checkout. "
        "Allow a dirty controller and staged target; verify controller snapshot unchanged. "
        "No arbitrary paths, commands or writes; no wt executable required."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectWorktreePickStateTool,
    readiness=git_readiness,
)

CHERRY_PICK_PROJECT_WORKTREE_COMMIT: Final = ToolDefinition(
    name="cherry_pick_project_worktree_commit",
    description=(
        "Apply exactly one observed non-merge commit into a registered Worktrunk checkout using "
        "cherry-pick --no-commit, with expected controller/target HEAD and target index fingerprint. "
        "Reuse protected-path and ignored/untracked collision guards; preserve a dirty controller. "
        "No automatic commit, reset, abort, retry or conflict resolution. Inspect uncertain outcomes."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=CherryPickProjectWorktreeCommitTool,
    readiness=git_readiness,
)

COMMIT_PROJECT_WORKTREE_PICK: Final = ToolDefinition(
    name="commit_project_worktree_pick",
    description=(
        "Commit only explicitly reviewed staged paths in a registered Worktrunk checkout. "
        "Require exact controller/target HEAD, target index fingerprint and staged path set. "
        "Use startup commit identity/signing and hooks; verify parent, changed paths, signature receipt "
        "and target clean state. Preserve dirty controller; never reset, amend or retry on uncertainty."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=lambda settings: (*commit_commands(settings), "wt", "git.worktree.write"),
    create_runtime=CommitProjectWorktreePickTool,
    readiness=commit_readiness,
)
