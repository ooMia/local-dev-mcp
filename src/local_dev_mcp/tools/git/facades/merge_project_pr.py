"""Conditionally squash a reviewed PR and verify the actual result."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...gh import pr_merge
from . import _pr_merge
from ._pr_merge import MergeDigest, PRNumber
from ._refs import ExpectedHead
from .remote_pr import remote_readiness

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@dataclass(frozen=True)
class MergeProjectPrTool:
    settings: ServerSettings

    async def execute(
        self, number: PRNumber, expected_head: ExpectedHead, expected_plan_digest: MergeDigest
    ) -> pr_merge.MergeResult:
        repository, run = await _pr_merge.context(self.settings, "gh.pr.merge")
        try:
            return await pr_merge.merge(run, repository, number, expected_head, expected_plan_digest)
        except pr_merge.MergeCommandError, ValueError:
            raise ToolError(
                "PR merge preconditions are invalid or changed. Review a fresh plan; select an open, ready main PR."
            ) from None


MERGE_PROJECT_PR: Final = ToolDefinition(
    name="merge_project_pr",
    description=(
        "Direct squash a PR observed Ready on main using its reviewed head/digest; verify the actual commit message. "
        "Requires independent gh.pr.merge permission. Already-merged PRs are inspected without another write. "
        "No Ready transition, CI verification, force, bypass, auto-merge, queue or branch deletion. "
        "Only merged_verified confirms the requested result; inspect unconfirmed or merged_mismatch before acting."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    required_commands=("git", "gh", "gh.pr.merge"),
    create_runtime=MergeProjectPrTool,
    readiness=remote_readiness,
)
