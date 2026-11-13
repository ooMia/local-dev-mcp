"""The Worktrunk subprocess boundary must fail closed and reap its direct child."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git.worktrunk import WorktrunkAdapter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure_mode", ["timeout", "cancelled", "io_error"])
def test_worktrunk_reaps_process_after_communication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    class Stream:
        async def read(self, size: int) -> bytes:
            if failure_mode == "timeout":
                raise TimeoutError
            if failure_mode == "cancelled":
                raise asyncio.CancelledError
            raise OSError("output pipe closed")

    class Process:
        stdout = Stream()
        returncode: int | None = None
        kills = 0
        waits = 0

        def kill(self) -> None:
            self.kills += 1
            self.returncode = -9

        async def wait(self) -> int:
            self.waits += 1
            assert self.returncode is not None
            return self.returncode

    process = Process()
    calls = 0

    async def launch(*argv: str, **options: Any) -> Process:
        nonlocal calls
        calls += 1
        assert argv[1:3] == ("-C", str(tmp_path))
        assert argv[-2:] == ("--format", "json")
        assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    (tmp_path / ".env").write_text("")
    settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write")))
    adapter = WorktrunkAdapter(settings, binary="wt-test")

    async def scenario() -> None:
        if failure_mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await adapter._run("remove", str(tmp_path / ".lab" / "target"), write=True)  # pyright: ignore[reportPrivateUsage]
        else:
            expected = "timed out" if failure_mode == "timeout" else "could not be started or observed"
            with pytest.raises(ToolError, match=expected):
                await adapter._run("remove", str(tmp_path / ".lab" / "target"), write=True)  # pyright: ignore[reportPrivateUsage]
        assert calls == 1
        assert process.kills == 1
        assert process.waits == 1

    asyncio.run(scenario())


def test_worktrunk_stops_collecting_oversized_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stream:
        reads = 0

        async def read(self, size: int) -> bytes:
            self.reads += 1
            assert self.reads <= 17  # The 18th chunk would be an unnecessary read.
            return b"x" * min(size, 65536)

    class Process:
        stdout = Stream()
        returncode: int | None = None
        kills = 0
        waits = 0

        async def communicate(self) -> tuple[bytes, None]:
            raise AssertionError("Unbounded communicate() must not read the whole stream")

        def kill(self) -> None:
            self.kills += 1
            self.returncode = -9

        async def wait(self) -> int:
            self.waits += 1
            assert self.returncode is not None
            return self.returncode

    process = Process()
    launched = 0

    async def launch(*argv: str, **options: Any) -> Process:
        nonlocal launched
        launched += 1
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    (tmp_path / ".env").write_text("")
    settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write")))
    adapter = WorktrunkAdapter(settings, binary="wt-test")

    async def scenario() -> None:
        with pytest.raises(ToolError, match="exceeded the 1 MiB limit"):
            await adapter._run("remove", str(tmp_path / ".lab" / "target"), write=True)  # pyright: ignore[reportPrivateUsage]
        assert launched == 1
        assert process.stdout.reads == 17
        assert process.kills == 1
        assert process.waits == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "exit_code", "message"),
    [
        (b'{"status":"ok"}', 1, "did not complete successfully"),
        (b"invalid-json", 1, "Worktrunk failed"),
        (b"invalid-json", 0, "did not return valid JSON"),
        (b"true", 0, "unsupported JSON"),
    ],
)
def test_worktrunk_rejects_failed_or_untrusted_cli_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    exit_code: int,
    message: str,
) -> None:
    class Stream:
        read_count = 0

        async def read(self, size: int) -> bytes:
            self.read_count += 1
            return payload if self.read_count == 1 else b""

    class Process:
        stdout = Stream()
        returncode = exit_code
        waits = 0

        def kill(self) -> None:
            raise AssertionError("A finished child should not be killed")

        async def wait(self) -> int:
            self.waits += 1
            return exit_code

    process = Process()
    launches = 0

    async def launch(*argv: str, **options: Any) -> Process:
        nonlocal launches
        launches += 1
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    (tmp_path / ".env").write_text("")
    settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "wt", "git.worktree.write")))
    adapter = WorktrunkAdapter(settings, binary="wt-test")

    async def scenario() -> None:
        with pytest.raises(ToolError, match=message):
            await adapter._run("remove", str(tmp_path / ".lab" / "target"), write=True)  # pyright: ignore[reportPrivateUsage]
        assert launches == 1
        assert process.stdout.read_count == 2
        assert process.waits == 1

    asyncio.run(scenario())
