"""Compare two observed local refs without changing the index or worktree."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import revisions
from . import _repository, _visibility
from ._repository import ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


type ExpectedHead = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class ChangedPathResult(TypedDict):
    status: str
    path: str
    original_path: str | None


class ProjectComparison(TypedDict):
    project: str
    base_ref: str
    target_ref: str
    base_head: str
    target_head: str
    merge_base: str | None
    base_only_commits: int
    target_only_commits: int
    paths: list[str]
    changes: list[ChangedPathResult]
    patch: str
    complete: bool
    reason: str | None
    next_action: str | None
    observed_at: str


@dataclass(frozen=True)
class CompareProjectRefsTool:
    settings: ServerSettings

    async def execute(
        self,
        base_ref: str,
        target_ref: str,
        expected_base_head: ExpectedHead,
        expected_target_head: ExpectedHead,
        paths: Annotated[
            ProjectPaths | None,
            Field(description="Optional project-relative paths; omit for all readable changes."),
        ] = None,
    ) -> ProjectComparison:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, ["."] if paths is None else paths)
        await _repository.verify_root(repo)
        _visibility.require_selection(repo, checked)
        base = await revisions.resolve(repo, base_ref)
        target = await revisions.resolve(repo, target_ref)
        if base != expected_base_head or target != expected_target_head:
            raise ToolError("A compared ref changed. Re-observe both full SHAs before requesting a diff.")

        common, only_base, only_target = await revisions.ancestry(repo, base, target)
        all_changes = await revisions.changed_paths(repo, base, target)
        scopes = tuple(Path(path) for path in checked)
        selected_paths: list[str] = []
        visible: list[ChangedPathResult] = []
        excluded = False
        for change in all_changes:
            endpoints = (change.path,) if change.original_path is None else (change.original_path, change.path)
            selected = [path for path in endpoints if any(Path(path).is_relative_to(scope) for scope in scopes)]
            if not selected:
                continue
            if not all(_visibility.readable(repo, path) for path in endpoints):
                excluded = True
                continue
            visible.append({"status": change.status, "path": change.path, "original_path": change.original_path})
            selected_paths.extend(selected)
        result_patch = await revisions.patch(repo, base, target, list(dict.fromkeys(selected_paths)))

        # A local branch may move while the comparison runs. Never report the
        # resulting snapshot as current if either input changed during the read.
        if await revisions.resolve(repo, base_ref) != base or await revisions.resolve(repo, target_ref) != target:
            raise ToolError("A compared ref moved during the read. Discard this diff and observe both refs again.")
        return {
            "project": str(repo.root),
            "base_ref": base_ref,
            "target_ref": target_ref,
            "base_head": base,
            "target_head": target,
            "merge_base": common,
            "base_only_commits": only_base,
            "target_only_commits": only_target,
            "paths": checked,
            "changes": visible,
            "patch": result_patch,
            "complete": not excluded,
            "reason": "restricted_paths" if excluded else None,
            "next_action": _visibility.restricted_message() if excluded else None,
            "observed_at": observed_at(),
        }


COMPARE_PROJECT_REFS: Final = ToolDefinition(
    name="compare_project_refs",
    description=(
        "Compare explicit, locally available Git refs using full observed commit SHAs. "
        "Reports common ancestry, exclusive commit counts, visible changed paths and a bounded patch. "
        "Filters protected/unreadable paths and both sides of renames; incomplete scopes are flagged. "
        "Does not fetch, checkout, modify the index, or create commits. Narrow paths if the patch is too large."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=CompareProjectRefsTool,
    readiness=git_readiness,
)
