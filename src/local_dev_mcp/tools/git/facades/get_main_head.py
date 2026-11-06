"""Read the current local main commit without switching the workspace branch."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import merge
from . import _repository
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class MainHead(TypedDict):
    project: str
    branch: str
    head: str
    observed_at: str


@dataclass(frozen=True)
class GetMainHeadTool:
    settings: ServerSettings

    async def execute(self) -> MainHead:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        head = await merge.main_head(repo)
        return {"project": str(repo.root), "branch": "main", "head": head, "observed_at": observed_at()}


GET_MAIN_HEAD: Final = ToolDefinition(
    name="get_main_head",
    description=(
        "Read the full commit SHA for the local main branch without changing the checked-out branch. "
        "Use its head value as expected_main_head for an explicitly authorized merge_project_branch call."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetMainHeadTool,
    readiness=git_readiness,
)
