"""Scoped GitHub credential helper uses the authenticated gh process only for remote Git calls."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import _remote

if TYPE_CHECKING:
    from local_dev_mcp.app.runtime import ServerSettings

pytestmark = pytest.mark.integration


def test_git_credential_uses_scoped_gh_helper_without_global_configuration(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Git shell helper executable layout differs on native Windows")

    fake_gh = tmp_path / "mock gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "auth" ] && [ "$2" = "git-credential" ] && [ "$3" = "get" ]; then\n'
        "  printf 'username=example\\npassword=mock-only\\n'\n"
        "else\n"
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    options = _remote.scoped_gh_credential_options(fake_gh)
    assert "credential.helper=" not in options
    assert "credential.https://github.com.helper=" in options
    assert all("--global" not in part for part in options)

    result = subprocess.run(
        ["git", *options, "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    assert "username=example" in result.stdout
    assert "password=mock-only" in result.stdout


def test_only_network_git_commands_receive_gh_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("Mock shell command uses POSIX shebang")
    fake_git = tmp_path / "fake git"
    fake_git.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_git.chmod(0o755)
    fake_gh = tmp_path / "fake gh"
    fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    def executable(name: str) -> Path:
        if name == "gh":
            return fake_gh
        raise AssertionError(name)

    monkeypatch.setattr(_remote, "executable", executable)

    class MockSettings:
        policy = Policy(tmp_path, (tmp_path,), ("git", "gh"))

        def require_git(self) -> Path:
            return fake_git

    remote = _remote.Remote(cast("ServerSettings", MockSettings()))

    async def scenario() -> None:
        network = await remote.git("ls-remote", "--heads", "origin", "refs/heads/feat/example")
        assert network.code == 0
        assert "credential.https://github.com.helper=" in network.output
        assert "auth git-credential" in network.output
        local = await remote.git("rev-parse", "--show-toplevel")
        assert local.code == 0
        assert "credential.https://github.com.helper=" not in local.output

    asyncio.run(scenario())


def test_credential_helper_requires_gh_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git = tmp_path / "git"

    class MockSettings:
        policy = Policy(tmp_path, (tmp_path,), ("git",))

        def require_git(self) -> Path:
            return fake_git

    remote = _remote.Remote(cast("ServerSettings", MockSettings()))
    with pytest.raises(ToolError):
        asyncio.run(remote.git("ls-remote", "origin"))
