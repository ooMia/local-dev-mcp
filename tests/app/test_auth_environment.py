"""Host GitHub CLI account locations must not leak ambient authentication tokens."""

from pathlib import Path

import pytest

from local_dev_mcp import auth_environment
from local_dev_mcp.tools.git import _remote

pytestmark = pytest.mark.unit


def test_account_locations_accept_only_absolute_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GH_CONFIG_DIR", ".local/gh")
    monkeypatch.setenv("XDG_STATE_HOME", ".local/state")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GH_TOKEN", "never-propagate-me")
    locations = auth_environment.account_locations()
    assert locations["HOME"] == str(tmp_path / "home")
    assert locations["XDG_CONFIG_HOME"] == str(tmp_path / "config")
    assert "GH_CONFIG_DIR" not in locations
    assert "XDG_STATE_HOME" not in locations
    assert "GH_TOKEN" not in locations


def test_missing_home_uses_os_account_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(auth_environment, "os_account_home", lambda: str(tmp_path / "account"))
    monkeypatch.setenv("GH_TOKEN", "must-remain-isolated")
    locations = auth_environment.account_locations()
    assert locations["HOME"] == str(tmp_path / "account")
    assert "GH_TOKEN" not in locations
    process = _remote.env()
    assert process["HOME"] == locations["HOME"]
    assert process["GH_PROMPT_DISABLED"] == "1"


def test_remote_environment_ignores_relative_gh_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GH_CONFIG_DIR", ".config/gh")
    monkeypatch.setenv("XDG_STATE_HOME", ".local/state")
    process = _remote.env()
    assert process["HOME"] == str(tmp_path / "home")
    assert "GH_CONFIG_DIR" not in process
    assert "XDG_STATE_HOME" not in process


def test_no_available_account_home_does_not_invent_workspace_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(auth_environment, "os_account_home", lambda: None)
    assert "HOME" not in auth_environment.account_locations()
