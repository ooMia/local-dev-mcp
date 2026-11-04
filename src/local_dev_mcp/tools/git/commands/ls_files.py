"""Inspect unresolved index entries."""

from .._execution import GitExecution


async def has_unmerged(execution: GitExecution) -> bool:
    result = await execution.run("ls-files", "--unmerged", "-z", operation="Inspect Git conflicts")
    return bool(result.output)
