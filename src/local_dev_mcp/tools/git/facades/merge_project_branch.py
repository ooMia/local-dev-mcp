"""Safely fast-forward local main from an observed clean source branch."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition, safe_execution
from .._execution import GitExecution
from .._prepare import PreparedGitCommit, commit_readiness, prepare_git_commit
from ..commands import merge, show, status
from . import _repository
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


type ExpectedHead = Annotated[
    str,
    Field(
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
        description="Full observed commit SHA, not a branch name or abbreviated hash.",
    ),
]


class ProjectMerge(TypedDict):
    project: str
    merged: bool
    branch_before: str
    branch_after: str
    source_head: str
    main_head_before: str
    main_head_after: str
    signature_verified: bool | None
    observed_at: str


@dataclass(frozen=True)
class MergeProjectBranchTool:
    settings: ServerSettings

    @property
    def execute(self) -> Callable[..., Awaitable[ProjectMerge]]:
        @safe_execution
        async def provide() -> PreparedGitCommit:
            return await prepare_git_commit(self.settings)

        async def invoke(
            expected_source_branch: str,
            expected_source_head: ExpectedHead,
            expected_main_head: ExpectedHead,
            prepared: PreparedGitCommit = Depends(provide),
        ) -> ProjectMerge:
            return await self.merge(expected_source_branch, expected_source_head, expected_main_head, prepared)

        return invoke

    async def merge(
        self,
        expected_source_branch: str,
        expected_source_head: str,
        expected_main_head: str,
        prepared: PreparedGitCommit,
    ) -> ProjectMerge:
        if not expected_source_branch or expected_source_branch == "main" or "\0" in expected_source_branch:
            raise ToolError("Provide a checked-out source branch other than main.")
        repo = GitExecution(self.settings, prepared)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        before_branch, before_head, before_entries = await status.read_porcelain_v2(repo)
        if before_branch != expected_source_branch or before_head != expected_source_head:
            raise ToolError("Source branch or HEAD changed. Re-read project status; no merge was attempted.")
        if before_entries:
            raise ToolError("Main merge requires a clean index and Working Tree, including untracked files.")
        main_before = await merge.main_head(repo)
        if main_before != expected_main_head:
            raise ToolError("Local main moved. Re-observe its HEAD before attempting a merge.")
        await merge.require_fast_forward(repo, main_before, expected_source_head)

        signature_verified: bool | None = None
        if prepared.signing is not None:
            try:
                signature_verified = await show.verify_signature(
                    repo, expected_source_head, prepared.signing.fingerprint
                )
            except ToolError, UnicodeError:
                signature_verified = False
            if not signature_verified:
                raise ToolError(
                    "The source HEAD signature cannot be verified with the configured key; no merge was attempted."
                )

        # Git refuses switching to a branch checked out in another worktree.
        # After a successful switch, never automatically roll back on a failure.
        await merge.switch_to_main(repo)
        try:
            branch, head, entries = await status.read_porcelain_v2(repo)
            if branch != "main" or head != expected_main_head or entries:
                raise ToolError("Post-switch main baseline differs; no merge attempted. Inspect project status.")
            await merge.merge_fast_forward(repo, expected_source_head)
            after_branch, after_head, after_entries = await status.read_porcelain_v2(repo)
            if after_branch != "main" or after_head != expected_source_head or after_entries:
                raise ToolError("Merge result could not be confirmed. Inspect main, index and Working Tree.")
        except ToolError as error:
            raise ToolError(
                "Main may now be checked out and/or changed. Inspect current branch, HEAD and index; "
                "do not retry automatically. " + str(error)
            ) from None

        return {
            "project": str(repo.root),
            "merged": True,
            "branch_before": expected_source_branch,
            "branch_after": "main",
            "source_head": expected_source_head,
            "main_head_before": main_before,
            "main_head_after": expected_source_head,
            "signature_verified": signature_verified,
            "observed_at": observed_at(),
        }


def merge_commands(settings: ServerSettings) -> tuple[str, ...]:
    return settings.commit_required_commands


MERGE_PROJECT_BRANCH: Final = ToolDefinition(
    name="merge_project_branch",
    description=(
        "Fast-forward local main from the currently checked-out clean source branch. "
        "Requires exact observed source branch/HEAD and main HEAD. Verifies ancestry, "
        "requires a clean index/Working Tree, and checks the source HEAD signature when signing is enabled. "
        "Switches to main and uses git merge --ff-only; never auto-resolves conflicts, resets, pulls, pushes, "
        "rebases or creates a merge commit. On failure inspect status and HEAD; no automatic rollback."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=merge_commands,
    create_runtime=MergeProjectBranchTool,
    readiness=commit_readiness,
)
