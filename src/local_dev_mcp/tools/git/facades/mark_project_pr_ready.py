"""Make a reviewed selected-origin Draft PR ready and observe the result."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...gh import pr_merge, pr_ready
from . import _pr_merge
from ._pr_merge import MergeDigest, PRNumber
from ._refs import ExpectedHead
from .remote_pr import remote_readiness

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class MarkProjectPrReadyTool:
    settings: ServerSettings

    async def execute(
        self, number: PRNumber, expected_head: ExpectedHead, expected_plan_digest: MergeDigest
    ) -> pr_ready.ReadyResult:
        repository, run = await _pr_merge.context(self.settings, "gh.pr.ready")
        try:
            return await pr_ready.mark_ready(run, repository, number, expected_head, expected_plan_digest)
        except pr_merge.MergeCommandError, ValueError:
            raise ToolError("Ready preconditions changed or are invalid. Review a fresh open-main PR plan.") from None


MARK_PROJECT_PR_READY: Final = ToolDefinition(
    name="mark_project_pr_ready",
    description=(
        "Explicitly mark one reviewed open main PR Ready with independent gh.pr.ready permission. "
        "Requires the observed head and merge plan digest; rechecks after the non-atomic Ready transition. "
        "Already Ready means observation only. No merge, review approval or arbitrary PR edits. "
        "Only ready_verified confirms the requested result; never automatically replay uncertain writes."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "gh.pr.ready"),
    create_runtime=MarkProjectPrReadyTool,
    readiness=remote_readiness,
)
