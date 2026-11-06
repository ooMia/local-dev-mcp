"""Local-ref resolution and bounded read-only history comparisons."""

import hashlib
import re
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output
from .._names import branch_name_allowed

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class ChangedPath:
    status: str
    path: str
    original_path: str | None


def checked_revision(value: str) -> str:
    """Accept only local branch names or full object IDs; never revision expressions."""
    if not (branch_name_allowed(value) or _SHA.fullmatch(value)):
        raise ToolError("Use a local branch name or full commit SHA without revision operators.")
    if _SHA.fullmatch(value):
        return value
    return f"refs/heads/{value}"


async def resolve(execution: GitExecution, reference: str) -> str:
    selected = checked_revision(reference)
    result = await execution.run(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{selected}^{{commit}}",
        operation="Resolve local Git commit",
        allow_failure=True,
    )
    if result.code != 0:
        raise ToolError("The requested local ref is missing or does not resolve to a commit.")
    head = text_output(result.output).strip()
    if _SHA.fullmatch(head) is None:
        raise ToolError("Git returned an invalid commit object ID.")
    return head


async def ancestry(execution: GitExecution, base: str, target: str) -> tuple[str | None, int, int]:
    base_result = await execution.run(
        "merge-base",
        base,
        target,
        operation="Find common ancestor",
        allow_failure=True,
    )
    if base_result.code not in (0, 1):
        raise ToolError("Cannot establish common ancestry between the selected refs.")
    common: str | None = None
    if base_result.code == 0:
        common = text_output(base_result.output).strip()
        if _SHA.fullmatch(common) is None:
            raise ToolError("Git returned an invalid common ancestor.")
    counts = await execution.run(
        "rev-list",
        "--left-right",
        "--count",
        f"{base}...{target}",
        operation="Count branch-only commits",
    )
    numbers = text_output(counts.output).split()
    if len(numbers) != 2 or any(not token.isascii() or not token.isdecimal() for token in numbers):
        raise ToolError("Git returned invalid exclusive commit counts.")
    return common, int(numbers[0]), int(numbers[1])


async def changed_paths(execution: GitExecution, base: str, target: str) -> list[ChangedPath]:
    """Inspect *all* rename endpoints before applying public path selection."""
    result = await execution.run(
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--submodule=short",
        base,
        target,
        "--",
        operation="Inspect comparison paths",
    )
    if not result.output:
        return []
    raw = text_output(result.output)
    if not raw.endswith("\0"):
        raise ToolError("Git returned an incomplete comparison path record.")
    records = iter(raw[:-1].split("\0"))
    changes: list[ChangedPath] = []
    for value in records:
        if not value or value[0] not in "ACDMRTUXB":
            raise ToolError("Git returned an unsupported comparison status.")
        old = next(records, None)
        if not old:
            raise ToolError("Git returned an incomplete comparison path.")
        if value[0] in "RC":
            new = next(records, None)
            if not new:
                raise ToolError("Git returned an incomplete rename record.")
            changes.append(ChangedPath(value[0], new, old))
        else:
            changes.append(ChangedPath(value[0], old, None))
    return changes


async def patch(execution: GitExecution, base: str, target: str, visible_paths: list[str]) -> str:
    if not visible_paths:
        return ""
    result = await execution.run(
        "diff",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--submodule=short",
        base,
        target,
        "--",
        *visible_paths,
        operation="Read comparison patch",
    )
    return text_output(result.output)


async def resolve_commit(execution: GitExecution, oid: str) -> str:
    """Accept only a full SHA pointing directly to an ordinary commit object."""
    if _SHA.fullmatch(oid) is None:
        raise ToolError("The selected commit must be a complete commit SHA.")
    result = await execution.run(
        "cat-file",
        "-t",
        oid,
        operation="Verify commit object",
        allow_failure=True,
    )
    if result.code != 0 or text_output(result.output).strip() != "commit":
        raise ToolError("The selected SHA is not an available commit object.")
    return oid


async def index_fingerprint(execution: GitExecution) -> str:
    """Observe staged index records without creating any Git objects."""
    result = await execution.run("ls-files", "--stage", "-z", operation="Fingerprint Git index")
    return hashlib.sha256(result.output).hexdigest()


async def commit_paths(execution: GitExecution, oid: str) -> list[str]:
    """Return touched paths for one ordinary non-merge commit."""
    parents = await execution.run(
        "rev-list",
        "--parents",
        "-n",
        "1",
        oid,
        operation="Check commit parents",
    )
    fields = text_output(parents.output).strip().split()
    if not fields or fields[0] != oid or len(fields) > 2:
        raise ToolError("Only an ordinary non-merge commit may be cherry-picked.")
    result = await execution.run(
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--no-renames",
        "--name-only",
        "-r",
        "-z",
        oid,
        operation="Read selected commit paths",
    )
    if not result.output:
        return []
    data = text_output(result.output)
    if not data.endswith("\0"):
        raise ToolError("Git returned an incomplete commit path list.")
    return list(dict.fromkeys(data[:-1].split("\0")))
