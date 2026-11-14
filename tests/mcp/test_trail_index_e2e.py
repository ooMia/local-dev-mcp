"""Optional real-Trail integration in a disposable project, never the active checkout."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from local_dev_mcp.execution import base_environment
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager
from local_dev_mcp.tools.workspace._files import WorkspaceFiles

pytestmark = pytest.mark.integration


def test_real_trail_status_refresh_and_post_edit_sync(tmp_path: Path) -> None:
    binary = shutil.which("trail", path=os.environ.get("PATH"))
    if binary is None:
        pytest.skip("The Trail CLI is not installed on the test host.")

    policy = Policy(tmp_path, (tmp_path,), ("trail",))
    index = IndexManager(policy, base_environment(), executable=binary, refresh_on_write=True)
    files = WorkspaceFiles(policy, change_callback=index.changed)

    async def scenario() -> None:
        try:
            # Starting with no graph must not be reported as Fresh.
            initial = await index.status()
            assert initial["structural_state"] in ("missing", "unknown")

            first = await index.refresh()
            assert first["outcome"] in ("updated", "already_fresh"), first
            assert first["ready_for_structural_search"]

            created = await files.create_file(
                "example.py",
                "def alpha() -> int:\n    return 1\n",
                "trail-e2e-creation",
            )
            assert created.changed and created.outcome == "applied"
            await index.wait_for_pending_refresh()
            after = await index.status()
            assert after["structural_state"] == "fresh", after
            assert after["ready_for_structural_search"], after

            replay = await files.create_file(
                "example.py",
                "def alpha() -> int:\n    return 1\n",
                "trail-e2e-creation",
            )
            assert replay.replayed
            assert not (await index.status())["pending_changes"]
        finally:
            await index.close()
            files.clear()

    asyncio.run(scenario())
