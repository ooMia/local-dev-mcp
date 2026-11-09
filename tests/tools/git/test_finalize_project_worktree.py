"""A verified merged PR permits lossless linked-worktree finalization only."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git._remote import Remote, Result
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, request, seed

pytestmark = pytest.mark.integration
MERGED = "b" * 40


def allow(root: Path) -> Policy:
    return replace(
        policy(root),
        allowed_commands=("git", "git.worktree.write", "gh", "git.remote.read"),
    )


def github(
    monkeypatch: pytest.MonkeyPatch,
    expected_head: str,
    *,
    merged: bool = True,
    source_branch: str = "feat/publish",
) -> None:
    async def gh(self: Remote, *args: str, write: bool = False, input_text: str | None = None) -> Result:
        assert not write and input_text is None
        if args[:2] == ("repo", "view"):
            return Result(0, '{"url":"https://github.com/owner/project"}')
        if args[:2] == ("pr", "view"):
            if args[-1] == "headRefName":
                return Result(0, json.dumps({"headRefName": source_branch}))
            return Result(
                0,
                json.dumps(
                    {
                        "url": "https://github.com/owner/project/pull/12",
                        "title": "feat: finished worktree",
                        "headRefOid": expected_head,
                        "baseRefName": "main",
                        "state": "MERGED" if merged else "OPEN",
                        "isDraft": False,
                        "mergeCommit": {"oid": MERGED} if merged else None,
                    }
                ),
            )
        assert args[-1] == "repos/owner/project/git/commits/" + MERGED
        return Result(
            0,
            json.dumps(
                {
                    "sha": MERGED,
                    "parents": [{"sha": "c" * 40}],
                    "message": "feat: finished worktree (#12)\n\n"
                    f"Source-PR: https://github.com/owner/project/pull/12\nSource-Head: {expected_head}",
                }
            ),
        )

    monkeypatch.setattr(Remote, "gh", gh)


def finalize(head: str, *, remove_branch: bool = True) -> dict[str, object]:
    return {
        "branch": "feat/publish",
        "expected_branch_head": head,
        "expected_branch": "main",
        "expected_head": head,
        "merged_pr_number": 12,
        "delete_branch": remove_branch,
    }


@pytest.mark.parametrize("remove_branch", [False, True])
def test_verified_merge_removes_clean_worktree_preserving_controller(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, remove_branch: bool
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "remote", "add", "origin", "https://github.com/owner/project.git")
        github(monkeypatch, head)
        before = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(allow(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            path = tmp_path / str(ready["path"])
            receipt = path.with_suffix(".json")
            assert path.exists() and receipt.is_file()
            result = await success(session, "finalize_project_worktree", finalize(head, remove_branch=remove_branch))
            assert result["status"] == "finalized"
            assert result["worktree_removed"] is True
            assert result["branch_deleted"] is remove_branch
            assert result["receipt_removed"] is True
            assert (await success(session, "inspect_project_worktree", {"branch": "feat/publish"}))["state"] == "absent"
        assert not path.exists() and not receipt.exists()
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "ls-files", "--stage") == before
        assert bool(await git(tmp_path, "branch", "--list", "feat/publish")) is not remove_branch

    asyncio.run(scenario())


def test_orphan_receipt_after_external_worktree_and_branch_removal(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "remote", "add", "origin", "https://github.com/owner/project.git")
        github(monkeypatch, head)
        async with Client(create_server(allow(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            path = tmp_path / str(ready["path"])
            receipt = path.with_suffix(".json")
            await git(tmp_path, "worktree", "remove", str(path))
            await git(tmp_path, "branch", "-D", "feat/publish")
            assert receipt.is_file()
            assert (await success(session, "inspect_project_worktree", {"branch": "feat/publish"}))[
                "state"
            ] == "partial"
            result = await success(session, "finalize_project_worktree", finalize(head))
            assert result["status"] == "finalized"
            assert result["worktree_removed"] is False
            assert result["branch_deleted"] is False
            assert result["receipt_removed"] is True
            assert (await success(session, "inspect_project_worktree", {"branch": "feat/publish"}))["state"] == "absent"

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["dirty", "ignored", "open", "head", "pr-branch", "permission"])
def test_finalize_does_not_remove_unverified_or_dirty_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        await git(tmp_path, "remote", "add", "origin", "https://github.com/owner/project.git")
        github(
            monkeypatch,
            "a" * 40 if invalid == "head" else head,
            merged=invalid != "open",
            source_branch="feat/other" if invalid == "pr-branch" else "feat/publish",
        )
        selection = allow(tmp_path)
        if invalid == "permission":
            selection = replace(selection, allowed_commands=("git", "git.worktree.write"))
        async with Client(create_server(selection)) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            path = tmp_path / str(ready["path"])
            receipt = path.with_suffix(".json")
            if invalid == "dirty":
                (path / "user.txt").write_text("keep me\n")
            if invalid == "ignored":
                (path / ".env").write_text("ignored credentials stay\n")
            await failure(session, "finalize_project_worktree", finalize(head))
            assert path.is_dir() and receipt.is_file()
            assert await git(tmp_path, "rev-parse", "feat/publish") == head
            if invalid == "dirty":
                assert (path / "user.txt").read_text() == "keep me\n"
            if invalid == "ignored":
                assert (path / ".env").read_text() == "ignored credentials stay\n"

    asyncio.run(scenario())
