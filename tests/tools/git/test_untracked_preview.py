"""Explicit, read-only previews of selected untracked project files."""

import asyncio
from pathlib import Path

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success

pytestmark = pytest.mark.integration


def test_untracked_preview_shows_selected_text_without_staging_or_leaking_secrets(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / ".gitignore").write_text("ignored.txt\n")
        folder = tmp_path / "files"
        folder.mkdir()
        (folder / "tracked.txt").write_text("before\n")
        await git(tmp_path, "add", ".gitignore", "files/tracked.txt")
        await git(tmp_path, "commit", "-m", "baseline")
        (folder / "tracked.txt").write_text("after\n")
        (folder / "new.txt").write_text("new first\nnew second\n")
        (folder / "ignored.txt").write_text("ignored-canary")
        (folder / ".env.local").write_text("private-canary")
        async with client(tmp_path) as session:
            before = await success(session, "get_project_pick_state")
            result = await success(
                session,
                "get_project_diff",
                {"paths": ["files"], "include_untracked": True},
            )
            assert "+after" in str(result["patch"])
            assert "+new first" in str(result["patch"])
            assert "new.txt" in str(result["patch"])
            assert "ignored-canary" not in str(result)
            assert "private-canary" not in str(result)
            assert result["complete"] is False
            after = await success(session, "get_project_pick_state")
            assert before["head"] == after["head"]
            assert before["index_fingerprint"] == after["index_fingerprint"]
            assert "files/new.txt" not in await git(tmp_path, "ls-files")

    asyncio.run(scenario())


def test_untracked_preview_requires_explicit_narrow_selection(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "new.txt").write_text("safe")
        async with client(tmp_path) as session:
            arguments: tuple[dict[str, object], ...] = (
                {"include_untracked": True},
                {"include_untracked": True, "paths": ["."]},
                {"include_untracked": True, "paths": ["new.txt"], "staged": True},
            )
            for args in arguments:
                await failure(session, "get_project_diff", args)
            unchanged = await success(session, "get_project_diff", {"paths": ["new.txt"]})
            assert unchanged["patch"] == ""

    asyncio.run(scenario())


def test_untracked_preview_excludes_binary_and_symlink(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "safe.txt").write_text("preview-visible")
        (tmp_path / "binary.bin").write_bytes(b"secret\\x00value".replace(b"\\x00", b"\x00"))
        (tmp_path / "other.txt").write_text("not-exposed-from-link")
        try:
            (tmp_path / "link.txt").symlink_to(tmp_path / "other.txt")
        except OSError, NotImplementedError:
            pytest.skip("Symlinks unavailable")
        async with client(tmp_path) as session:
            result = await success(
                session,
                "get_project_diff",
                {
                    "include_untracked": True,
                    "paths": ["safe.txt", "binary.bin", "link.txt"],
                },
            )
            assert "preview-visible" in str(result["patch"])
            assert "secret" not in str(result)
            assert "not-exposed-from-link" not in str(result)
            assert result["complete"] is False

    asyncio.run(scenario())


def test_untracked_preview_large_file_is_incomplete_and_index_unchanged(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "safe.txt").write_text("normal-preview\n")
        (tmp_path / "oversize.txt").write_text("oversize-private-canary" * 1800)
        async with client(tmp_path) as session:
            before = await success(session, "get_project_pick_state")
            result = await success(
                session,
                "get_project_diff",
                {
                    "include_untracked": True,
                    "paths": ["safe.txt", "oversize.txt"],
                },
            )
            assert "normal-preview" in str(result["patch"])
            assert "oversize-private-canary" not in str(result)
            assert result["complete"] is False and result["reason"] == "untracked_skipped"
            after = await success(session, "get_project_pick_state")
            assert before["index_fingerprint"] == after["index_fingerprint"]

    asyncio.run(scenario())
