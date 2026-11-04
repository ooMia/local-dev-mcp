"""Fixed rm operation on already checked literal project paths."""

from .._execution import GitExecution


async def remove_cached_recursive(execution: GitExecution, paths: list[str]) -> None:
    """Remove checked paths from the index using --cached -r, never --force.

    Working files remain untouched. Git may reject removal when staged content
    differs from working files. Returns no process output.
    """
    await execution.run(
        "rm", "--cached", "-r", "--", *paths, operation="Unstage project paths", write=True, capture=False
    )
