"""Real subprocess checks for the fixed-argv optional Trail adapter."""

import asyncio
import sys
from pathlib import Path

import pytest

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, TrailProcessResult

pytestmark = pytest.mark.integration


class ProcessHarness(IndexManager):
    async def run_command(self, executable: Path, args: tuple[str, ...], timeout: float) -> TrailProcessResult:
        return await self._run(executable, args, timeout)


def test_argv_subprocess_uses_fixed_cwd_and_returns_bounded_output(tmp_path: Path) -> None:
    index = ProcessHarness(Policy(tmp_path, (tmp_path,), ("trail",)), {"PATH": ""})

    async def scenario() -> None:
        result = await index.run_command(
            Path(sys.executable),
            ("-c", "import os; print(os.getcwd()); print('ready')"),
            5,
        )
        assert result.exit_code == 0
        assert result.error is None
        assert str(tmp_path) in result.output
        assert "ready" in result.output

    asyncio.run(scenario())


def test_output_limit_kills_process_without_returning_partial_output(tmp_path: Path) -> None:
    index = ProcessHarness(Policy(tmp_path, (tmp_path,), ("trail",)), {"PATH": ""})

    async def scenario() -> None:
        result = await index.run_command(
            Path(sys.executable),
            ("-c", "print('x'*150000)"),
            5,
        )
        assert result.error == "output_limit"
        assert result.output == ""
        assert result.exit_code is None

    asyncio.run(scenario())


def test_timeout_reports_failure_not_success(tmp_path: Path) -> None:
    index = ProcessHarness(Policy(tmp_path, (tmp_path,), ("trail",)), {"PATH": ""})

    async def scenario() -> None:
        result = await index.run_command(
            Path(sys.executable),
            ("-c", "import time; time.sleep(5)"),
            0.05,
        )
        assert result.error == "timeout"
        assert result.exit_code is None

    asyncio.run(scenario())
