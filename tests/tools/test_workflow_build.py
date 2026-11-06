"""Inspect real uv artifacts rather than treating exclude configuration as proof."""

import asyncio
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from local_dev_mcp.execution import base_environment
from tests.support.git import WorkspaceFactory

pytestmark = pytest.mark.integration


def test_actual_uv_build_excludes_working_metadata_and_includes_workflow_cli(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    asyncio.run(workspace_factory())
    project = Path(__file__).resolve().parents[2]
    dist = tmp_path / "artifacts"
    uv = shutil.which("uv")
    assert uv is not None
    built = subprocess.run(
        [uv, "build", "--out-dir", str(dist)],
        cwd=project,
        env=base_environment(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    checked = subprocess.run(
        [sys.executable, "-m", "local_dev_mcp.tools.workflow", "--workspace", str(tmp_path), "artifacts", str(dist)],
        env=base_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    with zipfile.ZipFile(next(dist.glob("*.whl"))) as wheel:
        assert "local_dev_mcp/tools/workflow.py" in wheel.namelist()
