"""Common gate reads exact Git snapshots and never blesses missing decisions."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_dev_mcp.tools.release import check_integration, read_snapshot
from local_dev_mcp.tools.release._git import LocalGit
from tests.support.git import WorkspaceFactory, git
from tests.support.release import feature, record_text, release_workspace

pytestmark = pytest.mark.integration


def test_snapshot_check_keeps_worktree_and_refs_unchanged(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        head = await feature(tmp_path)
        before = await git(tmp_path, "status", "--porcelain=v1")
        result = await check_integration(LocalGit(tmp_path).read, base, head)
        assert result["kind"] == "change"
        assert result["assessments"] == ["example"]
        assert (await read_snapshot(LocalGit(tmp_path).read, head)).plan()["maximum_impact"] == "minor"
        assert await git(tmp_path, "status", "--porcelain=v1") == before
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


def test_inherited_assessments_do_not_cover_new_change(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory, pending=True)
        head = await feature(tmp_path, assessment=False)
        with pytest.raises(ValueError, match="new assessment"):
            await check_integration(LocalGit(tmp_path).read, base, head)
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation", ["modify", "delete", "disable", "remove-config", "symlink-config", "change-version"]
)
def test_gate_rejects_assessment_and_config_bypasses(
    tmp_path: Path, workspace_factory: WorkspaceFactory, operation: str
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory, pending=True)
        await feature(tmp_path)
        previous = tmp_path / ".changes/previous.toml"
        project = tmp_path / "pyproject.toml"
        if operation == "modify":
            previous.write_text(record_text(impact="patch"))
        elif operation == "delete":
            previous.unlink()
        elif operation == "disable":
            project.write_text(project.read_text().replace("enabled = true", "enabled = false"))
        elif operation == "remove-config":
            project.write_text(project.read_text().split("[tool.local-dev-mcp.release]")[0])
        elif operation == "symlink-config":
            content = project.read_text()
            project.unlink()
            (tmp_path / "other.toml").write_text(content)
            project.symlink_to("other.toml")
        else:
            project.write_text(project.read_text().replace('version = "1.2.3"', 'version = "1.3.0"'))
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "attempt bypass")
        head = await git(tmp_path, "rev-parse", "HEAD")
        with pytest.raises(ValueError):
            await check_integration(LocalGit(tmp_path).read, base, head)
        assert await git(tmp_path, "rev-parse", "main") == base

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["symlink", "nested", "bad-name", "non-utf8", "oversize", "blank"])
def test_invalid_record_tree_cannot_pass_the_common_gate(
    tmp_path: Path, workspace_factory: WorkspaceFactory, invalid: str
) -> None:
    async def scenario() -> None:
        base = await release_workspace(tmp_path, workspace_factory)
        await feature(tmp_path)
        path = tmp_path / ".changes/example.toml"
        if invalid == "symlink":
            path.unlink()
            path.symlink_to("../feature.txt")
        elif invalid == "nested":
            nested = tmp_path / ".changes/nested"
            nested.mkdir()
            path.rename(nested / path.name)
        elif invalid == "bad-name":
            path.rename(path.with_name("invalid_name.toml"))
        elif invalid == "non-utf8":
            path.write_bytes(b"\xff")
        elif invalid == "oversize":
            path.write_text("#" * 16385)
        else:
            path.write_text(record_text().replace('reason = "Existing public calls remain valid"', 'reason = ""'))
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "invalid assessment")
        with pytest.raises(ValueError):
            await check_integration(LocalGit(tmp_path).read, base, await git(tmp_path, "rev-parse", "HEAD"))

    asyncio.run(scenario())


def test_snapshot_authorizes_inherited_inputs_before_content_reads(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await release_workspace(tmp_path, workspace_factory, pending=True)
        head = await feature(tmp_path)
        reads: list[tuple[str, ...]] = []
        repository = LocalGit(tmp_path)

        async def read(*args: str) -> bytes:
            reads.append(args)
            return await repository.read(*args)

        def authorize(path: str) -> None:
            if path == ".changes/previous.toml":
                raise ValueError("Restricted release input")

        with pytest.raises(ValueError, match="Restricted"):
            await read_snapshot(read, head, authorize=authorize)
        assert not any(args == ("show", f"{head}:.changes/previous.toml") for args in reads)

    asyncio.run(scenario())


@pytest.mark.parametrize("ref", ["HEAD~1", "main", "-x", "a" * 39])
def test_common_checker_rejects_revision_expressions(ref: str) -> None:
    async def read(*args: str) -> bytes:
        pytest.fail("Invalid refs must fail before Git access")

    with pytest.raises(ValueError, match="SHA"):
        asyncio.run(check_integration(read, ref, "b" * 40))


def test_cli_reads_committed_plan_without_loading_workspace_environment(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def setup() -> str:
        base = await release_workspace(tmp_path, workspace_factory)
        await feature(tmp_path)
        return base

    base = asyncio.run(setup())
    (tmp_path / ".env").write_text("GIT_COMMIT_SIGN=invalid\nFASTMCP_ENV_FILE=/not-a-file\n")
    # A dirty uncommitted assessment must not alter a committed plan.
    (tmp_path / ".changes/example.toml").write_text(record_text(impact="major"))
    command = [sys.executable, "-m", "local_dev_mcp.tools.release", "--workspace", str(tmp_path)]
    result = subprocess.run([*command, "plan"], capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(result.stdout)["maximum_impact"] == "minor"
    checked = subprocess.run(
        [*command, "check", "--base", base], capture_output=True, text=True, check=True, timeout=20
    )
    assert json.loads(checked.stdout)["kind"] == "change"
    denied = subprocess.run(
        [*command, "check", "--base", "HEAD~1"], capture_output=True, text=True, check=False, timeout=20
    )
    assert denied.returncode == 2
    assert "GIT_COMMIT_SIGN" not in denied.stderr


def test_unknown_full_commit_is_a_safe_validation_error(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await release_workspace(tmp_path, workspace_factory)
        with pytest.raises(ValueError):
            await read_snapshot(LocalGit(tmp_path).read, "f" * 40)

    asyncio.run(scenario())
