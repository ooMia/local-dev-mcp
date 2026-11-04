"""Review patches without external diff drivers or textconv."""

from .._execution import GitExecution, text_output


async def patch(execution: GitExecution, paths: list[str], *, staged: bool = False) -> str:
    selection = ("--cached",) if staged else ()
    result = await execution.run(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        *selection,
        "--",
        *paths,
        operation="Read project diff",
    )
    return text_output(result.output)
