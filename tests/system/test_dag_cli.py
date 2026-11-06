"""Exercise option discovery before pytest has selected the tests directory."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from local_dev_mcp.execution import base_environment

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("separate_value", [True, False])
def test_dag_options_load_with_an_existing_report_directory_and_no_test_path(separate_value: bool) -> None:
    project = Path(__file__).resolve().parents[2]
    artifacts = project / ".lab"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="pytest-options-", dir=artifacts) as directory:
        report = str(Path(directory).relative_to(project))
        report_args = ["--dag-report", report] if separate_value else [f"--dag-report={report}"]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pytest",
                "-n",
                "0",
                "-m",
                "dag",
                "--dag-workers",
                "4",
                "--dag-node",
                "head_after",
                "--dag-fail-fast",
                *report_args,
                "--collect-only",
            ],
            cwd=project,
            env=base_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "test_dag_workflow.py" in result.stdout
    assert "unrecognized arguments" not in result.stderr
