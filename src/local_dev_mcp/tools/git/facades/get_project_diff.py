"""Bounded working-tree or staged patch output, without external diff drivers."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import diff
from . import _repository, _visibility
from ._repository import ProjectPaths, observed_at
from ._untracked_preview import preview_untracked

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ProjectDiff(TypedDict):
    project: str
    staged: bool
    paths: list[str]
    patch: str
    complete: bool
    reason: str | None
    next_action: str | None
    observed_at: str


@dataclass(frozen=True)
class GetProjectDiffTool:
    settings: ServerSettings

    async def execute(
        self,
        staged: Annotated[
            bool, Field(description="True reviews index versus HEAD; false reviews working tree versus index.")
        ] = False,
        paths: Annotated[
            ProjectPaths | None,
            Field(description="Literal relative paths. Omitted or null reviews the whole project; [] is invalid."),
        ] = None,
        include_untracked: Annotated[
            bool,
            Field(description="Opt in to bounded text previews of selected, nonignored untracked files."),
        ] = False,
    ) -> ProjectDiff:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, ["."] if paths is None else paths)
        if include_untracked and (staged or paths is None or "." in checked):
            raise ToolError("Untracked preview requires explicit narrow paths and staged=false.")
        await _repository.verify_root(repo)
        _visibility.require_selection(repo, checked)
        # A pathspec can turn a cross-scope rename into an apparent addition or
        # deletion. Inspect both sides globally before selecting requested paths.
        changes = await diff.changed_paths(repo, ["."], staged=staged)
        scopes = tuple(Path(path) for path in checked)
        visible: list[str] = []
        excluded = False
        for change in changes:
            selected = [path for path in change if any(Path(path).is_relative_to(scope) for scope in scopes)]
            if not selected:
                continue
            if all(_visibility.readable(repo, path) for path in change):
                visible.extend(selected)
            else:
                excluded = True
        # Never pass an empty selection to Git: it would mean the whole project.
        patch = await diff.patch(repo, list(dict.fromkeys(visible)), staged=staged) if visible else ""
        preview_complete = True
        if include_untracked:
            untracked_patch, preview_complete = await preview_untracked(repo, checked)
            patch += untracked_patch
        reason = "restricted_paths" if excluded else ("untracked_skipped" if not preview_complete else None)
        return {
            "project": str(repo.root),
            "staged": staged,
            "paths": checked,
            "patch": patch,
            "complete": not excluded and preview_complete,
            "reason": reason,
            "next_action": (
                _visibility.restricted_message()
                if excluded
                else (
                    "Some untracked content was omitted due to type, symlink, size, readability or count limits. "
                    "Narrow the requested paths or inspect individual permitted files."
                    if not preview_complete
                    else None
                )
            ),
            "observed_at": observed_at(),
        }


GET_PROJECT_DIFF: Final = ToolDefinition(
    name="get_project_diff",
    description=(
        "Review a working-tree or staged Git patch. Untracked content is excluded; output over 1 MiB is an error. "
        "Unreadable changes are omitted with complete=false and recovery guidance. "
        "Explicit protected paths are rejected; renames are shown as deletion/addition patches. "
        "include_untracked=true requires explicit non-root paths and staged=false; previews only small readable "
        "nonignored UTF-8 regular files without staging, and reports omitted files with complete=false."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectDiffTool,
    readiness=git_readiness,
)
