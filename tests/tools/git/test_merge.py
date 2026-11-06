"""Safe main-only fast-forward merge: observed refs, cleanliness and signature semantics."""

import asyncio
from pathlib import Path
from typing import cast

import pytest

from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success
from tests.support.signing import SigningSetup

pytestmark = pytest.mark.integration


async def prepared_feature(root: Path, factory: WorkspaceFactory) -> tuple[str, str]:
    await factory()
    await git(root, "branch", "-M", "main")
    (root / "feature.txt").write_text("base\n")
    await git(root, "add", "feature.txt")
    await git(root, "commit", "-m", "base")
    main_head = await git(root, "rev-parse", "HEAD")
    await git(root, "switch", "-c", "migration/document-core")
    (root / "feature.txt").write_text("feature\n")
    await git(root, "add", "feature.txt")
    await git(root, "commit", "-m", "feature")
    return main_head, await git(root, "rev-parse", "HEAD")


def args(main: str, source: str) -> dict[str, object]:
    return {
        "expected_source_branch": "migration/document-core",
        "expected_source_head": source,
        "expected_main_head": main,
    }


def test_main_fast_forward_preserves_commit_and_switches_cleanly(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        main, feature = await prepared_feature(tmp_path, workspace_factory)
        async with client(tmp_path) as session:
            observed_main = await success(session, "get_main_head")
            assert observed_main["branch"] == "main"
            assert observed_main["head"] == main
            result = await success(session, "merge_project_branch", args(main, feature))
            after = await success(session, "get_project_status")
            assert result["merged"] is True
            assert result["branch_before"] == "migration/document-core"
            assert result["branch_after"] == "main"
            assert result["main_head_before"] == main
            assert result["main_head_after"] == feature
            assert result["signature_verified"] is None
            assert after["branch"] == "main"
            assert after["head"] == feature
            assert after["entries"] == []
            assert (await success(session, "get_main_head"))["head"] == feature
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == feature
        assert await git(tmp_path, "rev-parse", "refs/heads/migration/document-core") == feature

    asyncio.run(scenario())


@pytest.mark.parametrize("dirty", ["unstaged", "staged", "untracked"])
def test_main_merge_requires_entire_clean_worktree(
    tmp_path: Path, workspace_factory: WorkspaceFactory, dirty: str
) -> None:
    async def scenario() -> None:
        main, source = await prepared_feature(tmp_path, workspace_factory)
        path = tmp_path / ("new.txt" if dirty == "untracked" else "feature.txt")
        path.write_text("dirty\n")
        if dirty == "staged":
            await git(tmp_path, "add", "feature.txt")
        async with client(tmp_path) as session:
            error = await failure(session, "merge_project_branch", args(main, source))
            assert "clean" in error.lower()
            after = await success(session, "get_project_status")
            assert after["branch"] == "migration/document-core"
            assert after["head"] == source
            assert isinstance(after["entries"], list)
            assert len(cast(list[object], after["entries"])) > 0
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == main

    asyncio.run(scenario())


def test_main_merge_rejects_changed_expected_head(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        main, source = await prepared_feature(tmp_path, workspace_factory)
        async with client(tmp_path) as session:
            assert "moved" in (await failure(session, "merge_project_branch", args("f" * 40, source))).lower()
            assert "changed" in (await failure(session, "merge_project_branch", args(main, "e" * 40))).lower()
            after = await success(session, "get_project_status")
            assert after["branch"] == "migration/document-core"
            assert after["head"] == source
            assert await failure(
                session,
                "merge_project_branch",
                {**args(main, source), "target_branch": "another"},
            )
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == main

    asyncio.run(scenario())


def test_divergence_requires_review_and_does_not_modify_refs(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        _, source = await prepared_feature(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "main")
        (tmp_path / "other.txt").write_text("main only\n")
        await git(tmp_path, "add", "other.txt")
        await git(tmp_path, "commit", "-m", "diverge main")
        main = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "migration/document-core")
        async with client(tmp_path) as session:
            error = await failure(session, "merge_project_branch", args(main, source))
            assert "fast-forward" in error
            after = await success(session, "get_project_status")
            assert after["branch"] == "migration/document-core"
            assert after["head"] == source
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == main

    asyncio.run(scenario())


def test_main_merge_refuses_unsigned_source_with_signing_required(
    tmp_path: Path, workspace_factory: WorkspaceFactory, signing_setup: SigningSetup
) -> None:
    async def scenario() -> None:
        main, source = await prepared_feature(tmp_path, workspace_factory)
        # Reconfigure the server to require the prepared signing key; the
        # fixture-created source commit is unsigned.
        with (tmp_path / ".env").open("a") as stream:
            stream.write(signing_setup.dotenv())
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "merge_project_branch", args(main, source))
            assert "signature" in error.lower()
            assert (await success(session, "get_project_status"))["branch"] == "migration/document-core"
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == main

    asyncio.run(scenario())


def test_main_merge_verifies_signed_source_when_signing_enabled(
    tmp_path: Path, workspace_factory: WorkspaceFactory, signing_setup: SigningSetup
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        await git(tmp_path, "branch", "-M", "main")
        (tmp_path / "base.txt").write_text("base\\n")
        await git(tmp_path, "add", "base.txt")
        await git(tmp_path, "commit", "-m", "base")
        main = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "switch", "-c", "migration/document-core")
        (tmp_path / "source.txt").write_text("signed feature\\n")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["source.txt"]})
            created = await success(session, "commit_project_changes", {"message": "signed feature"})
            assert created["signature_verified"] is True
            source = created["head_after"]
            assert isinstance(source, str)
            result = await success(session, "merge_project_branch", args(main, source))
            assert result["signature_verified"] is True
            assert result["main_head_after"] == source
        assert await git(tmp_path, "rev-parse", "refs/heads/main") == source

    asyncio.run(scenario())
