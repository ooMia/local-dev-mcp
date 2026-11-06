"""Regression checks for the local PostgreSQL 18 Compose reset boundary."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "postgres"


def test_compose_uses_postgresql_18_volume_location_and_loopback_port() -> None:
    compose = (INFRA / "compose.yaml").read_text(encoding="utf-8")
    assert "image: docker.io/library/postgres:18\n" in compose
    assert "postgres_data:/var/lib/postgresql\n" in compose
    assert "postgres_data:/var/lib/postgresql/data" not in compose
    assert '"127.0.0.1:${B3S_DB_PORT:-55432}:5432"' in compose
    assert "${B3S_DB_PASSWORD:?" in compose


def test_reset_script_has_valid_bash_syntax_and_requires_destructive_flag() -> None:
    script = INFRA / "reset-to-18.sh"
    text = script.read_text(encoding="utf-8")
    assert "--discard-data" in text
    assert "compose=(podman compose -p postgres -f compose.yaml)" in text
    assert text.index('"${compose[@]}" pull postgres') < text.index('"${compose[@]}" down --volumes')
    if (bash := shutil.which("bash")) is None:
        pytest.skip("Bash unavailable for syntax-only validation")
    process = subprocess.run((bash, "-n", str(script)), capture_output=True, text=True, check=False, timeout=10)
    assert process.returncode == 0, process.stderr
