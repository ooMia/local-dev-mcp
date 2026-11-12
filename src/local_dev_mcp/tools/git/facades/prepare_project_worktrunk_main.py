"""Explicit preparation of main in an independent Worktrunk checkout."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .._prepare import git_readiness
from ..worktrunk.main import prepare_main
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class PrepareProjectWorktrunkMainTool:
    settings: ServerSettings

    async def execute(
        self,
        expected_branch: BranchName,
        expected_head: ExpectedHead,
        expected_main_head: ExpectedHead,
    ) -> dict[str, str]:
        return await prepare_main(self.settings, expected_branch, expected_head, expected_main_head)


PREPARE_PROJECT_WORKTRUNK_MAIN: Final = ToolDefinition(
    name="prepare_project_worktrunk_main",
    description=(
        "Create an independent linked main checkout under the fixed ignored .lab/wt/main path "
        "when main is not already checked out. Requires exact observed source/main heads, "
        "git.worktree.write and wt permissions; preserves dirty original files and does not "
        "update main refs or run Worktrunk hooks. Refuses path occupancy and existing main "
        "registrations. On an uncertain result inspect actual Git worktrees before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=PrepareProjectWorktrunkMainTool,
    readiness=git_readiness,
)
