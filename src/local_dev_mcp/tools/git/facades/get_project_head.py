"""Compose shared repository checks within one HEAD lookup budget."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._execution import GitExecution
from .._prepare import git_readiness
from . import _repository
from ._repository import observed_at

HEAD_TIMEOUT: Final = 10

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ProjectHead(TypedDict):
    project: str
    head: str
    observed_at: str


@dataclass(frozen=True)
class GetProjectHeadTool:
    settings: ServerSettings

    async def execute(self) -> ProjectHead:
        """Read the current local project's HEAD commit at invocation time."""
        repo = GitExecution(self.settings)
        try:
            # One budget covers all commands, including unborn-branch detection.
            async with asyncio.timeout(HEAD_TIMEOUT):
                await _repository.verify_root(repo)
                head = await _repository.read_head(repo)
                if head is None:
                    raise ToolError("The project must have at least one commit before HEAD can be read.")
        except TimeoutError:
            raise ToolError(f"Git HEAD lookup timed out after {HEAD_TIMEOUT} seconds.") from None
        return {
            "project": str(repo.root),
            "head": head,
            "observed_at": observed_at(),
        }


GET_PROJECT_HEAD: Final = ToolDefinition(
    name="get_project_head",
    description="Read the current local project's full HEAD commit SHA at invocation time.",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetProjectHeadTool,
    readiness=git_readiness,
)
