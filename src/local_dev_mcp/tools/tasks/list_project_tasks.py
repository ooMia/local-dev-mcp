"""Discover registered task recipes and shared quality profiles."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ...quality import load_quality_manifest
from ._response import TaskInfo

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


class QualityProfileInfo(TypedDict):
    name: str
    checks: list[str]
    manifest_digest: str
    require_clean: bool
    ready: bool
    reason: str | None


class ProjectTasks(TypedDict):
    project: str
    snapshot_at: str
    policy_edits_allowed: bool
    tasks: list[TaskInfo]
    profiles_status: Literal["absent", "available", "invalid"]
    profiles: list[QualityProfileInfo]


@dataclass(frozen=True)
class ListProjectTasksTool:
    settings: ServerSettings

    async def execute(self) -> ProjectTasks:
        recipes = self.settings.tasks.catalogue()
        ready = {item["name"]: item["ready"] for item in recipes}
        status: Literal["absent", "available", "invalid"] = "absent"
        profiles: list[QualityProfileInfo] = []
        root = self.settings.policy.root
        path = root / ".config" / "quality.toml"
        if path.exists() or path.is_symlink():
            status = "invalid"
            try:
                self.settings.policy.require_path_access(path)
                manifest = load_quality_manifest(root)
                for profile in manifest.profiles:
                    manifest.select(profile.name, self.settings.policy.jobs)
                    unready = [name for name in profile.checks if not ready.get(name, False)]
                    profiles.append(
                        QualityProfileInfo(
                            name=profile.name,
                            checks=list(profile.checks),
                            manifest_digest=manifest.digest,
                            require_clean=profile.require_clean,
                            ready=not unready,
                            reason="registered_check_unavailable" if unready else None,
                        )
                    )
                status = "available"
            except OSError, ValueError, PermissionError:
                # An invalid manifest must never be interpreted as an empty/pass profile.
                profiles = []
        return {
            "project": str(root),
            "snapshot_at": self.settings.snapshot_at,
            "policy_edits_allowed": self.settings.policy.allow_policy_edits,
            "tasks": recipes,
            "profiles_status": status,
            "profiles": profiles,
        }


LIST_PROJECT_TASKS: Final = ToolDefinition(
    name="list_project_tasks",
    description=(
        "List registered lint, format and test recipes and shared quality profiles. "
        "Profiles contain ordered existing recipe names, not extra command permissions. "
        "absent/invalid profiles cannot be used as quality evidence; unavailable checks are flagged. "
        "Task permissions and executables are fixed at startup; no commands run and argv is not exposed."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ListProjectTasksTool,
)
