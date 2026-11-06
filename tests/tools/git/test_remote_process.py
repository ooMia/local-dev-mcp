"""Exercise the actual bounded async gh command adapter without GitHub credentials."""

import asyncio
from pathlib import Path

import pytest

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import _remote
from tests.support.git import WorkspaceFactory

pytestmark = pytest.mark.integration


def test_remote_process_adapter_accepts_bounded_stdin(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        fake = tmp_path / "fake-gh"
        fake.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
        fake.chmod(0o755)
        original = _remote.executable

        def executable(name: str) -> Path:
            return fake if name == "gh" else original(name)

        monkeypatch.setattr(_remote, "executable", executable)
        policy = Policy(tmp_path, (tmp_path,), ("git", "gh", "gh.pr.create"))
        remote = _remote.Remote(ServerSettings.load(policy))
        result = await remote.gh("fake", input_text="body with UTF-8: 정형 데이터\n")
        assert result.code == 0
        assert result.output == "body with UTF-8: 정형 데이터\n"
        git_result = await remote.git("rev-parse", "--show-toplevel")
        assert git_result.output.strip() == str(tmp_path)

    asyncio.run(scenario())
