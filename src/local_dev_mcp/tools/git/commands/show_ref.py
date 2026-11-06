"""Distinguish an existing, absent or unreadable Git reference."""

from .._execution import GitExecution


async def exists(execution: GitExecution, reference: str) -> bool | None:
    result = await execution.run(
        "show-ref", "--verify", "--quiet", reference, operation="Check HEAD reference", allow_failure=True
    )
    if result.code == 0:
        return True
    return False if result.code == 1 else None
