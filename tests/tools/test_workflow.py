"""Exercise the workflow CLI and installed hooks against isolated repositories."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_dev_mcp.execution import base_environment
from tests.support.git import WorkspaceFactory, git

pytestmark = pytest.mark.integration


def run_workflow(root: Path, *args: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "local_dev_mcp.tools.workflow", "--workspace", str(root), *args],
        env=base_environment() if environment is None else environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


async def setup(root: Path, factory: WorkspaceFactory, *, failing: str = "") -> str:
    await factory()
    await git(root, "branch", "-M", "main")
    config = ""
    for name in ("format_check", "lint", "typecheck", "unit", "integration"):
        program = (
            "from pathlib import Path; p=Path('checks.log'); "
            f"p.open('a').write('{name}\\n'); raise SystemExit({int(name == failing)})"
        )
        config += f"[jobs.{name}]\nargv = {json.dumps([sys.executable, '-c', program])}\n"
    (root / "config.toml").write_text(config)
    (root / ".gitignore").write_text("checks.log\n")
    await git(root, "add", ".")
    await git(root, "commit", "-m", "initial")
    return await git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("failing", ["", "lint"])
def test_quality_runs_registered_checks_and_propagates_failure(
    tmp_path: Path, workspace_factory: WorkspaceFactory, failing: str
) -> None:
    asyncio.run(setup(tmp_path, workspace_factory, failing=failing))
    result = run_workflow(tmp_path, "check")
    assert result.returncode == (1 if failing else 0), result.stderr
    expected = ["format_check", "lint"] if failing else ["format_check", "lint", "typecheck", "unit", "integration"]
    assert (tmp_path / "checks.log").read_text().splitlines() == expected


def test_installed_hooks_reject_failed_quality_and_missing_source_before_main_moves(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        initial = await setup(tmp_path, workspace_factory, failing="lint")
        installed = run_workflow(tmp_path, "install")
        assert installed.returncode == 0, installed.stderr
        (tmp_path / "data.txt").write_text("feature\n")
        await git(tmp_path, "add", "data.txt")
        refused = subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feat: example\n\nSource-PR: https://github.com/o/r/pull/12"],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert refused.returncode != 0
        assert await git(tmp_path, "rev-parse", "HEAD") == initial
        # Hook-driven ref and commit operations launch a separate Python process.
        await git(tmp_path, "switch", "-c", "feat/example", timeout=30)
        await git(tmp_path, "commit", "-m", "work in progress", timeout=30)
        assert await git(tmp_path, "rev-parse", "main") == initial
        await git(tmp_path, "switch", "main", timeout=30)
        (tmp_path / "message.txt").write_text("feat: missing source\n")
        missing = run_workflow(tmp_path, "hook", "commit-msg", str(tmp_path / "message.txt"))
        assert missing.returncode != 0
        assert "Source-PR" in missing.stderr
        (tmp_path / "message.txt").write_text("feat: example\n\nSource-PR: https://github.com/o/r/pull/12\n")
        assert run_workflow(tmp_path, "hook", "commit-msg", str(tmp_path / "message.txt")).returncode == 0

    asyncio.run(scenario())


def test_installer_preserves_existing_user_hook(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    asyncio.run(setup(tmp_path, workspace_factory))
    hook = tmp_path / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 42\n")
    result = run_workflow(tmp_path, "install")
    assert result.returncode == 0, result.stderr
    assert hook.read_text() == "#!/bin/sh\nexit 42\n"


def test_main_hook_rejects_partially_staged_inputs(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        (tmp_path / "data.txt").write_text("staged\n")
        await git(tmp_path, "add", "data.txt")
        (tmp_path / "data.txt").write_text("different working file\n")
        result = run_workflow(tmp_path, "hook", "pre-commit")
        assert result.returncode != 0
        assert not (tmp_path / "checks.log").exists()

    asyncio.run(scenario())


def test_no_verify_cannot_update_main_without_quality_approval(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        initial = await setup(tmp_path, workspace_factory)
        assert run_workflow(tmp_path, "install").returncode == 0
        (tmp_path / "data.txt").write_text("unvalidated\n")
        await git(tmp_path, "add", "data.txt")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "commit",
                "--no-verify",
                "-m",
                "feat: example\n\nSource-PR: https://github.com/o/r/pull/12",
            ],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0
        assert await git(tmp_path, "rev-parse", "main") == initial

    asyncio.run(scenario())


def test_approved_candidate_can_advance_main_and_stale_approval_is_refused(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        initial = await setup(tmp_path, workspace_factory)
        assert run_workflow(tmp_path, "install").returncode == 0
        await git(tmp_path, "switch", "-c", "feat/example", timeout=30)
        (tmp_path / "data.txt").write_text("validated\n")
        await git(tmp_path, "add", "data.txt")
        await git(tmp_path, "commit", "-m", "feat: example\n\nSource-PR: https://github.com/o/r/pull/12", timeout=30)
        candidate = await git(tmp_path, "rev-parse", "HEAD")
        refused = subprocess.run(
            ["git", "-C", str(tmp_path), "update-ref", "refs/heads/main", candidate, initial],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert refused.returncode != 0
        approved = run_workflow(tmp_path, "approve-main")
        assert approved.returncode == 0, approved.stderr
        await git(tmp_path, "update-ref", "refs/heads/main", candidate, initial, timeout=30)
        assert await git(tmp_path, "rev-parse", "main") == candidate
        (tmp_path / "data.txt").write_text("unapproved followup\n")
        await git(tmp_path, "add", "data.txt")
        await git(tmp_path, "commit", "-m", "fix: followup\n\nSource-PR: https://github.com/o/r/pull/12", timeout=30)
        next_head = await git(tmp_path, "rev-parse", "HEAD")
        stale = subprocess.run(
            ["git", "-C", str(tmp_path), "update-ref", "refs/heads/main", next_head, candidate],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert stale.returncode != 0
        assert await git(tmp_path, "rev-parse", "main") == candidate

    asyncio.run(scenario())


@pytest.mark.parametrize("leak", ["", ".changes/pending.toml", ".release.toml"])
def test_artifacts_reject_release_working_files(tmp_path: Path, workspace_factory: WorkspaceFactory, leak: str) -> None:
    import io
    import tarfile
    import zipfile

    asyncio.run(setup(tmp_path, workspace_factory))
    directory = tmp_path / "dist"
    directory.mkdir()
    with zipfile.ZipFile(directory / "probe.whl", "w") as wheel:
        wheel.writestr(leak or "probe/__init__.py", "data")
    with tarfile.open(directory / "probe.tar.gz", "w:gz") as source:
        entry = tarfile.TarInfo("probe/" + (leak or "src/probe/__init__.py"))
        entry.size = 4
        source.addfile(entry, io.BytesIO(b"data"))
    result = run_workflow(tmp_path, "artifacts", str(directory))
    assert result.returncode == (2 if leak else 0), result.stderr
    if leak:
        assert "leaked" in result.stderr


def test_candidate_with_missing_source_in_earlier_commit_is_refused(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/example")
        (tmp_path / "one.txt").write_text("one")
        await git(tmp_path, "add", "one.txt")
        await git(tmp_path, "commit", "-m", "feat: missing source")
        (tmp_path / "two.txt").write_text("two")
        await git(tmp_path, "add", "two.txt")
        await git(tmp_path, "commit", "-m", "feat: with source\n\nSource-PR: https://github.com/o/r/pull/12")
        result = run_workflow(tmp_path, "approve-main")
        assert result.returncode != 0
        assert "Source-PR" in result.stderr

    asyncio.run(scenario())


def test_workflow_child_preserves_explicit_github_auth_environment(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_dev_mcp.tools.workflow import run

    asyncio.run(setup(tmp_path, workspace_factory))
    monkeypatch.setenv("GH_TOKEN", "test-only-auth-canary")
    run(
        tmp_path,
        sys.executable,
        "-c",
        "import os,sys; sys.exit(0 if os.environ.get('GH_TOKEN') == 'test-only-auth-canary' else 42)",
    )


def test_commit_all_validates_gits_actual_temporary_index(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        (tmp_path / "tracked.txt").write_text("before\n")
        await git(tmp_path, "add", "tracked.txt")
        await git(tmp_path, "commit", "-m", "initial tracked file")
        assert run_workflow(tmp_path, "install").returncode == 0
        (tmp_path / "tracked.txt").write_text("after\n")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "commit",
                "-am",
                "feat: tracked update\n\nSource-PR: https://github.com/o/r/pull/12",
            ],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert await git(tmp_path, "show", "main:tracked.txt") == "after"

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_approval_rejects_hidden_working_tree_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, flag: str
) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        await git(tmp_path, "switch", "-c", "feat/example")
        (tmp_path / "tracked.txt").write_text("tested candidate\n")
        await git(tmp_path, "add", "tracked.txt")
        await git(tmp_path, "commit", "-m", "feat: candidate\n\nSource-PR: https://github.com/o/r/pull/12")
        await git(tmp_path, "update-index", flag, "tracked.txt")
        (tmp_path / "tracked.txt").write_text("different working copy\n")
        result = run_workflow(tmp_path, "approve-main")
        assert result.returncode != 0
        assert not (tmp_path / "checks.log").exists()

    asyncio.run(scenario())


def test_linked_worktree_uses_common_main_guard_and_approval(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        initial = await setup(tmp_path, workspace_factory)
        assert run_workflow(tmp_path, "install").returncode == 0
        linked = tmp_path / "linked"
        # Workflow hooks may start an interpreter while other tests run concurrently.
        await git(tmp_path, "worktree", "add", "-b", "feat/linked", str(linked), timeout=30)
        (linked / "data.txt").write_text("linked candidate\n")
        await git(linked, "add", "data.txt")
        await git(linked, "commit", "-m", "feat: linked\n\nSource-PR: https://github.com/o/r/pull/12")
        candidate = await git(linked, "rev-parse", "HEAD")
        refused = subprocess.run(
            ["git", "-C", str(linked), "update-ref", "refs/heads/main", candidate, initial],
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert refused.returncode != 0
        approved = run_workflow(linked, "approve-main")
        assert approved.returncode == 0, approved.stderr
        assert (tmp_path / ".git/workflow-approval.json").exists()
        await git(tmp_path, "switch", "--detach")
        await git(linked, "update-ref", "refs/heads/main", candidate, initial)
        assert await git(tmp_path, "rev-parse", "main") == candidate

    asyncio.run(scenario())


def test_installer_overrides_global_hooks_only_for_this_repository(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    asyncio.run(setup(tmp_path, workspace_factory, failing="lint"))
    home = tmp_path / "home"
    home.mkdir()
    global_config = home / ".gitconfig"
    global_config.write_text("[core]\n hooksPath = /preserve/user/hooks\n")
    before = global_config.read_bytes()
    environment = {**base_environment(), "HOME": str(home)}
    result = run_workflow(tmp_path, "install", environment=environment)
    assert result.returncode == 0, result.stderr
    assert global_config.read_bytes() == before
    checked = run_workflow(tmp_path, "doctor", environment=environment)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    details = json.loads(checked.stdout)
    assert details["ready"] is True
    assert details["effective_hooks_path"] == str(tmp_path / ".git/local-dev-mcp/hooks")
    assert not (tmp_path / ".git/hooks/pre-commit").exists()


def test_preapproved_squash_reuses_quality_for_exact_main_tree(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        assert run_workflow(tmp_path, "install").returncode == 0
        await git(tmp_path, "switch", "-c", "feat/example")
        (tmp_path / "data.txt").write_text("squash candidate\n")
        await git(tmp_path, "add", "data.txt")
        await git(tmp_path, "commit", "-m", "development checkpoint")
        approved = run_workflow(tmp_path, "approve-main", "--squash")
        assert approved.returncode == 0, approved.stderr
        assert len((tmp_path / "checks.log").read_text().splitlines()) == 5
        await git(tmp_path, "switch", "main")
        await git(tmp_path, "merge", "--squash", "feat/example")
        await git(tmp_path, "commit", "-m", "feat: result\n\nSource-PR: https://github.com/o/r/pull/12")
        assert len((tmp_path / "checks.log").read_text().splitlines()) == 5
        assert await git(tmp_path, "show", "main:data.txt") == "squash candidate"

    asyncio.run(scenario())


@pytest.mark.parametrize("duplicate", [False, True])
def test_installer_overrides_worktree_scope_and_preserves_first_backup(
    tmp_path: Path, workspace_factory: WorkspaceFactory, duplicate: bool
) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        await git(tmp_path, "config", "--local", "extensions.worktreeConfig", "true")
        if duplicate:
            await git(tmp_path, "config", "--local", "--add", "extensions.worktreeConfig", "true")
        await git(tmp_path, "config", "--local", "core.hooksPath", "/old/repository/hooks")
        await git(tmp_path, "config", "--worktree", "core.hooksPath", "/old/worktree/hooks")
        if duplicate:
            await git(tmp_path, "config", "--worktree", "extensions.worktreeConfig", "false")
        result = run_workflow(tmp_path, "install")
        assert result.returncode == 0, result.stderr
        expected = str(tmp_path / ".git/local-dev-mcp/hooks")
        assert await git(tmp_path, "config", "--local", "--get", "core.hooksPath") == expected
        assert await git(tmp_path, "config", "--worktree", "--get", "core.hooksPath") == expected
        backup = tmp_path / ".git/local-dev-mcp/hooks-config-backup.json"
        saved = backup.read_bytes()
        assert "/old/repository/hooks" in saved.decode()
        assert "/old/worktree/hooks" in saved.decode()
        assert run_workflow(tmp_path, "install").returncode == 0
        assert backup.read_bytes() == saved
        assert run_workflow(tmp_path, "doctor").returncode == 0

    asyncio.run(scenario())


def test_doctor_detects_missing_hooks_and_configuration_drift(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await setup(tmp_path, workspace_factory)
        before = run_workflow(tmp_path, "doctor")
        assert before.returncode == 2
        assert json.loads(before.stdout)["ready"] is False
        assert run_workflow(tmp_path, "install").returncode == 0
        assert run_workflow(tmp_path, "doctor").returncode == 0
        path = tmp_path / ".git/local-dev-mcp/hooks/reference-transaction"
        path.unlink()
        missing = run_workflow(tmp_path, "doctor")
        assert missing.returncode == 2
        assert json.loads(missing.stdout)["ready"] is False
        assert run_workflow(tmp_path, "install").returncode == 0
        await git(tmp_path, "config", "--local", "core.hooksPath", "/changed/hooks")
        drift = run_workflow(tmp_path, "doctor")
        assert drift.returncode == 2
        assert json.loads(drift.stdout)["effective_hooks_path"] == "/changed/hooks"

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="Directory symlinks need Windows developer mode.")
def test_doctor_rejects_symlinked_managed_directory(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    asyncio.run(setup(tmp_path, workspace_factory))
    assert run_workflow(tmp_path, "install").returncode == 0
    managed = tmp_path / ".git/local-dev-mcp"
    moved = tmp_path / ".git/moved-managed-hooks"
    managed.rename(moved)
    managed.symlink_to(moved, target_is_directory=True)
    result = run_workflow(tmp_path, "doctor")
    assert result.returncode == 2
    assert json.loads(result.stdout)["ready"] is False
