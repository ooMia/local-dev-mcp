"""Switch only between local branches when tracked files and index are clean."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import status
from . import _repository
from ._refs import BranchName, ExpectedHead, resolve_local_ref
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class SwitchedBranch(TypedDict):
    project: str
    branch_before: str
    branch_after: str
    head_before: str
    head_after: str
    untracked_paths_unchanged: bool
    observed_at: str


@dataclass(frozen=True)
class SwitchProjectBranchTool:
    settings: ServerSettings

    async def execute(
        self,
        target_branch: BranchName,
        expected_target_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
    ) -> SwitchedBranch:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        before_branch, before_head, before_entries = await status.read_porcelain_v2(repo)
        if before_branch is None or before_head is None:
            raise ToolError("A checked-out branch and existing HEAD commit are required.")
        if before_branch != expected_branch or before_head != expected_head:
            raise ToolError("The repository baseline changed. Re-read status before switching.")
        if any(
            entry["index_status"] not in (".", "?") or entry["worktree_status"] not in (".", "?")
            for entry in before_entries
        ):
            raise ToolError("Tracked files and Git index must be clean before switching branches.")
        kind, target_sha = await resolve_local_ref(repo, target_branch)
        if kind != "branch" or target_sha != expected_target_head:
            raise ToolError("The target branch changed. Re-read its commit SHA before switching.")
        if target_branch == before_branch:
            raise ToolError("The requested branch is already checked out.")

        # Git itself prevents untracked collisions. Also protect ignored paths.
        await repo.run(
            "switch",
            "--no-guess",
            "--no-overwrite-ignore",
            target_branch,
            operation="Switch local branch",
            write=True,
            capture=False,
        )
        try:
            after_branch, after_head, after_entries = await status.read_porcelain_v2(repo)
        except ToolError as error:
            raise ToolError(
                "Branch switching may have succeeded but the resulting state could not be verified. "
                "Inspect branch, HEAD and existing edits."
            ) from error
        if after_branch is None or after_head is None or after_branch != target_branch or after_head != target_sha:
            raise ToolError("Post-switch branch or HEAD differs from the requested target. Inspect project status.")
        before_untracked = {entry["path"] for entry in before_entries if entry["index_status"] == "?"}
        after_untracked = {entry["path"] for entry in after_entries if entry["index_status"] == "?"}
        if not before_untracked.issubset(after_untracked):
            raise ToolError(
                "The switch completed but a previously untracked path is no longer reported as untracked. "
                "Inspect user files before further writes."
            )
        return {
            "project": str(repo.root),
            "branch_before": before_branch,
            "branch_after": after_branch,
            "head_before": before_head,
            "head_after": after_head,
            "untracked_paths_unchanged": before_untracked == after_untracked,
            "observed_at": observed_at(),
        }


SWITCH_PROJECT_BRANCH: Final = ToolDefinition(
    name="switch_project_branch",
    description=(
        "Switch to an existing local branch using observed source and target SHAs. "
        "Requires a clean tracked working tree and index. Permits noncolliding untracked files. "
        "No forced checkout, stash, reset or automatic recovery. Inspect status after an error."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=SwitchProjectBranchTool,
    readiness=git_readiness,
)
