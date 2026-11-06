"""Bind the common release checker to MCP Git access and post-commit evidence."""

import re

from fastmcp.exceptions import ToolError

from ...release import IntegrationCheck, check_integration
from .._execution import GitExecution, text_output
from . import _visibility

_TITLE = re.compile(r"(?:feat|fix|refactor|docs|test|build|ci|chore|perf)(?:\([^()\r\n]+\))?!?: \S[^\r\n]*")
_TOPIC = re.compile(r"(?:feat|fix|refactor|docs|test|build|ci|chore|perf)/[A-Za-z0-9][A-Za-z0-9/-]*")


def require_message(message: str | None) -> str:
    if (
        message is None
        or "\0" in message
        or not message.splitlines()
        or _TITLE.fullmatch(message.splitlines()[0]) is None
    ):
        raise ToolError("Assessed squash integration requires an explicit Conventional Commit message.")
    return message


async def validate(
    repo: GitExecution, base: str, source: str, source_branch: str, message: str | None
) -> IntegrationCheck:
    def authorize(path: str) -> None:
        if not _visibility.readable(repo, path):
            raise ValueError("Required release input is outside the readable workspace scope.")

    async def read(*args: str) -> bytes:
        return (await repo.run(*args, operation="Inspect release integration snapshot")).output

    try:
        checked = await check_integration(read, base, source, authorize=authorize)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if checked["kind"] == "legacy":
        return checked
    require_message(message)
    if checked["kind"] == "change" and _TOPIC.fullmatch(source_branch) is None:
        raise ToolError("Use a type/topic branch for ordinary assessed integration.")
    if checked["kind"] == "release":
        plan = checked["plan"]
        if plan is None or source_branch != f"release/{plan['next_version']}":
            raise ToolError("Use release/<the computed version> for verified release integration.")
    names = text_output(
        (
            await repo.run(
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                base,
                source,
                operation="Inspect assessed integration paths",
            )
        ).output
    )
    if any(not _visibility.readable(repo, path) for path in names.split("\0") if path):
        raise ToolError("Assessed integration contains protected or unreadable changed paths.")
    return checked


async def tree(repo: GitExecution, ref: str) -> str:
    return text_output(
        (await repo.run("rev-parse", "--verify", f"{ref}^{{tree}}", operation="Read Git tree")).output
    ).strip()


async def verify_commit(repo: GitExecution, head: str, parent: str, expected_tree: str, message: str) -> None:
    parents = (
        text_output((await repo.run("rev-list", "--parents", "-n", "1", head, operation="Verify squash parent")).output)
        .strip()
        .split()
    )
    title = text_output(
        (await repo.run("show", "-s", "--format=%s", head, operation="Verify squash title")).output
    ).rstrip("\n")
    if parents != [head, parent] or await tree(repo, head) != expected_tree or title != message.splitlines()[0]:
        raise ToolError(
            f"Created commit {head} failed squash verification. Inspect HEAD, index and hooks; do not replay."
        )
