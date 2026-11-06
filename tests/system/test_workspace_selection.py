"""Use an explicit workspace through a real CLI and independent stdio client."""

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_dev_mcp.execution import base_environment
from tests.support.git import configure_identity, git, initialize
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_workspace_keeps_file_and_git_work_out_of_the_caller(tmp_path: Path, relative: bool) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "명시 대상 'quoted' [workspace]"
    caller.mkdir()
    target.mkdir()
    selected = os.path.relpath(target, caller) if relative else str(target)
    payload = "selected workspace update\n"

    async def scenario() -> None:
        for root in (caller, target):
            configure(root, commands='["git"]')
            await initialize(root)
            with (root / ".git/info/exclude").open("a") as excluded:
                excluded.write("config.toml\n")
            (root / "shared.txt").write_text("caller baseline\n" if root == caller else "target baseline\n")
            await git(root, "add", "--", "shared.txt")
            await git(root, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "baseline")
        configure_identity(target, name="Selected Writer", email="selected@example.invalid")
        # A library loading the invocation directory's dotenv would reject this canary.
        with (caller / ".env").open("a") as environment:
            environment.write("FASTMCP_LOG_LEVEL=INVALID_CALLER_CANARY\n")
        caller_environment = (caller / ".env").read_bytes()
        caller_policy = (caller / "config.toml").read_bytes()
        caller_head = await git(caller, "rev-parse", "HEAD")
        target_before = await git(target, "rev-parse", "HEAD")
        (target / "shared.txt").write_text(payload)

        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-I", "-m", "local_dev_mcp", "--workspace", selected, "run"],
                cwd=caller,
                env=base_environment(),
            ),
            read_timeout_seconds=15,
            cache=None,
        ) as session:

            async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                result = await session.call_tool(name, arguments)
                assert not result.is_error, result.content
                assert result.structured_content is not None
                content: dict[str, object] = dict(result.structured_content)
                assert content["project"] == str(target.resolve())
                return content

            readiness = await call("get_tool_readiness")
            assert readiness["scope"] == "startup_snapshot_only"
            read = await call("read_workspace_file", {"path": "shared.txt"})
            assert read["content"] == payload
            assert read["version"] == hashlib.sha256(payload.encode()).hexdigest()
            assert isinstance(read["read_ref"], str)
            status = await call("get_project_status")
            assert status["head"] == target_before
            staged = await call("stage_project_paths", {"paths": ["shared.txt"]})
            assert staged["success"] is True
            committed = await call("commit_project_changes", {"message": "explicit workspace commit"})
            assert committed["success"] is True and committed["head_before"] == target_before
            assert committed["signing_requested"] is False
            target_after = committed["head_after"]
            assert isinstance(target_after, str) and target_after != target_before
            assert (await call("get_project_head"))["head"] == target_after

        assert await git(target, "rev-parse", "HEAD") == target_after
        assert await git(target, "show", "HEAD:shared.txt") == payload.removesuffix("\n")
        assert await git(target, "show", "-s", "--format=%an") == "Selected Writer"
        assert await git(caller, "rev-parse", "HEAD") == caller_head
        assert await git(caller, "status", "--porcelain") == ""
        assert (caller / "shared.txt").read_text() == "caller baseline\n"
        assert (caller / ".env").read_bytes() == caller_environment
        assert (caller / "config.toml").read_bytes() == caller_policy

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("denied", "not allowed"),
        ("missing_policy", "configuration file is missing"),
        ("invalid_policy", "Invalid TOML syntax"),
        ("missing_environment", "[.env]"),
    ],
)
def test_selected_workspace_failure_never_falls_back_to_a_valid_caller(
    tmp_path: Path, failure: str, message: str
) -> None:
    caller = tmp_path / "valid caller"
    target = tmp_path / "selected target"
    caller.mkdir()
    target.mkdir()
    configure(caller)
    configure(target, paths="[]" if failure == "denied" else '["."]')
    if failure == "missing_policy":
        (target / "config.toml").unlink()
    elif failure == "invalid_policy":
        (target / "config.toml").write_text("[")
    elif failure == "missing_environment":
        (target / ".env").unlink()
    (caller / "untouched.txt").write_text("caller remains untouched")
    caller_policy = (caller / "config.toml").read_bytes()

    result = subprocess.run(
        [sys.executable, "-I", "-m", "local_dev_mcp", "--workspace", str(target), "run"],
        cwd=caller,
        env=base_environment(),
        input="",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 3, result.stderr
    assert result.stdout == ""
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert (caller / "untouched.txt").read_text() == "caller remains untouched"
    assert (caller / "config.toml").read_bytes() == caller_policy
