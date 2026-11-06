"""Read HEAD's symbolic reference without guessing a branch."""

from .._execution import GitExecution, text_output


async def head_reference(execution: GitExecution) -> str | None:
    result = await execution.run("symbolic-ref", "--quiet", "HEAD", operation="Read HEAD reference", allow_failure=True)
    return text_output(result.output).strip() if result.code == 0 else None
