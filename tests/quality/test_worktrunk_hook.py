"""Ensure the project Worktrunk Hook is discoverable without executing it."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_project_pre_merge_hook_is_visible_to_worktrunk(tmp_path: Path) -> None:
    wt = shutil.which("wt")
    git = shutil.which("git")
    if wt is None or git is None:
        pytest.skip("Worktrunk and Git CLIs are required on the test host")
    root = Path(__file__).resolve().parents[2]
    project_config = (root / ".config" / "wt.toml").read_text()
    assert "[pre-merge]" in project_config
    assert "python -m local_dev_mcp.quality --profile pre_merge" in project_config

    subprocess.run([git, "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "wt.toml").write_text(project_config)
    result = subprocess.run([wt, "-C", str(tmp_path), "hook", "show"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "quality" in result.stdout
    assert "pre-merge" in result.stdout
