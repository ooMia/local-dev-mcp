"""Inspect unresolved index entries."""

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


async def has_unmerged(execution: GitExecution) -> bool:
    result = await execution.run("ls-files", "--unmerged", "-z", operation="Inspect Git conflicts")
    return bool(result.output)


async def stage_candidates(execution: GitExecution, paths: list[str]) -> list[str]:
    """List tracked and nonignored untracked entries before add -A reads their content."""
    result = await execution.run(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *paths,
        operation="Inspect staging scope",
    )
    if not result.output:
        return []
    text = text_output(result.output)
    if not text.endswith("\0"):
        raise ToolError("Git returned an incomplete staging path record.")
    return list(dict.fromkeys(text[:-1].split("\0")))
