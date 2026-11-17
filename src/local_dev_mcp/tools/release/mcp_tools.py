"""MCP adapters for the existing immutable release plan and explicit preparation."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ...errors import DependencyError, PolicyError
from ...mcp.registry import ToolDefinition
from ...readiness import PreparationIssue
from .. import executable
from ..git._execution import GitExecution
from ..git._prepare import git_readiness, require_git_access
from ..git.commands import status
from ..workspace._discovery import initial_rules
from ..workspace._edits import EditRejected
from ..workspace._reads import Budget, ScanStopped
from ..workspace._writes import require_writable
from ..workspace.errors import WorkspaceError
from ._plan import ReleasePlan
from ._prepare import PreparedRelease, prepare_release
from ._snapshots import read_snapshot

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type CommitSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
type PlanDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type ReleaseVersion = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")]


class ReleaseSummary(TypedDict):
    project: str
    branch: str | None
    clean: bool
    plan: ReleasePlan


class ReleasePreparation(PreparedRelease):
    project: str
    branch: str


def _authorize(settings: ServerSettings, relative: str, *, write: bool = False) -> None:
    """Authorize Git tree input and potential output paths before exposing or writing content."""
    root = settings.policy.root
    target = root / relative
    try:
        if not settings.policy.allows_path(target, write=write):
            raise ValueError("A required release path is outside the permitted workspace scope.")
        if not target.parent.resolve().is_relative_to(root):
            raise ValueError("A release path has an external parent.")
    except OSError, ValueError:
        raise ValueError("A required release path is unavailable or restricted.") from None


def _writable(settings: ServerSettings, relative: str) -> None:
    """Preflight every release output and consumed assessment using normal workspace write rules."""
    _authorize(settings, relative, write=True)
    root = settings.policy.root
    workspace = settings.files.workspace
    scoped = workspace.path(relative)
    budget = Budget()
    try:
        if (root / relative).exists() or (root / relative).is_symlink():
            require_writable(scoped, budget)
        else:
            parent = workspace.path(str(Path(relative).parent))
            parent.checked()
            rules = initial_rules(parent, budget)
            if rules is None or rules.excluded(scoped, False):
                raise ValueError("The release output is ignored or cannot be checked.")
    except OSError, PermissionError, WorkspaceError, EditRejected, ScanStopped:
        raise ValueError("A release output is inaccessible, read-only or ignored.") from None


async def _snapshot(settings: ServerSettings) -> tuple[GitExecution, str | None, str, bool, ReleasePlan]:
    require_git_access(settings)
    repo = GitExecution(settings)
    # Verify the root without exposing potentially restricted changed paths.
    root_result = await repo.run("rev-parse", "--show-toplevel", operation="Check release Git root")
    if await asyncio.to_thread(lambda: Path(root_result.output.decode().strip()).resolve() != repo.root):
        raise ToolError("The selected release workspace must be the Git root.")
    branch, head, entries = await status.read_porcelain_v2(repo)
    if head is None:
        raise ToolError("A committed HEAD is required to inspect the release plan.")

    async def read(*args: str) -> bytes:
        return (await repo.run(*args, operation="Read release snapshot")).output

    try:
        snapshot = await read_snapshot(read, head, authorize=lambda p: _authorize(settings, p))
        plan = snapshot.plan()
    except ValueError:
        raise ToolError(
            "Release plan is unavailable or invalid; inspect the committed release settings and assessments."
        ) from None
    return repo, branch, head, not entries, plan


@dataclass(frozen=True)
class GetReleasePlanTool:
    settings: ServerSettings

    async def execute(self) -> ReleaseSummary:
        repo, branch, _, clean, plan = await _snapshot(self.settings)
        return {"project": str(repo.root), "branch": branch, "clean": clean, "plan": plan}


@dataclass(frozen=True)
class PrepareProjectReleaseTool:
    settings: ServerSettings

    async def execute(
        self, expected_head: CommitSHA, expected_plan: PlanDigest, expected_version: ReleaseVersion
    ) -> ReleasePreparation:
        # This is a separate explicit write operation, never performed when only a recommendation is requested.
        require_git_access(self.settings)
        try:
            self.settings.policy.require_command("uv.release")
        except PermissionError:
            raise ToolError("Release preparation is not permitted; allow uv.release and restart the server.") from None
        repo, branch, head, clean, plan = await _snapshot(self.settings)
        if (head, plan["digest"], plan["next_version"]) != (expected_head, expected_plan, expected_version):
            raise ToolError("Release HEAD, plan digest or version changed. Read a fresh get_release_plan result.")
        if not clean or branch is None or branch != f"release/{expected_version}":
            raise ToolError("Prepare from a clean release/<recommended-version> branch at the observed HEAD.")

        # Prepare modifies four files and consumes every assessment. Reject restricted or ignored targets in advance.
        try:
            for relative in ("pyproject.toml", "uv.lock", "CHANGELOG.md", ".release.toml"):
                _writable(self.settings, relative)
            for change in plan["changes"]:
                _writable(self.settings, f".changes/{change['id']}.toml")
        except ValueError:
            raise ToolError("Release outputs or assessments are not writable under workspace policy.") from None
        try:
            prepared = await prepare_release(repo.root, expected_head, expected_plan)
        except ValueError, OSError:
            raise ToolError(
                "Release preparation failed or could not be verified. Files may have changed; "
                "inspect current status, version and lock before attempting another operation."
            ) from None
        return {"project": str(repo.root), "branch": branch, **prepared}


def _prepare_readiness(settings: ServerSettings) -> PreparationIssue | None:
    previous = git_readiness(settings)
    if previous is not None:
        return previous
    try:
        settings.policy.require_command("uv.release")
        executable("uv")
    except PermissionError:
        return PreparationIssue.from_error(PolicyError.command_denied("uv.release"))
    except DependencyError as error:
        return PreparationIssue.from_error(error)
    return None


GET_RELEASE_PLAN: Final = ToolDefinition(
    name="get_release_plan",
    description=(
        "Read committed release assessments and their reasons, maximum impact, current and recommended next version, "
        "digest, and exact HEAD. Reports whether the working tree is clean. Does not edit files or infer compatibility "
        "from commit messages; uncommitted assessments are excluded."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=GetReleasePlanTool,
    readiness=git_readiness,
)

PREPARE_PROJECT_RELEASE: Final = ToolDefinition(
    name="prepare_project_release",
    description=(
        "Prepare the exact recommended version from a clean release/<version> branch using a previously observed "
        "HEAD, plan digest and version. Requires opt-in uv.release permission. Updates pyproject.toml, uv.lock, "
        "CHANGELOG.md and .release.toml, and consumes assessed changes. No commit, merge, tag, push or publish. "
        "May leave partial files on failure; inspect status before retrying."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    required_commands=("git", "uv.release"),
    create_runtime=PrepareProjectReleaseTool,
    readiness=_prepare_readiness,
)
