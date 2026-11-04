"""Internal repository compositions and project path contracts; never auto-registered."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypedDict

from fastmcp.exceptions import ToolError
from pydantic import Field

from .._execution import GitExecution
from ..commands import ls_files, rev_parse, show_ref, symbolic_ref

# One declaration supplies FastMCP's schema and runtime shape validation.
# Workspace containment and symlink meaning still require filesystem checks.
type ProjectPaths = Annotated[
    list[Annotated[str, Field(min_length=1)]],
    Field(
        min_length=1,
        description=(
            "Nonempty literal project-relative file or directory paths, e.g. ['src/main.py']. "
            "Use ['.'] explicitly for the whole project. Absolute paths and workspace escapes are rejected."
        ),
    ),
]


def observed_at() -> str:
    return datetime.now(UTC).isoformat()


class PathsResult(TypedDict):
    project: str
    success: bool
    paths: list[str]
    observed_at: str


async def verify_root(execution: GitExecution) -> None:
    root = await rev_parse.working_tree_root(execution)
    try:
        matches = root.resolve() == execution.root
    except (OSError, ValueError) as error:
        raise ToolError("Cannot resolve the Git working tree root.") from error
    if not matches:
        raise ToolError("The project must be the Git working tree root; ancestor repositories are not adopted.")


async def read_head(execution: GitExecution) -> str | None:
    head = await rev_parse.head_commit(execution)
    if head is not None:
        return head
    # An absent branch is unborn; broken refs or detached HEAD are not fallbacks.
    reference = await symbolic_ref.head_reference(execution)
    if reference is not None:
        exists = await show_ref.exists(execution, reference)
        if exists is False and reference.startswith("refs/heads/"):
            return None
    raise ToolError("Cannot resolve HEAD as a commit or an unborn branch; no write fallback was selected.")


def check_paths(execution: GitExecution, paths: list[str]) -> list[str]:
    if not paths:
        raise ToolError("Provide a nonempty list of project-relative paths; use '.' explicitly for the whole project.")
    checked: list[str] = []
    for path in paths:
        if not path or "\0" in path or Path(path).is_absolute():
            raise ToolError("Paths must be nonempty project-relative names without NUL characters.")
        candidate = Path(os.path.abspath(execution.root / path))
        if not candidate.is_relative_to(execution.root):
            raise ToolError("A requested path leaves the project root.")
        if candidate == execution.root:
            checked.append(".")
            continue
        try:
            parent = candidate.parent.resolve()
        except (OSError, ValueError) as error:
            raise ToolError("Cannot resolve a requested path's parent.") from error
        if not parent.is_relative_to(execution.root):
            raise ToolError("A requested path traverses a symlink outside the project root.")
        # Preserve a final symlink as a Git entry; do not follow its target.
        checked.append(str(candidate.relative_to(execution.root)))
    return checked


async def require_writable_state(execution: GitExecution) -> None:
    for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = await rev_parse.operation_path(execution, state)
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ToolError("Cannot inspect the current Git operation state.") from error
        raise ToolError("Writes are not supported during merge, rebase, cherry-pick or revert operations.")
    if await ls_files.has_unmerged(execution):
        raise ToolError("Writes are not supported while the index contains unresolved conflicts.")
