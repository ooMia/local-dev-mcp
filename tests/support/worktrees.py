"""Temporary Git worktree scenarios shared by integration tests."""

from pathlib import Path

from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, git


async def seed(root: Path, workspace_factory: WorkspaceFactory, *, sha256: bool = False) -> tuple[str, str]:
    await workspace_factory(sha256=sha256)
    await git(root, "branch", "-M", "main")
    (root / ".gitignore").write_text(".lab/\n.env\n")
    (root / "base.txt").write_text("base\n")
    await git(root, "add", ".gitignore", "base.txt")
    await git(root, "commit", "-m", "chore: base")
    base = await git(root, "rev-parse", "HEAD")
    await git(root, "branch", "feat/publish", base)
    return base, await git(root, "symbolic-ref", "--short", "HEAD")


def policy(root: Path) -> Policy:
    return Policy(root, (root,), ("git", "git.worktree.write"), allow_policy_edits=True)


def request(head: str) -> dict[str, object]:
    return {"branch": "feat/publish", "expected_branch_head": head, "expected_branch": "main", "expected_head": head}
