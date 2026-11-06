"""Apply public path visibility before Git content reaches a tool result."""

from fastmcp.exceptions import ToolError

from .._execution import GitExecution


def readable(execution: GitExecution, path: str, *, traverse: bool = False) -> bool:
    candidate = execution.root / path
    if not execution.settings.policy.allows_path(candidate, traverse=traverse):
        return False
    try:
        # Git may report a removed entry; resolve existing parents without
        # following the final entry, which Git treats as an object itself.
        parent = candidate.parent.resolve()
    except OSError, ValueError:
        return False
    if not parent.is_relative_to(execution.root):
        return False
    return execution.settings.policy.allows_path(parent / candidate.name, traverse=traverse)


def require_selection(execution: GitExecution, paths: list[str]) -> None:
    for path in paths:
        if path != "." and not readable(execution, path, traverse=True):
            raise ToolError(
                "A requested Git path is protected or outside the readable scope. "
                "Choose an allowed project-relative path; no file content was returned."
            )


def restricted_message() -> str:
    return "Restricted paths were excluded. Review allowed paths separately; an empty result is not a clean repository."
