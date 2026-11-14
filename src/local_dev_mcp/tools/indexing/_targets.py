"""Resolve only exact, Git-registered project or linked-worktree Trail targets.

M4's initial internal boundary does not expand the public M1-M3 tool schemas.
The same server policy and startup environment apply to either target.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type IndexTargetKind = Literal["project", "worktree"]


async def resolve_index_target(
    settings: ServerSettings,
    *,
    kind: IndexTargetKind,
    branch: str | None = None,
    expected_head: str | None = None,
) -> ServerSettings:
    """Bind an immutable target root, rejecting caller-supplied filesystem paths."""
    try:
        settings.policy.require_project_access()
        settings.policy.require_command("trail")
    except PermissionError as error:
        raise ToolError(str(error)) from None

    if kind == "project":
        if branch is not None or expected_head is not None:
            raise ToolError("Project indexing does not accept a Worktree selector.")
        return settings

    if kind != "worktree" or not branch or not expected_head:
        raise ToolError("Worktree indexing requires an exact registered branch and complete HEAD.")

    from ..git.worktrunk import WorktrunkAdapter

    # require_ready verifies Git registration, common Git directory, path access,
    # clean status, exact branch and HEAD both before and after target resolution.
    scoped = await WorktrunkAdapter(settings).require_ready(branch, expected_head)
    environment = dict(scoped.environment)
    # Trail otherwise may write .ignore or append /graft/ to tracked .gitignore.
    # This is restricted to the explicitly selected Worktree, not the controller.
    environment.update({"GRAFT_NO_IGNORE": "1", "GRAFT_NO_GITIGNORE": "1"})
    return replace(
        scoped,
        environment=tuple(environment.items()),
        trail_refresh_on_write=False,
    )
