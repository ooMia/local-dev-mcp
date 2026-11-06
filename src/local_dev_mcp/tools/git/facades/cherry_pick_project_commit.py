"""Apply one ordinary commit into the current index and working tree, without committing."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution, text_output
from .._prepare import git_readiness
from ..commands import revisions, status
from . import _repository, _visibility
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type FullSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
type Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PickResult(TypedDict):
    project: str
    commit: str
    outcome: str
    git_exit_code: int
    head_unchanged: bool
    index_fingerprint: str
    changed_paths: list[str]
    unmerged_paths: list[str]
    complete: bool
    observed_at: str
    next_action: str


@dataclass(frozen=True)
class CherryPickProjectCommitTool:
    settings: ServerSettings

    async def execute(
        self,
        commit: FullSHA,
        expected_branch: str,
        expected_head: FullSHA,
        expected_index_fingerprint: Fingerprint,
        allow_staged: bool = False,
    ) -> PickResult:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        branch, head, before = await status.read_porcelain_v2(repo)
        if branch != expected_branch or head != expected_head or branch is None:
            raise ToolError("Git branch or HEAD changed. Re-read pick state.")
        if any(e["worktree_status"] not in (".", "?") for e in before):
            raise ToolError("Unstaged tracked changes or conflicts prevent selective cherry-pick.")
        if not allow_staged and any(e["index_status"] not in (".", "?") for e in before):
            raise ToolError("Initial cherry-pick requires a clean index; use allow_staged for reviewed continuation.")
        if await revisions.index_fingerprint(repo) != expected_index_fingerprint:
            raise ToolError("Git index changed. Re-read pick state before applying a commit.")
        resolved = await revisions.resolve_commit(repo, commit)
        affected = await revisions.commit_paths(repo, resolved)
        if any(not _visibility.readable(repo, p) for p in affected):
            raise ToolError("The commit touches a protected path; no cherry-pick was attempted.")
        # Git may silently overwrite ignored files during cherry-pick. Check the
        # entire target path set against the index, not just porcelain status.
        tracked_result = await repo.run(
            "ls-files",
            "--cached",
            "-z",
            operation="Inspect tracked paths before cherry-pick",
        )
        raw_paths = text_output(tracked_result.output)
        if raw_paths and not raw_paths.endswith("\0"):
            raise ToolError("Git returned incomplete tracked paths; no cherry-pick was attempted.")
        tracked: set[str] = set(raw_paths[:-1].split("\0")) if raw_paths else set()
        for path in affected:
            candidate = repo.root / path
            if path not in tracked and (candidate.exists() or candidate.is_symlink()):
                raise ToolError(
                    "The commit would replace an existing untracked or ignored file. No cherry-pick was attempted."
                )
        # Git cherry-pick can overwrite ignored, untracked user files. Check both
        # ignored and ordinary untracked entries at every affected source path.
        if affected:
            collisions = await repo.run(
                "ls-files",
                "--others",
                "-z",
                "--",
                *affected,
                operation="Check cherry-pick untracked collisions",
            )
            if collisions.output:
                raise ToolError(
                    "The commit overlaps untracked or ignored user files. "
                    "Cherry-pick was not attempted; review those paths without deleting them."
                )
        # No --abort, reset, retry or commit.
        result = await repo.run(
            "cherry-pick",
            "--no-commit",
            resolved,
            operation="Apply one commit without committing",
            write=True,
            capture=False,
            allow_failure=True,
        )
        try:
            after_branch, after_head, after = await status.read_porcelain_v2(repo)
            fingerprint = await revisions.index_fingerprint(repo)
        except ToolError as error:
            raise ToolError(
                "Cherry-pick execution ended, but post-apply state could not be confirmed. "
                "Inspect Git status, index, and operation state before taking any action."
            ) from error
        if after_branch != branch or after_head != head:
            raise ToolError("HEAD or branch moved during cherry-pick; inspect state before proceeding.")
        visible = [e for e in after if _visibility.readable(repo, e["path"])]
        merged = [e["path"] for e in visible if e["kind"] == "unmerged"]
        return {
            "project": str(repo.root),
            "commit": resolved,
            "outcome": "applied" if result.code == 0 else "failed_or_conflicted",
            "git_exit_code": result.code,
            "head_unchanged": True,
            "index_fingerprint": fingerprint,
            "changed_paths": [e["path"] for e in visible if e["kind"] != "untracked"],
            "unmerged_paths": merged,
            "complete": len(visible) == len(after),
            "observed_at": observed_at(),
            "next_action": (
                "Review staged diff and remove unrelated changes before the next commit."
                if result.code == 0
                else "Inspect conflicts and index; do not retry, abort or reset automatically."
            ),
        }


CHERRY_PICK_PROJECT_COMMIT: Final = ToolDefinition(
    name="cherry_pick_project_commit",
    description=(
        "Apply one observed, non-merge full commit SHA using cherry-pick --no-commit. "
        "Require exact branch/HEAD/index state, no unstaged changes and explicit allow_staged for continuation. "
        "Reject protected paths; never auto-commit, abort, reset, stash or retry. "
        "Failure may leave a partial index or conflicts."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=CherryPickProjectCommitTool,
    readiness=git_readiness,
)
