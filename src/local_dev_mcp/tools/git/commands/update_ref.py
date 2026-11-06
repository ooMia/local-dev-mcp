"""Direct-ref inspection and atomic creation without following symbolic destinations."""

from fastmcp.exceptions import ToolError

from .._execution import GitExecution


async def require_direct(repo: GitExecution, branch: str) -> None:
    result = await repo.run(
        "symbolic-ref",
        "--quiet",
        f"refs/heads/{branch}",
        operation="Check candidate ref indirection",
        allow_failure=True,
    )
    if result.code == 0:
        raise ToolError("The candidate destination is symbolic; no indirect ref writes are allowed.")
    if result.code != 1:
        raise ToolError("Cannot establish whether the candidate destination is a direct ref.")


async def create_absent_branch(repo: GitExecution, branch: str, head: str) -> None:
    await repo.run(
        "update-ref",
        "--no-deref",
        "-m",
        "local-dev-mcp: create consolidated candidate",
        f"refs/heads/{branch}",
        head,
        "0" * len(head),
        operation="Publish absent local candidate ref",
        write=True,
    )
