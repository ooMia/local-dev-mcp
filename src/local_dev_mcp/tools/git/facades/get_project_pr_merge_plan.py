"""Preview the exact squash message for one selected-origin PR."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...gh import pr_merge
from . import _pr_merge
from ._pr_merge import PRNumber
from .remote_pr import remote_readiness

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class GetProjectPrMergePlanTool:
    settings: ServerSettings

    async def execute(self, number: PRNumber) -> pr_merge.PlanReport:
        repository, run = await _pr_merge.context(self.settings, "git.remote.read")
        try:
            return (await pr_merge.observe(run, repository, number)).report()
        except pr_merge.MergeCommandError, ValueError:
            raise ToolError("Cannot inspect a valid main PR merge plan. Reinspect origin and PR metadata.") from None


GET_PROJECT_PR_MERGE_PLAN: Final = ToolDefinition(
    name="get_project_pr_merge_plan",
    description=(
        "Preview one selected-origin PR's actual head, exact squash subject/body and review digest. "
        "Read only; no Ready transition, merge, CI verification or ref changes."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=GetProjectPrMergePlanTool,
    readiness=remote_readiness,
)
