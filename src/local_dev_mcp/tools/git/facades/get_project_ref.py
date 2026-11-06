"""Read an explicit local branch or full commit SHA without checkout."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import revisions
from . import _repository
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


type LocalRef = Annotated[
    str,
    Field(min_length=1, max_length=128, description="Local branch name or full commit SHA; no revision expressions."),
]


class ProjectRef(TypedDict):
    project: str
    ref: str
    head: str
    observed_at: str


@dataclass(frozen=True)
class GetProjectRefTool:
    settings: ServerSettings

    async def execute(self, ref: LocalRef) -> ProjectRef:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        head = await revisions.resolve(repo, ref)
        return {"project": str(repo.root), "ref": ref, "head": head, "observed_at": observed_at()}


GET_PROJECT_REF: Final = ToolDefinition(
    name="get_project_ref",
    description=(
        "Resolve a locally available branch or complete commit SHA without changing branches. "
        "No remote refs, revision expressions, checkout, fetch or filesystem writes. "
        "Use the returned full SHA as a baseline for compare_project_refs."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectRefTool,
    readiness=git_readiness,
)
