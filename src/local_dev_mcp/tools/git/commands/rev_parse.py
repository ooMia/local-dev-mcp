"""Fixed rev-parse queries and their Git-native output validation."""

import re
from pathlib import Path

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


async def working_tree_root(execution: GitExecution) -> Path:
    result = await execution.run("rev-parse", "--show-toplevel", operation="Locate Git working tree")
    return Path(text_output(result.output).removesuffix("\n"))


async def head_commit(execution: GitExecution) -> str | None:
    result = await execution.run(
        "rev-parse", "--verify", "--quiet", "HEAD^{commit}", operation="Read HEAD", allow_failure=True
    )
    if result.code != 0:
        return None
    head = text_output(result.output).strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ToolError("Git returned an invalid commit object ID.")
    return head


async def operation_path(execution: GitExecution, state: str) -> Path:
    result = await execution.run("rev-parse", "--git-path", state, operation="Inspect Git operation state")
    path = Path(text_output(result.output).removesuffix("\n"))
    return path if path.is_absolute() else execution.root / path
