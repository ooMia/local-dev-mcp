"""M4-P1 public per-worktree Trail operations in disposable Git repositories."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, IndexReport
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import seed

pytestmark = pytest.mark.integration


async def _linked(root: Path, workspace_factory: WorkspaceFactory) -> tuple[str, Path, Path]:
    await seed(root, workspace_factory)
    (root / ".gitignore").write_text(".lab/\n.env\n/graft/\n", encoding="utf-8")
    (root / "feature.py").write_text("def marker(): return 'M4_BASE'\n", encoding="utf-8")
    await git(root, "add", ".gitignore", "feature.py")
    await git(root, "commit", "-m", "test: index marker")
    alpha = root / ".lab" / "wt" / "alpha"
    beta = root / ".lab" / "wt" / "beta"
    for branch, path, marker in (
        ("feat/m4-alpha", alpha, "M4_P1_ALPHA_19"),
        ("feat/m4-beta", beta, "M4_P1_BETA_27"),
    ):
        await git(root, "worktree", "add", "-b", branch, str(path), "main")
        (path / "feature.py").write_text(f"def marker(): return '{marker}'\n", encoding="utf-8")
        await git(path, "commit", "-am", "test: isolated worktree marker")
    return await git(alpha, "rev-parse", "HEAD"), alpha, beta


def _policy(root: Path, *, write: bool = True) -> Policy:
    commands = ("git", "trail", "wt", "git.worktree.write") if write else ("git", "trail")
    return Policy(root, (root,), commands)


def _search(binary: str, needle: str, root: Path) -> str:
    env = dict(os.environ)
    env.update({"GRAFT_NO_IGNORE": "1", "GRAFT_NO_GITIGNORE": "1"})
    result = subprocess.run(
        [binary, "grep", needle, str(root)], env=env, capture_output=True, text=True, check=True, timeout=35
    )
    return result.stdout


def test_m4_public_worktree_status_refresh_and_cross_branch_isolation(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    binary = shutil.which("trail", path=os.environ.get("PATH"))
    if binary is None:
        pytest.skip("Real Trail CLI is required for P1 verification.")

    async def scenario() -> None:
        alpha_sha, alpha, beta = await _linked(tmp_path, workspace_factory)
        beta_sha = await git(beta, "rev-parse", "HEAD")
        original_sha = await git(tmp_path, "rev-parse", "HEAD")
        args_a: dict[str, object] = {"branch": "feat/m4-alpha", "expected_head": alpha_sha}
        args_b: dict[str, object] = {"branch": "feat/m4-beta", "expected_head": beta_sha}
        async with Client(create_server(_policy(tmp_path))) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            for name in ("get_project_worktree_index_status", "refresh_project_worktree_index"):
                assert set(tools[name].input_schema["properties"]) == {"branch", "expected_head"}
                assert tools[name].input_schema["additionalProperties"] is False
            assert tools["get_project_index_status"].input_schema["properties"] == {}
            assert tools["refresh_project_index"].input_schema["properties"] == {}
            await failure(client, "get_project_worktree_index_status", {**args_a, "expected_head": "f" * 40})
            await failure(client, "refresh_project_worktree_index", {**args_a, "expected_head": "f" * 40})

            # Simultaneous updates for the *same* checkout must share one
            # root-scoped lock, rather than race through two fresh managers.
            responses = await asyncio.gather(
                success(client, "refresh_project_worktree_index", args_a),
                success(client, "refresh_project_worktree_index", args_a),
            )
            assert sorted(str(r["outcome"]) for r in responses) == ["already_fresh", "updated"]
            assert all(r["structural_state"] == "fresh" for r in responses)
            assert all(r["head"] == alpha_sha and r["branch"] == "feat/m4-alpha" for r in responses)
            assert all(r["project"] == str(tmp_path) for r in responses)
            assert all(r["worktree_path"] == ".lab/wt/alpha" for r in responses)

            refreshed_b = await success(client, "refresh_project_worktree_index", args_b)
            assert refreshed_b["outcome"] in ("updated", "already_fresh"), refreshed_b
            assert refreshed_b["ready_for_structural_search"]
            checked = await success(client, "get_project_worktree_index_status", args_b)
            assert checked["structural_state"] == "fresh"
            assert "M4_P1_ALPHA_19" in _search(binary, "M4_P1_ALPHA_19", alpha)
            assert "M4_P1_BETA_27" in _search(binary, "M4_P1_BETA_27", beta)
            assert "M4_P1_ALPHA_19" not in _search(binary, "M4_P1_ALPHA_19", beta)
            assert "M4_P1_BETA_27" not in _search(binary, "M4_P1_BETA_27", alpha)

            for target, head in ((alpha, alpha_sha), (beta, beta_sha), (tmp_path, original_sha)):
                assert await git(target, "rev-parse", "HEAD") == head
                assert await git(target, "status", "--porcelain") == ""

            (beta / "feature.py").write_text("user's uncommitted changes\n", encoding="utf-8")
            await failure(client, "refresh_project_worktree_index", args_b)
            await failure(client, "get_project_worktree_index_status", args_b)
            assert (beta / "feature.py").read_text() == "user's uncommitted changes\n"

        # Reading must not require the independent worktree-write grant.
        async with Client(create_server(_policy(tmp_path, write=False))) as client:
            report = await success(client, "get_project_worktree_index_status", args_a)
            assert report["structural_state"] == "fresh"
            await failure(client, "refresh_project_worktree_index", args_a)

    asyncio.run(scenario())


def test_m4_refresh_rejects_checkout_drift_after_trail(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        alpha_sha, alpha, _ = await _linked(tmp_path, workspace_factory)
        original_refresh = IndexManager.refresh

        async def drift(self: IndexManager) -> IndexReport:
            (alpha / "feature.py").write_text("external user mutation\n", encoding="utf-8")
            return await original_refresh(self)

        monkeypatch.setattr(IndexManager, "refresh", drift)
        args: dict[str, object] = {"branch": "feat/m4-alpha", "expected_head": alpha_sha}
        async with Client(create_server(_policy(tmp_path))) as client:
            message = await failure(client, "refresh_project_worktree_index", args)
            assert message
            assert (alpha / "feature.py").read_text() == "external user mutation\n"

    asyncio.run(scenario())
