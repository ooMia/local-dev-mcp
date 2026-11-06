"""Fixed Git queries and operations for an explicitly guarded main fast-forward."""

import re

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


async def main_head(execution: GitExecution) -> str:
    result = await execution.run(
        "rev-parse",
        "--verify",
        "--quiet",
        "refs/heads/main^{commit}",
        operation="Read local main",
        allow_failure=True,
    )
    if result.code != 0:
        raise ToolError("Local main must exist and point to a valid commit.")
    value = text_output(result.output).strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ToolError("Git returned an invalid local main commit ID.")
    return value


async def require_fast_forward(execution: GitExecution, old_main: str, source: str) -> None:
    result = await execution.run(
        "merge-base",
        "--is-ancestor",
        old_main,
        source,
        operation="Check main fast-forward",
        allow_failure=True,
    )
    if result.code == 1:
        raise ToolError(
            "Main and the source do not permit a fast-forward. "
            "No merge was attempted; resolve the divergence through explicit review."
        )
    if result.code != 0:
        raise ToolError("Cannot establish fast-forward ancestry; no merge was attempted.")


async def switch_to_main(execution: GitExecution) -> None:
    await execution.run(
        "switch",
        "--no-guess",
        "main",
        operation="Switch to main for guarded merge",
        write=True,
        capture=False,
    )


async def merge_fast_forward(execution: GitExecution, source_commit: str) -> None:
    await execution.run(
        "merge",
        "--ff-only",
        "--no-edit",
        source_commit,
        operation="Fast-forward local main",
        write=True,
        capture=False,
    )
