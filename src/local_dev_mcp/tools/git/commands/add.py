"""Fixed add operation on already checked literal project paths."""

from .._execution import GitExecution


async def add_all(execution: GitExecution, paths: list[str]) -> None:
    """Apply add -A to checked literal paths, including additions and deletions.

    Paths are required and limit the scope; an explicit ['.'] selects the project.
    Does not force ignored files. Returns no process output.
    """
    await execution.run("add", "-A", "--", *paths, operation="Stage project paths", write=True, capture=False)
