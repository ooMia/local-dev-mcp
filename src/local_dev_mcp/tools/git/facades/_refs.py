"""Strict local branch and full commit-ID resolution for Git facade calls."""

import re
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from .._execution import GitExecution, text_output
from .._names import branch_name_allowed

type BranchName = Annotated[
    str,
    Field(
        min_length=1, max_length=128, description="ASCII branch name with letters, digits, hyphens and slash segments."
    ),
]
type ExpectedHead = Annotated[
    str,
    Field(
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
        description="Exact full commit SHA previously observed.",
    ),
]
type RefKind = Literal["branch", "commit"]

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def check_branch_name(name: str) -> str:
    if not branch_name_allowed(name):
        raise ToolError("Use an ASCII topic branch or release/<canonical-X.Y.Z> branch name.")
    return name


async def resolve_local_ref(repo: GitExecution, ref: str) -> tuple[RefKind, str]:
    """Accept only a full commit object ID or refs/heads/<validated-name>."""
    if _SHA_RE.fullmatch(ref):
        result = await repo.run("cat-file", "-t", ref, operation="Verify commit object", allow_failure=True)
        if result.code != 0 or text_output(result.output).strip() != "commit":
            raise ToolError("The specified full SHA does not identify an available commit object.")
        return "commit", ref

    check_branch_name(ref)
    exists = await repo.run(
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{ref}",
        operation="Verify local branch",
        allow_failure=True,
    )
    if exists.code == 1:
        raise ToolError("The specified local branch does not exist.")
    if exists.code != 0:
        raise ToolError("Cannot confirm the specified local branch.")
    result = await repo.run(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{ref}^{{commit}}",
        operation="Resolve local branch",
        allow_failure=True,
    )
    if result.code != 0:
        raise ToolError("The specified local branch does not resolve to a commit.")
    sha = text_output(result.output).strip()
    if _SHA_RE.fullmatch(sha) is None:
        raise ToolError("Git returned an invalid commit SHA.")
    return "branch", sha


async def require_absent_branch(repo: GitExecution, name: str) -> None:
    check_branch_name(name)
    result = await repo.run(
        "check-ref-format",
        "--branch",
        name,
        operation="Check new branch name",
        allow_failure=True,
    )
    if result.code != 0:
        raise ToolError("Git rejected the proposed branch name.")
    exists = await repo.run(
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{name}",
        operation="Check existing branch",
        allow_failure=True,
    )
    if exists.code == 0:
        raise ToolError("The requested local branch already exists.")
    if exists.code != 1:
        raise ToolError("Cannot establish whether the requested branch already exists.")
