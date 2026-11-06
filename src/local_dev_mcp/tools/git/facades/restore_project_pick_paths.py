"""Discard explicitly selected staged migration paths back to HEAD, preserving all others."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import revisions, status
from . import _repository, _visibility
from ._repository import ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type FullSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
type Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RestoredPickPaths(TypedDict):
    project: str
    paths: list[str]
    head_unchanged: bool
    index_fingerprint: str
    observed_at: str


@dataclass(frozen=True)
class RestoreProjectPickPathsTool:
    settings: ServerSettings

    async def execute(
        self,
        paths: ProjectPaths,
        expected_branch: str,
        expected_head: FullSHA,
        expected_index_fingerprint: Fingerprint,
    ) -> RestoredPickPaths:
        repo = GitExecution(self.settings)
        selected = _repository.check_paths(repo, paths)
        if "." in selected or any(p.endswith("/") for p in selected):
            raise ToolError("Specify individual staged file paths; whole-tree restoration is disallowed.")
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        before_branch, before_head, before = await status.read_porcelain_v2(repo)
        if before_branch != expected_branch or before_head != expected_head or before_branch is None:
            raise ToolError("Git branch or HEAD changed. Re-read pick state.")
        if await revisions.index_fingerprint(repo) != expected_index_fingerprint:
            raise ToolError("Git index changed. Re-read pick state before restoration.")
        staged = {e["path"] for e in before if e["index_status"] not in (".", "?")}
        if not set(selected).issubset(staged):
            raise ToolError("Every restoration path must have an observed staged change.")
        if any(e["worktree_status"] not in (".", "?") for e in before):
            raise ToolError("Unstaged tracked changes prevent safe selected restoration.")
        _visibility.require_selection(repo, selected)
        await repo.run(
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "--",
            *selected,
            operation="Restore selected migration paths",
            write=True,
            capture=False,
        )
        after_branch, after_head, _ = await status.read_porcelain_v2(repo)
        if after_branch != before_branch or after_head != before_head:
            raise ToolError("Restoration completed but Git HEAD or branch changed. Inspect state.")
        return {
            "project": str(repo.root),
            "paths": selected,
            "head_unchanged": True,
            "index_fingerprint": await revisions.index_fingerprint(repo),
            "observed_at": observed_at(),
        }


RESTORE_PROJECT_PICK_PATHS: Final = ToolDefinition(
    name="restore_project_pick_paths",
    description=(
        "Explicitly discard selected staged paths from cherry-pick back to HEAD in index and working tree. "
        "Requires matching branch/HEAD/index snapshot, staged paths and no unstaged tracked edits. "
        "Never restore the whole tree or untracked files; failure is not automatically rolled back."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git",),
    create_runtime=RestoreProjectPickPathsTool,
    readiness=git_readiness,
)
