"""M4 preparation only: independent Trail graphs in disposable nested Git worktrees."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from local_dev_mcp.execution import base_environment
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager

pytestmark = pytest.mark.integration


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    return result.stdout.strip()


def test_explicit_nested_worktree_roots_have_separate_graphs(tmp_path: Path) -> None:
    trail = shutil.which("trail", path=os.environ.get("PATH"))
    if not trail or not shutil.which("git"):
        pytest.skip("Git and Trail are needed for a real Worktree probe.")

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / ".gitignore").write_text(".wt/\n/graft/\n", encoding="utf-8")
    (root / "module.py").write_text('def marker() -> str:\n    return "main"\n', encoding="utf-8")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=M4 Probe",
        "-c",
        "user.email=m4-probe@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test: initialize isolated repository",
    )

    nested = root / ".wt" / "feature"
    _git(root, "worktree", "add", "-b", "feat/m4-probe", str(nested))
    (nested / "module.py").write_text('def marker() -> str:\n    return "feature"\n', encoding="utf-8")
    assert _git(root, "branch", "--show-current") == "main"
    assert _git(nested, "branch", "--show-current") == "feat/m4-probe"

    # Trail's .ignore generation is optional; keep the test checkout clean.
    # GRAFT_NO_IGNORE is an upstream-documented opt-out for ripgrep re-admit files.
    environment = {**base_environment(), "GRAFT_NO_IGNORE": "1"}
    main_index = IndexManager(Policy(root, (root,), ("trail",)), environment, executable=trail)
    work_index = IndexManager(Policy(nested, (nested,), ("trail",)), environment, executable=trail)

    async def scenario() -> None:
        try:
            for index in (main_index, work_index):
                outcome = await index.refresh()
                assert outcome["structural_state"] == "fresh", outcome
                assert outcome["ready_for_structural_search"]
        finally:
            await main_index.close()
            await work_index.close()

    asyncio.run(scenario())

    main_graph = root / "graft" / ".graph" / "wiring.json"
    work_graph = nested / "graft" / ".graph" / "wiring.json"
    assert main_graph.is_file() and work_graph.is_file()
    assert main_graph.read_bytes() != work_graph.read_bytes()
    assert _git(root, "status", "--porcelain") == ""
    assert "feature" in (nested / "module.py").read_text(encoding="utf-8")
    assert "main" in (root / "module.py").read_text(encoding="utf-8")
