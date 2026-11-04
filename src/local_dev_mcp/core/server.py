"""Project tools served through FastMCP's typed, strict invocation interface."""

from dataclasses import replace
from typing import Final

from fastmcp import FastMCP

from ..diagnostics import warn_preparation
from ..runtime import ServerSettings
from ..settings import Policy
from ..tools import git, registry, workspace

TOOLS: Final[tuple[registry.ToolDefinition[ServerSettings], ...]] = (
    git.GET_PROJECT_HEAD,
    git.GET_PROJECT_STATUS,
    git.GET_PROJECT_DIFF,
    git.STAGE_PROJECT_PATHS,
    git.UNSTAGE_PROJECT_PATHS,
    git.COMMIT_PROJECT_CHANGES,
    workspace.GET_TOOL_READINESS,
)


def load_settings(policy: Policy) -> ServerSettings:
    settings = ServerSettings.load(policy)
    return replace(settings, readiness=registry.tool_readiness(TOOLS, settings))


def create_server(policy: Policy) -> FastMCP:
    settings = load_settings(policy)
    warn_preparation(tuple(state.issue for state in settings.readiness if state.issue is not None))
    server = FastMCP(
        "local-dev-mcp",
        version="0.1.0",
        strict_input_validation=True,
        mask_error_details=True,
        on_duplicate="error",
        tasks=False,
        middleware=[registry.SafeToolDiagnostics()],
        instructions=(
            "Use get_tool_readiness to inspect fixed startup preparation, not live execution health. "
            "Local project Git tools: inspect HEAD, status and diff; stage, unstage and commit explicit changes. "
            "Commit records the entire current index. Writes may have taken effect even when an error is reported; "
            "inspect status and HEAD before retrying. No fetch, pull, push, branch switching or integration tools."
        ),
    )
    registry.register_tools(server, policy, TOOLS, settings, readiness=settings.readiness)
    return server
