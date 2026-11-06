"""Cherry-pick must never overwrite an ignored or ordinary untracked user file."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ignored", [False, True])
def test_cherry_pick_refuses_user_file_collision(
    tmp_path: Path, workspace_factory: WorkspaceFactory, ignored: bool
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "base.txt").write_text("base\n")
        if ignored:
            (tmp_path / ".gitignore").write_text("user.txt\n")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "base")
        await git(tmp_path, "switch", "-c", "feature")
        (tmp_path / "user.txt").write_text("source version\n")
        await git(tmp_path, "add", *("-f",) if ignored else (), "user.txt")
        await git(tmp_path, "commit", "-m", "track user file")
        source = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-")
        (tmp_path / "user.txt").write_text("private user data\n")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_pick_state")
            err = await failure(
                session,
                "cherry_pick_project_commit",
                {
                    "commit": source,
                    "expected_branch": before["branch"],
                    "expected_head": before["head"],
                    "expected_index_fingerprint": before["index_fingerprint"],
                },
            )
            after = await success(session, "get_project_pick_state")
        assert "untracked or ignored" in err.lower()
        assert after["head"] == before["head"]
        assert after["index_fingerprint"] == before["index_fingerprint"]
        assert (tmp_path / "user.txt").read_text() == "private user data\n"

    asyncio.run(scenario())
