"""Create a local branch from an observed commit without switching the working tree."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import status
from . import _repository
from ._refs import BranchName, ExpectedHead, require_absent_branch, resolve_local_ref
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class CreatedBranch(TypedDict):
    project: str
    new_branch: str
    base_ref: str
    head: str
    branch_unchanged: bool
    observed_at: str


@dataclass(frozen=True)
class CreateProjectBranchFromRefTool:
    settings: ServerSettings

    async def execute(
        self,
        new_branch: BranchName,
        base_ref: str,
        expected_base_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
    ) -> CreatedBranch:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        before_branch, before_head, _ = await status.read_porcelain_v2(repo)
        if before_branch is None or before_head is None:
            raise ToolError("A checked-out branch and existing HEAD commit are required.")
        if before_branch != expected_branch or before_head != expected_head:
            raise ToolError("The repository baseline changed. Re-read status before a write.")
        _, base_sha = await resolve_local_ref(repo, base_ref)
        if base_sha != expected_base_head:
            raise ToolError("The base ref moved or its observed SHA does not match. Re-read the ref.")
        await require_absent_branch(repo, new_branch)

        # The explicit SHA pins the creation point even if base_ref moves after this check.
        await repo.run(
            "branch",
            "--no-track",
            new_branch,
            base_sha,
            operation="Create local branch without checkout",
            write=True,
            capture=False,
        )
        try:
            _, observed_sha = await resolve_local_ref(repo, new_branch)
            after_branch, after_head, _ = await status.read_porcelain_v2(repo)
        except ToolError as error:
            raise ToolError(
                "Branch creation may have succeeded, but the resulting state could not be verified. "
                "Inspect the ref and project status before retrying."
            ) from error
        if observed_sha != base_sha or after_branch != before_branch or after_head != before_head:
            raise ToolError("Post-creation Git state differs from the expected state. Inspect refs and status.")
        return {
            "project": str(repo.root),
            "new_branch": new_branch,
            "base_ref": base_ref,
            "head": observed_sha,
            "branch_unchanged": True,
            "observed_at": observed_at(),
        }


CREATE_PROJECT_BRANCH_FROM_REF: Final = ToolDefinition(
    name="create_project_branch_from_ref",
    description=(
        "Create an absent local branch from an observed local branch or exact commit SHA, without switching. "
        "Requires expected current branch/HEAD and base HEAD. Never overwrites a branch, commits or stashes. "
        "After a failure inspect refs/status: a branch may have been created."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=CreateProjectBranchFromRefTool,
    readiness=git_readiness,
)
