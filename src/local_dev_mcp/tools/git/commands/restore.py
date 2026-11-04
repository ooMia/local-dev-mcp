"""Fixed restore operation on already checked literal project paths."""

from .._execution import GitExecution


async def restore_staged(execution: GitExecution, paths: list[str]) -> None:
    """Restore checked paths in the index from HEAD using --staged.

    Working files remain untouched; the caller must establish a valid HEAD.
    Returns no process output.
    """
    await execution.run(
        "restore", "--staged", "--", *paths, operation="Unstage project paths", write=True, capture=False
    )
