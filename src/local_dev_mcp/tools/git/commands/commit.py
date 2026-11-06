"""Commit only the current index using explicit signing intent."""

from .._execution import GitExecution


async def commit_index(execution: GitExecution, message: str, *, signing: bool) -> None:
    """Commit the existing index with -m and user.useConfigOnly=true.

    The caller validates message and supplies signing intent from startup settings:
    true uses --gpg-sign, false uses --no-gpg-sign. No automatic staging, hook
    bypass or unsigned retry. Returns no process output.
    """
    await execution.run(
        "-c",
        "user.useConfigOnly=true",
        "commit",
        "--gpg-sign" if signing else "--no-gpg-sign",
        "-m",
        message,
        operation="Commit project changes",
        write=True,
        capture=False,
    )
