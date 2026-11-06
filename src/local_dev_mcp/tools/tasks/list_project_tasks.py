"""Discover registered CLI recipes and each recipe's fixed startup readiness."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from mcp.types import ToolAnnotations

from ...mcp.registry import ToolDefinition
from ._response import TaskInfo

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings


class ProjectTasks(TypedDict):
    project: str
    snapshot_at: str
    policy_edits_allowed: bool
    tasks: list[TaskInfo]


@dataclass(frozen=True)
class ListProjectTasksTool:
    settings: ServerSettings

    async def execute(self) -> ProjectTasks:
        return {
            "project": str(self.settings.policy.root),
            "snapshot_at": self.settings.snapshot_at,
            "policy_edits_allowed": self.settings.policy.allow_policy_edits,
            "tasks": self.settings.tasks.catalogue(),
        }


LIST_PROJECT_TASKS: Final = ToolDefinition(
    name="list_project_tasks",
    description=(
        "List the selected workspace's registered lint, format and test recipes and per-recipe startup readiness. "
        "Does not run commands or expose argv or environment values. A recipe's permission and executable are "
        "checked independently, so an unavailable recipe does not disable other recipes. Restart after config changes."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ListProjectTasksTool,
)
