"""Create and switch to a new local branch without discarding existing edits."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import status
from . import _repository
from ._refs import check_branch_name
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


type BranchName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description=(
            "New local branch name, e.g. wip/github-automation. "
            "ASCII letters, digits and hyphens in slash-separated segments."
        ),
    ),
]
type ExpectedHead = Annotated[
    str,
    Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", description="Exact HEAD observed before authorizing the switch."),
]


class BranchResult(TypedDict):
    project: str
    success: bool
    branch_before: str
    branch_after: str
    head_before: str
    head_after: str
    status_unchanged: bool
    observed_at: str


@dataclass(frozen=True)
class CreateProjectBranchTool:
    settings: ServerSettings

    async def execute(self, new_branch: BranchName, expected_branch: str, expected_head: ExpectedHead) -> BranchResult:
        check_branch_name(new_branch)
        if not expected_branch or "\0" in expected_branch:
            raise ToolError("Supply the currently observed branch name.")

        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        before_branch, before_head, before_entries = await status.read_porcelain_v2(repo)
        if before_branch is None or before_head is None:
            raise ToolError("A checked-out branch and existing HEAD commit are required.")
        if before_branch != expected_branch or before_head != expected_head:
            raise ToolError("The repository baseline changed. Re-read branch and HEAD before attempting any write.")
        if any(entry["index_status"] not in (".", "?") for entry in before_entries):
            raise ToolError("Unstage the index before creating a branch; no branch was created.")

        valid = await repo.run(
            "check-ref-format", "--branch", new_branch, operation="Check new branch name", allow_failure=True
        )
        if valid.code != 0:
            raise ToolError("Git rejected the proposed branch name.")
        exists = await repo.run(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{new_branch}",
            operation="Check existing branch",
            allow_failure=True,
        )
        if exists.code == 0:
            raise ToolError("The requested local branch already exists.")
        if exists.code != 1:
            raise ToolError("Could not establish whether the target branch exists; no switch was attempted.")

        # A new branch from the current HEAD retains tracked and untracked edits.
        # Git may invoke repository checkout hooks; do not claim transactional isolation.
        await repo.run(
            "switch",
            "--no-guess",
            "--no-track",
            "-c",
            new_branch,
            operation="Create and switch project branch",
            write=True,
            capture=False,
        )
        try:
            after_branch, after_head, after_entries = await status.read_porcelain_v2(repo)
        except ToolError as error:
            raise ToolError(
                "Branch switching may have succeeded but post-switch status could not be verified. "
                "Inspect branch, HEAD and edits."
            ) from error
        if after_branch is None or after_head is None or after_branch != new_branch or after_head != before_head:
            raise ToolError(
                "Post-switch branch or HEAD did not match the expected values. Inspect current branch, HEAD and edits."
            )
        return {
            "project": str(repo.root),
            "success": True,
            "branch_before": before_branch,
            "branch_after": after_branch,
            "head_before": before_head,
            "head_after": after_head,
            "status_unchanged": before_entries == after_entries,
            "observed_at": observed_at(),
        }


CREATE_PROJECT_BRANCH: Final = ToolDefinition(
    name="create_project_branch",
    description=(
        "Create and switch to an absent local branch from the current HEAD, carrying existing uncommitted changes. "
        "Requires the exact observed branch and full HEAD SHA; refuses staged changes, detached HEAD, "
        "unmerged index and ongoing Git integration operations. Does not stash, commit, push or reset. "
        "Repository checkout hooks may run. Inspect status and HEAD after failures; no automatic rollback."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=CreateProjectBranchTool,
    readiness=git_readiness,
)
