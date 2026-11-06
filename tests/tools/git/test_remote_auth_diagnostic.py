"""Read-only gh login diagnostics contain no credentials or command output."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import _remote
from local_dev_mcp.tools.git.facades import remote_pr

if TYPE_CHECKING:
    from local_dev_mcp.app.runtime import ServerSettings

pytestmark = pytest.mark.unit


@dataclass
class FakeRemote:
    root: Path
    auth_exit: int = 4
    repo_ok: bool = False
    git_ok: bool = True
    auth_probes: int = 0
    repository_probes: int = 0
    transport_probes: int = 0

    async def run(self, *args: Any, **kwargs: Any) -> _remote.Result:
        assert args == ("gh", "auth", "status", "--hostname", "github.com")
        assert kwargs == {"allow_failure": True}
        self.auth_probes += 1
        return _remote.Result(self.auth_exit, "")

    async def repo(self) -> tuple[str, str]:
        self.repository_probes += 1
        if not self.repo_ok:
            raise ToolError("Repo check failed.")
        return "example/project", "https://github.com/example/project"

    async def main_sha(self, repository: str) -> str:
        assert repository == "example/project"
        return "a" * 40

    async def branch(self, branch: str) -> str | None:
        assert branch == "main"
        self.transport_probes += 1
        if not self.git_ok:
            raise ToolError("Mock Git remote transport failure.")
        return "a" * 40


def run_report(monkeypatch: pytest.MonkeyPatch, fake: FakeRemote, home: str) -> remote_pr.RemoteAuthReport:
    def remote_factory(_settings: object) -> FakeRemote:
        return fake

    def auth_env() -> dict[str, str]:
        return {"HOME": home}

    monkeypatch.setattr(remote_pr, "Remote", remote_factory)
    monkeypatch.setattr(_remote, "env", auth_env)
    settings = cast(
        "ServerSettings",
        SimpleNamespace(policy=Policy(fake.root, (fake.root,), ("git", "gh", "git.remote.read"))),
    )
    return asyncio.run(remote_pr.DiagnoseRemoteAuthTool(settings).execute())


def test_no_login_reports_unauthenticated_without_returning_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeRemote(tmp_path)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    report = run_report(monkeypatch, fake, "/var/other-home")
    assert report["gh_auth_exit_code"] == 4
    assert report["repo_access"] == "not_checked"
    assert fake.auth_probes == 1 and fake.repository_probes == 0
    assert "gh" in report["action"].lower() or "GitHub" in report["action"]


def test_logged_in_origin_access_is_independently_verified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRemote(tmp_path, auth_exit=0, repo_ok=True)
    report = run_report(monkeypatch, fake, "/var/other-home")
    assert report["gh_auth_exit_code"] == 0
    assert report["repo_access"] == "matched"
    assert report["git_transport"] == "matched"
    assert fake.transport_probes == 1
    assert fake.auth_probes == 1 and fake.repository_probes == 1


def test_api_auth_success_but_git_credential_failure_is_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeRemote(tmp_path, auth_exit=0, repo_ok=True, git_ok=False)
    report = run_report(monkeypatch, fake, "/var/other-home")
    assert report["gh_auth_exit_code"] == 0
    assert report["repo_access"] == "matched"
    assert report["git_transport"] == "unavailable"
    assert "credential helper" in report["action"]
    assert fake.transport_probes == 1


def test_logged_in_but_inaccessible_repo_has_distinct_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeRemote(tmp_path, auth_exit=0, repo_ok=False)
    report = run_report(monkeypatch, fake, "/var/other-home")
    assert report["gh_auth_exit_code"] == 0
    assert report["repo_access"] == "unavailable_or_mismatched"


def test_workspace_home_is_rejected_before_gh_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRemote(tmp_path, auth_exit=0, repo_ok=True)
    report = run_report(monkeypatch, fake, str(tmp_path / ".local"))
    assert report["home_inside_workspace"] is True
    assert report["gh_auth_exit_code"] is None
    assert fake.auth_probes == 0
    assert fake.repository_probes == 0


def test_relative_gh_config_is_reported_but_not_exposed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRemote(tmp_path, auth_exit=4, repo_ok=False)
    monkeypatch.setenv("GH_CONFIG_DIR", ".local/gh-secret")
    report = run_report(monkeypatch, fake, "/var/other-home")
    assert report["gh_config_dir"] == "relative_ignored"
    assert ".local/gh-secret" not in str(report)
