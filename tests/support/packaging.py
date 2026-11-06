"""Opt-in wheel installation, owned and cleaned by one pytest session."""

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from local_dev_mcp.execution import base_environment


@dataclass(frozen=True)
class InstalledPackage:
    python: Path
    cli: Path
    wrapper: Path


@pytest.fixture(scope="session")
def installed_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[InstalledPackage]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("Packaging validation requires uv on PATH. Install uv before running -m packaging.")
    project = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="package-", dir=tmp_path_factory.getbasetemp()) as directory:
        root = Path(directory)
        cache = root / "cache"

        def run(stage: str, *args: str) -> None:
            try:
                result = subprocess.run(
                    [uv, "--no-config", "--cache-dir", str(cache), "--no-python-downloads", *args],
                    cwd=project,
                    env=base_environment(),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                pytest.fail(f"Packaging {stage} failed ({type(error).__name__}); check uv and build/network access.")
            if result.returncode:
                pytest.fail(
                    f"Packaging {stage} failed; check build dependencies/network access.\n{result.stderr[-2000:]}"
                )

        # Export exact runtime versions without including this editable project.
        requirements = root / "requirements.txt"
        run("dependency export", "export", "--locked", "--no-dev", "--no-emit-project", "-o", str(requirements))
        dist = root / "dist"
        run("build", "build", "--out-dir", str(dist))
        wheels = list(dist.glob("*.whl"))
        assert len(wheels) == 1, "Expected one project wheel."
        environment = root / "venv"
        run("environment creation", "venv", "--python", sys.executable, str(environment))
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        run("installation", "pip", "install", "--python", str(python), "-r", str(requirements), str(wheels[0]))
        suffix = ".exe" if os.name == "nt" else ""
        yield InstalledPackage(python, scripts / f"local-dev-mcp{suffix}", scripts / f"local-dev-mcp-gpg{suffix}")
