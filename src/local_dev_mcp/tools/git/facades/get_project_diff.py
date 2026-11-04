"""Bounded working-tree or staged patch output, without external diff drivers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ...registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import diff
from . import _repository
from ._repository import ProjectPaths, observed_at

if TYPE_CHECKING:
    from ....runtime import ServerSettings


class ProjectDiff(TypedDict):
    project: str
    staged: bool
    paths: list[str]
    patch: str
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
    ) -> ProjectDiff:
        repo = GitExecution(self.settings)
        checked = _repository.check_paths(repo, ["."] if paths is None else paths)
        await _repository.verify_root(repo)
        patch = await diff.patch(repo, checked, staged=staged)
        return {
            "project": str(repo.root),
            "staged": staged,
            "paths": checked,
            "patch": patch,
            "observed_at": observed_at(),
        }


GET_PROJECT_DIFF: Final = ToolDefinition(
    name="get_project_diff",
    description=(
        "Review a working-tree or staged Git patch. Untracked content is excluded; output over 1 MiB is an error."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectDiffTool,
    readiness=git_readiness,
)
