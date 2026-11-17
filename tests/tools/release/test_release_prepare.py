"""Actual uv release preparation, deterministic notes and strict integration deltas."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from local_dev_mcp.execution import base_environment
from local_dev_mcp.tools.release import check_integration, prepare_release, read_snapshot
from local_dev_mcp.tools.release._git import LocalGit
from local_dev_mcp.tools.uv import update_version
from tests.support.git import WorkspaceFactory, client, git, initialize
from tests.support.mcp import failure, success
from tests.support.release import release_workspace

pytestmark = pytest.mark.integration


async def ready(root: Path, factory: WorkspaceFactory) -> tuple[str, str]:
    await release_workspace(root, factory, pending=True)
    await asyncio.to_thread(
        subprocess.run,
        ["uv", "lock", "--offline", "--no-config", "--cache-dir", str(root.parent / "uv-cache")],
        cwd=root,
        env=base_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n<!-- towncrier release notes start -->\n\n## [1.2.3]\n\nExisting release notes.\n"
    )
    await git(root, "add", "uv.lock", "CHANGELOG.md")
    await git(root, "commit", "-m", "build: lock initial project")
    head = await git(root, "rev-parse", "HEAD")
    plan = (await read_snapshot(LocalGit(root).read, head)).plan()
    await git(root, "switch", "-c", "release/1.3.0")
    return head, plan["digest"]


def test_prepare_uses_real_uv_and_produces_verifiable_release_only_delta(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        base, digest = await ready(tmp_path, workspace_factory)
        original_notes = (tmp_path / "CHANGELOG.md").read_bytes()
        result = await prepare_release(tmp_path, base, digest)
        assert result["prepared"] is True
        assert result["version"] == "1.3.0"
        assert await git(tmp_path, "rev-parse", "HEAD") == base
        assert not (tmp_path / ".venv").exists()
        assert not (tmp_path / ".changes/previous.toml").exists()
        assert b"Existing release notes." in (tmp_path / "CHANGELOG.md").read_bytes()
        assert original_notes.split(b"## [1.2.3]")[1] in (tmp_path / "CHANGELOG.md").read_bytes()
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "chore(release): prepare 1.3.0")
        checked = await check_integration(LocalGit(tmp_path).read, base, await git(tmp_path, "rev-parse", "HEAD"))
        assert checked["kind"] == "release"
        assert checked["assessments"] == ["previous"]

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["head", "digest", "dirty", "branch"])
def test_prepare_rejects_stale_or_dirty_request_before_version_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, invalid: str
) -> None:
    async def scenario() -> None:
        base, digest = await ready(tmp_path, workspace_factory)
        before = (tmp_path / "pyproject.toml").read_bytes()
        if invalid == "dirty":
            (tmp_path / "unexpected.txt").write_text("not reviewed")
        elif invalid == "branch":
            await git(tmp_path, "switch", "main")
        with pytest.raises(ValueError):
            await prepare_release(
                tmp_path, "f" * 40 if invalid == "head" else base, "0" * 64 if invalid == "digest" else digest
            )
        assert (tmp_path / "pyproject.toml").read_bytes() == before
        assert not (tmp_path / ".release.toml").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "CHANGELOG.md", ".release.toml"])
def test_prepare_never_follows_output_symlinks(tmp_path: Path, workspace_factory: WorkspaceFactory, path: str) -> None:
    async def scenario() -> None:
        base, _ = await ready(tmp_path, workspace_factory)
        outside = tmp_path.parent / "outside-target"
        outside.write_text("must stay untouched")
        candidate = tmp_path / path
        if candidate.exists():
            candidate.unlink()
        candidate.symlink_to(outside)
        await git(tmp_path, "add", path)
        await git(tmp_path, "commit", "-m", "introduce output symlink")
        head = await git(tmp_path, "rev-parse", "HEAD")
        with pytest.raises(ValueError):
            await prepare_release(tmp_path, head, "0" * 64)
        assert outside.read_text() == "must stay untouched"
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


@pytest.mark.parametrize("tamper", ["source", "dependency", "old-notes", "new-notes", "lock", "receipt", "new-main"])
def test_release_check_rejects_unrelated_or_stale_preparation(
    tmp_path: Path, workspace_factory: WorkspaceFactory, tamper: str
) -> None:
    async def scenario() -> None:
        base, digest = await ready(tmp_path, workspace_factory)
        await prepare_release(tmp_path, base, digest)
        if tamper == "source":
            (tmp_path / "hidden-feature.py").write_text("raise RuntimeError()\n")
        elif tamper == "dependency":
            path = tmp_path / "pyproject.toml"
            path.write_text(path.read_text().replace("[project]", '[project]\ndependencies = ["unreviewed"]'))
        elif tamper in ("old-notes", "new-notes"):
            path = tmp_path / "CHANGELOG.md"
            path.write_text(
                path.read_text().replace("Existing release notes.", "rewritten history")
                if tamper == "old-notes"
                else path.read_text() + "unreviewed notes\n"
            )
        elif tamper == "lock":
            path = tmp_path / "uv.lock"
            path.write_text(path.read_text().replace('version = "1.3.0"', 'version = "9.0.0"'))
        elif tamper == "receipt":
            path = tmp_path / ".release.toml"
            path.write_text(path.read_text().replace(digest, "0" * 64))
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "chore(release): attempt preparation")
        candidate = await git(tmp_path, "rev-parse", "HEAD")
        if tamper == "new-main":
            await git(tmp_path, "switch", "main")
            (tmp_path / "other.txt").write_text("another main change\n")
            await git(tmp_path, "add", ".")
            await git(tmp_path, "commit", "-m", "feat: another change")
            base = await git(tmp_path, "rev-parse", "HEAD")
        with pytest.raises(ValueError):
            await check_integration(LocalGit(tmp_path).read, base, candidate)

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong_branch", [False, True])
def test_prepared_release_integrates_through_same_mcp_gate(
    tmp_path: Path, workspace_factory: WorkspaceFactory, wrong_branch: bool
) -> None:
    async def scenario() -> None:
        base, digest = await ready(tmp_path, workspace_factory)
        await prepare_release(tmp_path, base, digest)
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "chore(release): prepare 1.3.0")
        source = await git(tmp_path, "rev-parse", "HEAD")
        branch = "feat/hidden-release" if wrong_branch else "release/1.3.0"
        if wrong_branch:
            await git(tmp_path, "branch", "-m", branch)
        args: dict[str, object] = {
            "expected_source_branch": branch,
            "expected_source_head": source,
            "expected_main_head": base,
            "message": "chore(release): release 1.3.0",
        }
        async with client(tmp_path) as session:
            if wrong_branch:
                assert await failure(session, "merge_project_branch", args)
                assert await git(tmp_path, "rev-parse", "main") == base
            else:
                result = await success(session, "merge_project_branch", args)
                assert result["mode"] == "squash"
                assert result["assessments"] == ["previous"]
                assert await git(tmp_path, "rev-parse", "main^") == base

    asyncio.run(scenario())


def test_prepare_detects_untracked_external_changes_without_rolling_back_version(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def interrupted(root: Path, version: str) -> None:
        nonlocal calls
        calls += 1
        await update_version(root, version)
        (root / "unreviewed.txt").write_text("external concurrent change\n")

    monkeypatch.setattr("local_dev_mcp.tools.release._prepare.update_version", interrupted)

    async def scenario() -> None:
        base, digest = await ready(tmp_path, workspace_factory)
        with pytest.raises(ValueError, match=r"unrelated|unexpected|state"):
            await prepare_release(tmp_path, base, digest)
        assert 'version = "1.3.0"' in (tmp_path / "pyproject.toml").read_text()
        assert (tmp_path / ".changes/previous.toml").exists()
        assert await git(tmp_path, "rev-parse", "HEAD") == base
        assert calls == 1

    asyncio.run(scenario())


def test_prepare_rejects_parent_uv_workspace_before_any_project_or_parent_write(tmp_path: Path) -> None:
    root = tmp_path / "parent" / "child"
    root.mkdir(parents=True)

    async def factory(**options: object) -> Path:
        await initialize(root)
        return root

    async def scenario() -> None:
        base, digest = await ready(root, factory)
        parent = root.parent
        (parent / "pyproject.toml").write_text(
            '[project]\nname = "parent"\nversion = "0.1.0"\nrequires-python = ">=3.15"\n'
            '[tool.uv.workspace]\nmembers = ["child"]\n'
        )
        await asyncio.to_thread(
            subprocess.run,
            [
                "uv",
                "lock",
                "--offline",
                "--no-config",
                "--project",
                str(parent),
                "--cache-dir",
                str(tmp_path / "cache"),
            ],
            env=base_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        parent_lock = (parent / "uv.lock").read_bytes()
        project = (root / "pyproject.toml").read_bytes()
        with pytest.raises(ValueError):
            await prepare_release(root, base, digest)
        assert (parent / "uv.lock").read_bytes() == parent_lock
        assert (root / "pyproject.toml").read_bytes() == project
        assert (root / ".changes/previous.toml").exists()

    asyncio.run(scenario())
