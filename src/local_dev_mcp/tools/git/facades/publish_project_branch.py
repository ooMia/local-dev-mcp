"""Publish a prepared topic branch using the existing remote publication gates."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from .. import _worktrees
from ..worktrunk import WorktrunkAdapter
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
    backend: Literal["native", "worktrunk"] = "native",
    allow_dirty_controller: bool = False,
) -> AsyncGenerator[ServerSettings]:
    remote_pr.require_permission(settings, permission)
    if allow_dirty_controller and backend != "worktrunk":
        raise ToolError("Dirty controller opt-in is supported only for the Worktrunk publication backend.")
    fingerprint = await _worktrees.original_snapshot(
        settings, original_branch, original_head, allow_dirty=allow_dirty_controller
    )

    async def selected_checkout() -> ServerSettings:
        if backend == "worktrunk":
            return await WorktrunkAdapter(settings).require_ready(branch, head)
        return await _worktrees.require_ready(settings, branch, head)

    target = await selected_checkout()
    try:
        yield target
        await selected_checkout()
    finally:
        await _worktrees.confirm_original(
            settings, original_branch, original_head, fingerprint, allow_dirty=allow_dirty_controller
        )


@dataclass(frozen=True)
class InspectProjectBranchRemoteTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        backend: Literal["native", "worktrunk"] = "native",
    ) -> remote_pr.RemoteState:
        remote_pr.require_permission(self.settings, "git.remote.read")

        async def selected_checkout() -> ServerSettings:
            if backend == "worktrunk":
                return await WorktrunkAdapter(self.settings).require_ready(branch, expected_branch_head, write=False)
            return await _worktrees.require_ready(self.settings, branch, expected_branch_head)

        target = await selected_checkout()
        result = await remote_pr.InspectRemoteTool(target).execute()
        await selected_checkout()
        result["project"] = str(self.settings.policy.root)
        return result


@dataclass(frozen=True)
class FetchProjectBranchRemoteMainTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_branch_head: ExpectedHead,
        expected_remote_main: ExpectedHead,
    ) -> remote_pr.FetchedMain:
        """Fetch only origin/main from a clean registered Worktrunk checkout."""
        remote_pr.require_permission(self.settings, "git.remote.write")
        adapter = WorktrunkAdapter(self.settings)
        target = await adapter.require_ready(branch, expected_branch_head)
        result = await remote_pr.FetchRemoteMainTool(target).execute(branch, expected_branch_head, expected_remote_main)
        # A shared tracking ref may have moved; the source checkout must not.
        await adapter.require_ready(branch, expected_branch_head)
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
        backend: Literal["native", "worktrunk"] = "native",
        allow_dirty_controller: bool = False,
    ) -> remote_pr.PushedFeature:
        async with publication(
            self.settings,
            branch,
            expected_branch_head,
            expected_branch,
            expected_head,
            "git.remote.write",
            backend,
            allow_dirty_controller,
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
        backend: Literal["native", "worktrunk"] = "native",
        allow_dirty_controller: bool = False,
    ) -> remote_pr.PullRequestResult:
        async with publication(
            self.settings,
            branch,
            expected_branch_head,
            expected_branch,
            expected_head,
            "gh.pr.create",
            backend,
            allow_dirty_controller,
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
        "Inspect remote refs for an exact clean branch HEAD. backend=native (default) uses the existing "
        "managed checkout; backend=worktrunk uses an authorized Git-registered linked checkout without "
        "Native receipts. Read-only: no checkout switching, fetch, push or PR creation."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "git.remote.read"),
    create_runtime=InspectProjectBranchRemoteTool,
    readiness=remote_pr.remote_readiness,
)
FETCH_PROJECT_BRANCH_REMOTE_MAIN: Final = ToolDefinition(
    name="fetch_project_branch_remote_main",
    description=(
        "Fetch the exact observed remote main into origin/main from one clean, Git-registered "
        "Worktrunk checkout without requiring a clean controller. Reuse existing no-tags, "
        "fast-forward-only, direct-ref and remote SHA gates. Never update local main or the "
        "controller index/working tree; inspect refs after uncertain failures."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "wt", "git.worktree.write", "gh", "git.remote.write"),
    create_runtime=FetchProjectBranchRemoteMainTool,
    readiness=remote_pr.remote_readiness,
)
PUSH_PROJECT_BRANCH: Final = ToolDefinition(
    name="push_project_branch",
    description=(
        "Push an exact prepared SHA from backend=native (default) or a Git-registered Worktrunk checkout. "
        "Requires observed original branch/HEAD, target SHA and remote refs. Explicit Worktrunk-only "
        "allow_dirty_controller pins dirty original contents; default still requires clean original. "
        "Reuses all non-force/main-history gates; "
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
        "Create or rediscover a Draft PR from backend=native (default) or a clean Worktrunk checkout. "
        "Uses controller template and target committed assessments; reuses baseline, history and duplicate-PR gates. "
        "Worktrunk-only allow_dirty_controller opt-in verifies original dirty contents before/after publication. "
        "Never invents assessments, merges, marks ready or changes Rulesets."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    required_commands=("git", "gh", "gh.pr.create"),
    create_runtime=CreateProjectBranchDraftPRTool,
    readiness=remote_pr.remote_readiness,
)
