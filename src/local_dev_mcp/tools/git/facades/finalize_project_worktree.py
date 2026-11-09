"""Finalize an owned, verified merged-PR worktree without force or data loss."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...gh import pr_merge
from .. import _worktrees
from .._execution import GitExecution
from .._remote import valid_sha
from ..commands import worktree
from . import _pr_merge, _repository
from ._pr_merge import PRNumber
from ._refs import BranchName, ExpectedHead, resolve_local_ref
from .remote_pr import remote_readiness

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class FinalizedWorktree(TypedDict):
    project: str
    branch: str
    path: str
    status: Literal["finalized"]
    worktree_removed: bool
    branch_deleted: bool
    receipt_removed: bool
    merged_pr_number: int
    merge_sha: str
    observed_at: str


async def _branch_head(repo: GitExecution, branch: str) -> str | None:
    exists = await repo.run(
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        operation="Observe cleanup branch",
        allow_failure=True,
    )
    if exists.code == 1:
        return None
    if exists.code != 0:
        raise ToolError("Cannot determine whether the cleanup branch exists.")
    kind, head = await resolve_local_ref(repo, branch)
    if kind != "branch":
        raise ToolError("The cleanup ref does not identify a local branch.")
    return head


def _receipt_bytes(path: Path, branch: str) -> bytes:
    if not _worktrees.read_receipt(path, branch):
        raise ToolError("Missing or invalid managed preparation receipt; refuse cleanup.")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ToolError("Cannot observe the managed preparation receipt.") from error


@dataclass(frozen=True)
class FinalizeProjectWorktreeTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        merged_pr_number: PRNumber,
        delete_branch: bool = False,
    ) -> FinalizedWorktree:
        _worktrees.require_permission(self.settings, write=True)
        expected = valid_sha(expected_branch_head)
        fingerprint = await _worktrees.original_snapshot(
            self.settings, expected_branch, expected_head, allow_dirty=True
        )
        repo = GitExecution(self.settings)
        path, receipt = _worktrees.locations(self.settings, branch, write=True)
        if expected_branch == branch:
            raise ToolError("A branch currently checked out by the controller cannot be finalized.")
        observed = await _worktrees.inspect(self.settings, branch)
        state = observed["state"]
        if state == "absent":
            raise ToolError("No managed worktree or receipt exists for the requested branch.")
        if state == "ready":
            if observed["clean"] is not True or observed["head"] != expected:
                raise ToolError("Target worktree is dirty or changed; no cleanup was attempted.")
        elif state != "partial" or path.exists() or path.is_symlink() or observed["registered"]:
            raise ToolError("Partial worktree retains files or Git registration; investigate manually.")
        receipt_before = _receipt_bytes(receipt, branch)
        local_head = await _branch_head(repo, branch)
        if local_head is not None and local_head != expected:
            raise ToolError("The local branch no longer matches the observed merged PR source.")
        if state == "ready" and local_head is None:
            raise ToolError("A registered worktree lost its branch ref; do not remove it.")
        if state == "ready":
            target = GitExecution(_worktrees.scoped_settings(self.settings, path))
            ignored = await target.run(
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                operation="Check ignored worktree contents",
            )
            if ignored.output:
                raise ToolError("Ignored files remain in the managed worktree; preserve them before cleanup.")

        # A completed PR is not sufficient without exact source identity and
        # authoritative parent/message verification of the squash commit.
        repository, run = await _pr_merge.context(self.settings, "git.remote.read")
        try:
            verified = await pr_merge.inspect_merged(run, repository, merged_pr_number, branch, expected)
        except pr_merge.MergeCommandError, ValueError:
            raise ToolError("Merged PR provenance could not be verified; no cleanup was attempted.") from None
        if verified["status"] != "merged_verified" or verified["merge_sha"] is None:
            raise ToolError("The PR merge result is not fully verified; no cleanup was attempted.")

        removed = False
        deleted = False
        async with self.settings.tasks.exclusive_git_cleanup():
            await _worktrees.confirm_original(
                self.settings, expected_branch, expected_head, fingerprint, allow_dirty=True
            )
            refreshed = await _worktrees.inspect(self.settings, branch)
            if state == "ready":
                if refreshed["state"] != "ready" or refreshed["clean"] is not True or refreshed["head"] != expected:
                    raise ToolError("Managed worktree changed during preflight; inspect before removal.")
            elif refreshed["state"] != "partial" or path.exists() or refreshed["registered"]:
                raise ToolError("Orphan receipt state changed; inspect before removal.")
            if _receipt_bytes(receipt, branch) != receipt_before:
                raise ToolError("Preparation receipt changed; no cleanup was attempted.")
            current_head = await _branch_head(repo, branch)
            if current_head != local_head:
                raise ToolError("Cleanup branch moved; reobserve before removal.")
            if state == "ready":
                target = GitExecution(_worktrees.scoped_settings(self.settings, path))
                ignored = await target.run(
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "-z",
                    operation="Recheck ignored worktree contents",
                )
                if ignored.output:
                    raise ToolError("Ignored files appeared during cleanup preflight.")
                await repo.run(
                    "worktree",
                    "remove",
                    "--",
                    str(path),
                    operation="Remove verified clean managed worktree",
                    write=True,
                )
                removed = True
                if path.exists() or any(row.path == path for row in await worktree.list_registered(repo)):
                    raise ToolError("Worktree removal could not be verified; inspect retained state.")
            if delete_branch and local_head is not None:
                if await _branch_head(repo, branch) != expected:
                    raise ToolError("Branch changed after worktree removal; do not delete it.")
                await repo.run(
                    "branch",
                    "-D",
                    "--",
                    branch,
                    operation="Delete exact verified merged topic branch",
                    write=True,
                )
                deleted = True
            await _worktrees.confirm_original(
                self.settings, expected_branch, expected_head, fingerprint, allow_dirty=True
            )
            if _receipt_bytes(receipt, branch) != receipt_before:
                raise ToolError("Preparation receipt changed after worktree removal.")
            try:
                receipt.unlink()
            except OSError as error:
                raise ToolError("Cleanup is partial: receipt could not be removed; inspect before retrying.") from error
            after = await _worktrees.inspect(self.settings, branch)
            if after["state"] != "absent":
                raise ToolError("Cleanup may be partial; inspect branch and worktree independently.")
        return {
            "project": str(repo.root),
            "branch": branch,
            "path": path.relative_to(repo.root).as_posix(),
            "status": "finalized",
            "worktree_removed": removed,
            "branch_deleted": deleted,
            "receipt_removed": True,
            "merged_pr_number": merged_pr_number,
            "merge_sha": verified["merge_sha"],
            "observed_at": _repository.observed_at(),
        }


FINALIZE_PROJECT_WORKTREE: Final = ToolDefinition(
    name="finalize_project_worktree",
    description=(
        "Finalize an exact managed worktree after an authoritative merged PR's branch, source SHA, "
        "squash parent and Source-PR/Source-Head message are verified. "
        "Refuses dirty or ignored target files, foreign/partial registration and active jobs. "
        "A safely orphaned preparation receipt is removable when both directory and Git registration are absent. "
        "delete_branch=true explicitly deletes only the exact verified merged topic branch. "
        "No force-removal, stash, reset, remote ref deletion or implicit main update. "
        "Interrupted cleanup may be partial: inspect state before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "git.worktree.write", "gh", "git.remote.read"),
    create_runtime=FinalizeProjectWorktreeTool,
    readiness=remote_readiness,
)
