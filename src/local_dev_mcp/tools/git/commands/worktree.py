"""Fixed linked-worktree commands and NUL-delimited Git output."""

from dataclasses import dataclass
from pathlib import Path

from fastmcp.exceptions import ToolError

from ....settings import Policy, protected_name
from .._execution import GitExecution, text_output


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None
    branch: str | None
    unavailable: bool


async def list_registered(execution: GitExecution) -> tuple[Worktree, ...]:
    result = await execution.run("worktree", "list", "--porcelain", "-z", operation="List Git worktrees")
    text = text_output(result.output)
    if not text.endswith("\0\0"):
        raise ToolError("Incomplete Git worktree listing.")
    records: list[Worktree] = []
    for block in text[:-2].split("\0\0"):
        fields = block.split("\0")
        if not fields[0].startswith("worktree "):
            raise ToolError("Invalid Git worktree listing.")
        values = dict(field.split(" ", 1) for field in fields if " " in field)
        records.append(
            Worktree(
                Path(fields[0][9:]),
                values.get("HEAD"),
                values.get("branch"),
                any(field.split(" ", 1)[0] in ("locked", "prunable", "bare") for field in fields),
            )
        )
    return tuple(records)


async def common_directory(execution: GitExecution) -> Path:
    result = await execution.run("rev-parse", "--git-common-dir", operation="Verify shared Git directory")
    path = Path(text_output(result.output).removesuffix("\n"))
    return (path if path.is_absolute() else execution.root / path).resolve()


async def add_existing_branch(execution: GitExecution, path: Path, branch: str) -> None:
    await execution.run(
        "-c",
        "submodule.recurse=false",
        "worktree",
        "add",
        "--",
        str(path),
        branch,
        operation="Prepare isolated worktree",
        write=True,
        capture=False,
    )


async def require_ignored(execution: GitExecution, relative: str) -> None:
    result = await execution.run(
        "--no-literal-pathspecs",
        "check-ignore",
        "--quiet",
        "--",
        relative,
        operation="Verify managed worktree ignore rule",
        allow_failure=True,
    )
    if result.code != 0:
        raise ToolError("Managed .lab/worktrees paths must be ignored before preparation.")


async def require_safe_checkout_tree(
    execution: GitExecution,
    policy: Policy,
    path: Path,
    head: str,
    *,
    operation: str,
) -> None:
    """Reject tracked protected paths and policy escapes before materializing a checkout."""
    tree = await execution.run("ls-tree", "-r", "--name-only", "-z", head, operation=operation)
    for name in filter(None, text_output(tree.output).split("\0")):
        if any(protected_name(part) for part in name.split("/")):
            raise ToolError("Target checkout tree contains protected paths; no worktree was created.")
        if not policy.allows_path(path / name) or not policy.allows_path(execution.root / name):
            raise ToolError("Target checkout tree includes paths outside the configured access policy.")
