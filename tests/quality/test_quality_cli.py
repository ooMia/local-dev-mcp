"""Direct CLI execution contract using disposable Git repositories."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_dev_mcp.quality.__main__ import run_profile
from tests.support.git import git_sync

pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> str:
    return git_sync(root, *args)


def _workspace(root: Path, code: str) -> None:
    (root / ".config").mkdir()
    (root / "config.toml").write_text(
        '[filesystem]\nallowed_paths = ["."]\n[system]\nallowed_commands = ["task.quality"]\n'
        '[jobs.check]\nkind = "lint"\npermission = "task.quality"\nargv = '
        + json.dumps([sys.executable, "-c", code])
        + "\n"
    )
    (root / ".config" / "quality.toml").write_text(
        'schema_version = 1\n[profiles.pre_merge]\nchecks = ["check"]\nrequire_clean = true\n'
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "config.toml", ".config/quality.toml")
    _git(root, "commit", "-q", "-m", "test fixture")


def test_shared_profile_executes_exact_registered_argv(tmp_path: Path) -> None:
    _workspace(tmp_path, 'print("quality check passed")')
    original = _git(tmp_path, "rev-parse", "HEAD")
    assert run_profile(tmp_path, "pre_merge") == 0
    assert _git(tmp_path, "rev-parse", "HEAD") == original


def test_failed_check_blocks_profile_and_preserves_head(tmp_path: Path) -> None:
    _workspace(tmp_path, "raise SystemExit(3)")
    original = _git(tmp_path, "rev-parse", "HEAD")
    assert run_profile(tmp_path, "pre_merge") == 1
    assert _git(tmp_path, "rev-parse", "HEAD") == original


def test_dirty_source_is_refused_before_running_check(tmp_path: Path) -> None:
    _workspace(tmp_path, "from pathlib import Path; Path('executed').touch()")
    (tmp_path / "untracked").write_text("user's file")
    with pytest.raises(ValueError, match="not clean"):
        run_profile(tmp_path, "pre_merge")
    assert not (tmp_path / "executed").exists()
    assert (tmp_path / "untracked").read_text() == "user's file"


def test_modifying_check_is_not_a_valid_pass(tmp_path: Path) -> None:
    _workspace(tmp_path, "from pathlib import Path; Path('modified').write_text('test')")
    with pytest.raises(ValueError, match="state changed"):
        run_profile(tmp_path, "pre_merge")
    assert (tmp_path / "modified").read_text() == "test"


def test_unknown_profile_is_refused(tmp_path: Path) -> None:
    _workspace(tmp_path, "print('should never run')")
    with pytest.raises(ValueError, match="Unknown quality profile"):
        run_profile(tmp_path, "not_registered")


def test_python_module_entrypoint_runs_shared_profile(tmp_path: Path) -> None:
    _workspace(tmp_path, 'print("quality check passed")')
    process = subprocess.run(
        [sys.executable, "-m", "local_dev_mcp.quality", "--profile", "pre_merge"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "quality: PASS pre_merge (1 checks)" in process.stdout


@pytest.mark.usefixtures("hostile_git_global")
def test_test_owned_git_and_quality_profile_ignore_invalid_global_signing(tmp_path: Path) -> None:
    _workspace(tmp_path, 'print("quality check passed")')
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert run_profile(tmp_path, "pre_merge") == 0
    assert _git(tmp_path, "rev-parse", "HEAD") == head


@pytest.mark.usefixtures("hostile_git_global")
def test_poisoned_global_signing_breaks_an_unisolated_git_commit(tmp_path: Path) -> None:
    """Negative control: prove the injected Git setting is not a no-op."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "unisolated"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "gpg" in result.stderr.lower()
