"""Create a single-parent snapshot commit without checkout or ordinary commit hooks."""

from .._execution import GitExecution, text_output


async def create(repo: GitExecution, tree: str, parent: str, message: str, *, signing: bool) -> str:
    result = await repo.run(
        "commit-tree",
        tree,
        "-p",
        parent,
        *(("-S",) if signing else ()),
        "-m",
        message,
        operation="Create candidate snapshot commit",
        write=True,
    )
    return text_output(result.output).strip()
