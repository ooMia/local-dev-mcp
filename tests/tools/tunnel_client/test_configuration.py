import sys
from pathlib import Path

import pytest

from local_dev_mcp.cli import main
from local_dev_mcp.settings import ConfigurationError
from local_dev_mcp.tools.git import command_environment
from local_dev_mcp.tools.tunnel_client._client import tunnel_environment
from tests.support.git import WorkspaceConfigFactory


def test_tunnel_uses_only_dotenv_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "ambient-secret")
    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nCONTROL_PLANE_API_KEY=file-secret\nOTHER_SECRET=hidden\n"
    )
    env = tunnel_environment(tmp_path)
    assert env["CONTROL_PLANE_API_KEY"] == "file-secret"
    assert "OTHER_SECRET" not in env
    assert "GIT_DIR" not in env
    assert "CONTROL_PLANE_API_KEY" not in command_environment()
    (tmp_path / ".env").write_text("CONTROL_PLANE_TUNNEL_ID=tunnel_test\n")
    with pytest.raises(ConfigurationError, match="CONTROL_PLANE_API_KEY"):
        tunnel_environment(tmp_path)


def test_misplaced_secret_is_not_reported_as_tunnel_id(tmp_path: Path) -> None:
    secret = "sk-test-not-a-real-key"
    (tmp_path / ".env").write_text(f"CONTROL_PLANE_TUNNEL_ID={secret}\nCONTROL_PLANE_API_KEY=another-test-value\n")
    with pytest.raises(ConfigurationError) as error:
        tunnel_environment(tmp_path)
    assert "CONTROL_PLANE_TUNNEL_ID" in str(error.value)
    assert secret not in str(error.value)


def test_connect_requires_command_permission_before_reading_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdio_workspace: WorkspaceConfigFactory,
) -> None:
    stdio_workspace()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "connect"])
    assert main() == 3
    assert "tunnel-client is not allowed" in capsys.readouterr().err
