"""Commit exactly one reviewed assessment in a managed topic worktree."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktree_assessments as assessments
from .. import _worktrees
from .._execution import GitExecution, text_output
from .._prepare import commit_readiness, prepare_git_commit
from ..commands import status
from . import _repository
from ._refs import BranchName, ExpectedHead
from .commit_project_changes import CommitMessage, CommitProjectChangesTool, ProjectCommit, commit_commands
from .stage_project_paths import StageProjectPathsTool

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class WorktreeAssessmentCommit(ProjectCommit):
    branch: str
    assessment_id: str
    digest: str


async def require_only_assessment(repo: GitExecution, branch: str, head: str, path: str) -> None:
    await _repository.require_writable_state(repo)
    actual_branch, actual_head, entries = await status.read_porcelain_v2(repo)
    if actual_branch != branch or actual_head != head or len(entries) != 1:
        raise ToolError("Target changed or contains unrelated edits; inspect before committing.")
    entry = entries[0]
    if (
        entry["path"] != path
        or entry["original_path"] is not None
        or (entry["kind"] != "untracked" and (entry["index_status"], entry["worktree_status"]) != ("A", "."))
    ):
        raise ToolError("Only the new reviewed assessment may be untracked or staged; other changes are rejected.")


@dataclass(frozen=True)
class CommitProjectWorktreeAssessmentTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        assessment_id: assessments.AssessmentId,
        expected_digest: assessments.AssessmentDigest,
        message: CommitMessage,
    ) -> WorktreeAssessmentCommit:
        _worktrees.require_permission(self.settings, write=True)
        if not message.strip() or "\0" in message:
            raise ToolError("Provide a nonempty commit message without NUL.")
        fingerprint = await _worktrees.original_snapshot(self.settings, expected_branch, expected_head)
        try:
            state = await _worktrees.inspect(self.settings, branch)
            if state["state"] != "ready" or state["head"] != expected_branch_head:
                raise ToolError("Target worktree is unprepared, partial or moved; inspect it before committing.")
            root, _ = _worktrees.locations(self.settings, branch, write=True)
            target = _worktrees.scoped_settings(self.settings, root)
            path = assessments.selected_path(self.settings, target, assessment_id)
            relative = path.relative_to(root).as_posix()
            content = assessments.reviewed_bytes(path, expected_digest)
            repo = GitExecution(target)
            await assessments.require_new(repo, expected_branch_head, assessment_id, assessments.parse(content))
            await require_only_assessment(repo, branch, expected_branch_head, relative)
            prepared = await prepare_git_commit(target)
            await StageProjectPathsTool(target).execute([relative])
            await require_only_assessment(repo, branch, expected_branch_head, relative)
            staged = await repo.run("show", ":" + relative, operation="Verify staged assessment bytes")
            mode = await repo.run("ls-files", "--stage", "-z", "--", relative, operation="Verify assessment mode")
            if staged.output != content or not mode.output.startswith(b"100644 "):
                raise ToolError("Staged bytes or file mode differ from review; staged state was retained.")
            assessments.reviewed_bytes(assessments.selected_path(self.settings, target, assessment_id), expected_digest)
            await require_only_assessment(repo, branch, expected_branch_head, relative)
            result = await CommitProjectChangesTool(target).commit(
                message + "\n\nAssessment-Source: " + expected_branch_head, prepared
            )
            after = result["head_after"]
            if after is None or result["head_before"] != expected_branch_head:
                raise ToolError(
                    "Commit may exist but its exact parent/HEAD could not be confirmed; inspect before retrying."
                )
            parent = await repo.run("rev-list", "--parents", "-n", "1", after, operation="Verify assessment parent")
            changed = await repo.run(
                "diff",
                "--name-only",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                expected_branch_head,
                after,
                operation="Verify assessment commit scope",
            )
            committed = await repo.run("show", after + ":" + relative, operation="Verify committed assessment bytes")
            tree = await repo.run("ls-tree", "-z", after, "--", relative, operation="Verify committed assessment mode")
            if (
                not tree.output.startswith(b"100644 blob ")
                or text_output(parent.output).strip().split() != [after, expected_branch_head]
                or changed.output != relative.encode() + b"\0"
                or committed.output != content
            ):
                raise ToolError("A commit was created but its scope differs from review; inspect before proceeding.")
            await _worktrees.require_ready(self.settings, branch, after)
            assessments.reviewed_bytes(assessments.selected_path(self.settings, target, assessment_id), expected_digest)
            result["project"] = str(self.settings.policy.root)
            return {**result, "branch": branch, "assessment_id": assessment_id, "digest": expected_digest}
        finally:
            await _worktrees.confirm_original(self.settings, expected_branch, expected_head, fingerprint)


def required_commands(settings: ServerSettings) -> tuple[str, ...]:
    return (*commit_commands(settings), "git.worktree.write")


COMMIT_PROJECT_WORKTREE_ASSESSMENT: Final = ToolDefinition(
    name="commit_project_worktree_assessment",
    description=(
        "Stage and commit only one new managed-worktree assessment whose bytes match the reviewed SHA-256 digest. "
        "Requires exact original/target refs, clean original, no unrelated target edits and startup identity/signing. "
        "Never amend, bypass hooks, fall back to unsigned or reset. A failed call can leave staged bytes or a commit; "
        "inspect target HEAD and files before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=required_commands,
    create_runtime=CommitProjectWorktreeAssessmentTool,
    readiness=commit_readiness,
)
