"""Bounded local commit history parsing for observed revision ranges."""

import re
from typing import TypedDict

from fastmcp.exceptions import ToolError

from .._execution import GitExecution, text_output


class HistoryCommit(TypedDict):
    sha: str
    parents: list[str]
    title: str


async def history(repo: GitExecution, base: str, target: str, *, count: int) -> list[HistoryCommit]:
    commits: list[HistoryCommit] = []
    if count:
        result = await repo.run(
            "log",
            "--reverse",
            "--topo-order",
            "-z",
            "--format=%H%x00%P%x00%s",
            f"{base}..{target}",
            operation="Read bounded local commit history",
        )
        raw = text_output(result.output)
        if not raw.endswith("\0"):
            raise ToolError("Git returned an incomplete history stream.")
        parts = raw[:-1].split("\0")
        if len(parts) != count * 3:
            raise ToolError("Git returned an incomplete or ambiguous commit history.")
        for i in range(0, len(parts), 3):
            sha, parent_string, subject = parts[i : i + 3]
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None:
                raise ToolError("History contains an invalid commit SHA.")
            parents = parent_string.split()
            if not parents or any(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", p) is None for p in parents):
                raise ToolError("History contains an invalid commit parent.")
            if not subject or len(subject) > 200 or any(ord(c) < 32 for c in subject):
                raise ToolError("Commit title contains invalid characters or exceeds the display limit.")
            commits.append({"sha": sha, "parents": parents, "title": subject})
    return commits
