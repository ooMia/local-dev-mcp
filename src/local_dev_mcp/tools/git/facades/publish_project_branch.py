"""Publish a prepared topic branch using the existing remote publication gates."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktrees
from . import remote_pr
from ._refs import BranchName, ExpectedHead

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


@asynccontextmanager
async def publication(
    settings: ServerSettings,
    branch: str,
    head: str,
    original_branch: str,
    original_head: str,
    permission: str,
) -> AsyncGenerator[ServerSettings]:
    remote_pr.require_permission(settings, permission)
    fingerprint = await _worktrees.original_snapshot(settings, original_branch, original_head)
    target = await _worktrees.require_ready(settings, branch, head)
    try:
        yield target
        await _worktrees.require_ready(settings, branch, head)
    finally:
        await _worktrees.confirm_original(settings, original_branch, original_head, fingerprint)


@dataclass(frozen=True)
class InspectProjectBranchRemoteTool:
    settings: ServerSettings

    async def execute(self, branch: BranchName, expected_branch_head: ExpectedHead) -> remote_pr.RemoteState:
        remote_pr.require_permission(self.settings, "git.remote.read")
        target = await _worktrees.require_ready(self.settings, branch, expected_branch_head)
        result = await remote_pr.InspectRemoteTool(target).execute()
        await _worktrees.require_ready(self.settings, branch, expected_branch_head)
        result["project"] = str(self.settings.policy.root)
        return result


@dataclass(frozen=True)
class PushProjectBranchTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_remote_main: ExpectedHead,
        expected_remote_feature: remote_pr.RemoteBranchHead = None,
    ) -> remote_pr.PushedFeature:
        async with publication(
            self.settings, branch, expected_branch_head, expected_branch, expected_head, "git.remote.write"
        ) as target:
            result = await remote_pr.PushFeatureTool(target).execute(
                branch, expected_branch_head, expected_remote_main, expected_remote_feature
            )
        result["project"] = str(self.settings.policy.root)
        return result


@dataclass(frozen=True)
class CreateProjectBranchDraftPRTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_branch: str,
        expected_head: ExpectedHead,
        expected_remote_main: ExpectedHead,
        title_text: str,
        purpose: remote_pr.Purpose = None,
        exceptions: list[dict[str, str]] | None = None,
    ) -> remote_pr.PullRequestResult:
        async with publication(
            self.settings, branch, expected_branch_head, expected_branch, expected_head, "gh.pr.create"
        ) as target:
            root = self.settings.policy.root
            template = root / ".github" / "PULL_REQUEST_TEMPLATE.md"
            try:
                for candidate in (template.parent, template):
                    self.settings.policy.require_path_access(candidate)
                    if candidate.is_symlink():
                        raise ToolError("Controller PR template cannot traverse symlinks.")
                if not template.is_file():
                    raise ToolError("Controller PR template is missing.")
            except PermissionError as error:
                raise ToolError(str(error)) from None
            # A historical branch may predate PR templates. The running controller
            # supplies its template while facts always come from the pinned target.
            result = await remote_pr.CreateFeaturePRTool(target, self.settings.policy.root).execute(
                branch, expected_branch_head, expected_remote_main, title_text, purpose, exceptions
            )
        result["project"] = str(self.settings.policy.root)
        return result


INSPECT_PROJECT_BRANCH_REMOTE: Final = ToolDefinition(
    name="inspect_project_branch_remote",
    description=(
        "Inspect remote refs for an exactly observed branch in its clean managed worktree; never switches the original."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=InspectProjectBranchRemoteTool,
    readiness=remote_pr.remote_readiness,
)
PUSH_PROJECT_BRANCH: Final = ToolDefinition(
    name="push_project_branch",
    description=(
        "Push an exact prepared branch SHA from its clean managed worktree without switching the original checkout. "
        "Requires observed original branch/HEAD, target SHA and remote refs. Reuses all non-force/main-history gates; "
        "never updates main, tags or bypasses hooks. Inspect both worktrees and remote refs after failure."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.write"),
    create_runtime=PushProjectBranchTool,
    readiness=remote_pr.remote_readiness,
)
CREATE_PROJECT_BRANCH_DRAFT_PR: Final = ToolDefinition(
    name="create_project_branch_draft_pr",
    description=(
        "Create or rediscover a Draft PR for an exact pushed managed branch, preserving the original checkout. "
        "Uses controller template and target committed assessments; reuses baseline, history and duplicate-PR gates. "
        "Never invents assessments, merges, marks ready or changes Rulesets."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "gh.pr.create"),
    create_runtime=CreateProjectBranchDraftPRTool,
    readiness=remote_pr.remote_readiness,
)
