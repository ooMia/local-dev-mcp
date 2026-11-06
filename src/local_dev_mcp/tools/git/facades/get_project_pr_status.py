"""Read the selected PR's exact-head checks and lifecycle state."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...gh import pr_merge, pr_status
from . import _pr_merge
from ._pr_merge import PRNumber
from ._refs import ExpectedHead
from .remote_pr import remote_readiness

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class GetProjectPrStatusTool:
    settings: ServerSettings

    async def execute(self, number: PRNumber, expected_head: ExpectedHead) -> pr_status.StatusReport:
        repository, run = await _pr_merge.context(self.settings, "git.remote.read")
        try:
            return await pr_status.inspect(run, repository, number, expected_head)
        except pr_merge.MergeCommandError, ValueError:
            raise ToolError("Cannot inspect exact-head PR checks. Reobserve the PR head and origin metadata.") from None


GET_PROJECT_PR_STATUS: Final = ToolDefinition(
    name="get_project_pr_status",
    description=(
        "Read one selected-origin main PR's lifecycle, mergeability and exact-head CI contexts. "
        "Classifies GitHub Actions Project quality as success/pending/failure/missing/unknown. "
        "Incomplete or different-head evidence is never success. No writes or merge policy enforcement."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=GetProjectPrStatusTool,
    readiness=remote_readiness,
)
