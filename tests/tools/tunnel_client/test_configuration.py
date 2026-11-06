import sys
from pathlib import Path

import pytest

from local_dev_mcp.app.cli import main
from local_dev_mcp.settings import ConfigurationError
from local_dev_mcp.tools.git import command_environment
from local_dev_mcp.tools.tunnel_client._client import tunnel_environment
from tests.support.workspace import WorkspaceConfigFactory

pytestmark = pytest.mark.integration


def test_tunnel_preserves_account_paths_and_explicit_shell_gh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "ambient-secret")
    monkeypatch.setenv("GH_TOKEN", "ambient-github-token")
    monkeypatch.setenv("GITHUB_TOKEN", "alternate-github-token")
    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "github-cli-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nCONTROL_PLANE_API_KEY=file-secret\nOTHER_SECRET=hidden\n"
    )
    env = tunnel_environment(tmp_path)
    assert env["CONTROL_PLANE_API_KEY"] == "file-secret"
    assert "OTHER_SECRET" not in env
    assert "GIT_DIR" not in env
    assert env["GH_TOKEN"] == "ambient-github-token"
    assert "GITHUB_TOKEN" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg")
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "github-cli-config")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "state")
    assert "CONTROL_PLANE_API_KEY" not in command_environment()
    (tmp_path / ".env").write_text("CONTROL_PLANE_TUNNEL_ID=tunnel_test\n")
    with pytest.raises(ConfigurationError, match="CONTROL_PLANE_API_KEY"):
        tunnel_environment(tmp_path)


def test_explicit_dotenv_gh_token_is_forwarded_without_ambient_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-win")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-alternate")
    token = "ghp_test_only_not_a_real_token"
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n"
        "CONTROL_PLANE_API_KEY=example-control-plane-key\n"
        f"GH_TOKEN={token}\n"
    )
    result = tunnel_environment(tmp_path)
    assert result["GH_TOKEN"] == token
    assert result["GH_TOKEN"] != "ambient-token-must-not-win"
    assert "GITHUB_TOKEN" not in result


def test_blank_dotenv_gh_token_uses_shell_export_without_persisting_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_shell_only_fake_token")
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n"
        "CONTROL_PLANE_API_KEY=fake-control-key\n"
        "GH_TOKEN=\n"
    )
    result = tunnel_environment(tmp_path)
    assert result["GH_TOKEN"] == "ghp_shell_only_fake_token"
    assert "ghp_shell_only_fake_token" not in (tmp_path / ".env").read_text()


def test_invalid_shell_export_is_rejected_without_printing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "token with spaces"
    monkeypatch.setenv("GH_TOKEN", secret)
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nCONTROL_PLANE_API_KEY=fake-control-key\n"
    )
    with pytest.raises(ConfigurationError) as error:
        tunnel_environment(tmp_path)
    assert "GH_TOKEN" in str(error.value)
    assert secret not in str(error.value)


def test_bad_explicit_gh_token_fails_without_disclosing_value(tmp_path: Path) -> None:
    invalid = "do not expose this value"
    (tmp_path / ".env").write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n"
        "CONTROL_PLANE_API_KEY=example-control-plane-key\n"
        f"GH_TOKEN={invalid}\n"
    )
    with pytest.raises(ConfigurationError) as failure:
        tunnel_environment(tmp_path)
    assert "GH_TOKEN" in str(failure.value)
    assert invalid not in str(failure.value)


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
    workspace_config: WorkspaceConfigFactory,
) -> None:
    workspace_config()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "connect"])
    assert main() == 3
    assert "tunnel-client is not allowed" in capsys.readouterr().err
