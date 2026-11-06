"""Review patches without external diff drivers or textconv."""

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


async def changed_paths(execution: GitExecution, paths: list[str], *, staged: bool = False) -> list[tuple[str, ...]]:
    """Inspect changed names, retaining both sides of renames before reading a patch."""
    selection = ("--cached",) if staged else ()
    result = await execution.run(
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--submodule=short",
        *selection,
        "--",
        *paths,
        operation="Inspect project diff paths",
    )
    if not result.output:
        return []
    text = text_output(result.output)
    if not text.endswith("\0"):
        raise ToolError("Git returned an incomplete changed-path record.")
    records = iter(text[:-1].split("\0"))
    changed: list[tuple[str, ...]] = []
    for status in records:
        if not status or status[0] not in "ACDMRTUXB":
            raise ToolError("Git returned an unsupported changed-path record.")
        first = next(records, None)
        if not first:
            raise ToolError("Git returned an incomplete changed-path record.")
        if status[0] in "RC":
            second = next(records, None)
            if not second:
                raise ToolError("Git returned an incomplete rename record.")
            changed.append((first, second))
        else:
            changed.append((first,))
    return changed


async def patch(execution: GitExecution, paths: list[str], *, staged: bool = False) -> str:
    selection = ("--cached",) if staged else ()
    result = await execution.run(
        "diff",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--submodule=short",
        *selection,
        "--",
        *paths,
        operation="Read project diff",
    )
    return text_output(result.output)
