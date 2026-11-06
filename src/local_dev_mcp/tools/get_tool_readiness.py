"""Report startup preparation without probing external execution state."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from mcp.types import ToolAnnotations

from ..mcp.registry import ToolDefinition
from ..readiness import ReadinessIssue

if TYPE_CHECKING:
    from ..app.runtime import ServerSettings


class ToolReadinessResult(TypedDict):
    name: str
    ready: bool
    issue: ReadinessIssue | None


class ReadinessReport(TypedDict):
    project: str
    snapshot_at: str
    scope: Literal["startup_snapshot_only"]
    policy_edits_allowed: bool
    tools: list[ToolReadinessResult]


@dataclass(frozen=True)
class GetToolReadinessTool:
    settings: ServerSettings

    async def execute(self) -> ReadinessReport:
        return {
            "project": str(self.settings.policy.root),
            "snapshot_at": self.settings.snapshot_at,
            "scope": "startup_snapshot_only",
            "policy_edits_allowed": self.settings.policy.allow_policy_edits,
            "tools": [
                {
                    "name": state.name,
                    "ready": state.issue is None,
                    "issue": state.issue.public() if state.issue else None,
                }
                for state in self.settings.readiness
            ],
        }


GET_TOOL_READINESS: Final = ToolDefinition(
    name="get_tool_readiness",
    description=(
        "Read each registered tool's fixed startup preparation and safe corrective actions. "
        "Readiness only describes the startup snapshot, not live Git/key state or execution success. "
        "Does not run commands or reload settings; corrected settings require restart."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=GetToolReadinessTool,
)
