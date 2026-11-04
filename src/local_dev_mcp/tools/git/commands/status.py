"""Porcelain-v2 status query and Git record decoding."""

import re
from typing import TypedDict

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


class StatusEntry(TypedDict):
    kind: str
    path: str
    original_path: str | None
    index_status: str
    worktree_status: str
    submodule: str | None


def _parse(data: bytes) -> tuple[str | None, str | None, list[StatusEntry]]:
    text = text_output(data)
    if not text.endswith("\0"):
        raise ToolError("Git returned an incomplete status record.")
    records = iter(text[:-1].split("\0"))
    branch: str | None = None
    head: str | None = None
    entries: list[StatusEntry] = []
    saw_branch = saw_head = False
    for record in records:
        if record.startswith("# branch.oid "):
            value = record.removeprefix("# branch.oid ")
            if value != "(initial)":
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                    raise ToolError("Git returned an invalid status HEAD.")
                head = value
            saw_head = True
        elif record.startswith("# branch.head "):
            value = record.removeprefix("# branch.head ")
            branch = None if value == "(detached)" else value
            saw_branch = True
        elif record.startswith("# "):
            continue  # Porcelain v2 explicitly permits new, unknown headers.
        elif record.startswith(("? ", "! ")):
            entries.append(
                {
                    "kind": "untracked" if record[0] == "?" else "ignored",
                    "path": record[2:],
                    "original_path": None,
                    "index_status": record[0],
                    "worktree_status": record[0],
                    "submodule": None,
                }
            )
        else:
            splits = {"1": 8, "2": 9, "u": 10}
            prefix = record[:1]
            if prefix not in splits:
                raise ToolError("Git returned an unsupported status record.")
            fields = record.split(" ", splits[prefix])
            if len(fields) != splits[prefix] + 1 or len(fields[1]) != 2:
                raise ToolError("Git returned a malformed status record.")
            original: str | None = None
            if prefix == "2":
                original = next(records, None)
                if original is None:
                    raise ToolError("Git returned an incomplete rename status record.")
            entries.append(
                {
                    "kind": {"1": "ordinary", "2": "renamed", "u": "unmerged"}[prefix],
                    "path": fields[-1],
                    "original_path": original,
                    "index_status": fields[1][0],
                    "worktree_status": fields[1][1],
                    "submodule": fields[2],
                }
            )
    if not saw_branch or not saw_head:
        raise ToolError("Git status did not provide branch and HEAD observations.")
    return branch, head, entries


async def read_porcelain_v2(execution: GitExecution) -> tuple[str | None, str | None, list[StatusEntry]]:
    """Decode status --porcelain=v2 --branch --untracked-files=all -z.

    Return branch, HEAD and entries; detached branch and unborn HEAD are None.
    Output format flags stay fixed because the parser depends on them.
    """
    result = await execution.run(
        "status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z", operation="Read project status"
    )
    return _parse(result.output)
