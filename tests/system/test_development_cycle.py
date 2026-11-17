"""Complete a small create/test/fix/review workflow using an in-process MCP server."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import git, initialize
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def test_stdio_creates_tests_fixes_formats_and_reviews_a_small_change(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "별도 개발 대상 'quoted'"
    caller.mkdir()
    target.mkdir()
    (caller / "config.toml").write_text("[invalid caller config")
    (caller / ".env").write_text("FASTMCP_LOG_LEVEL=invalid-caller-canary\n")
    (caller / "untouched.txt").write_text("caller remains untouched\n")
    configure(target, commands='["git", "python.run.pytest", "python.run.ruff"]')
    (target / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["unit: pure behavior"]\ntestpaths=["tests"]\n'
    )
    recipes = {
        "unit": (
            "test",
            "python.run.pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "--verbosity=-1",
                "--tb=short",
                "--no-header",
                "-rfE",
                "-n",
                "0",
                "-m",
                "unit",
            ],
        ),
        "lint": (
            "lint",
            "python.run.ruff",
            [sys.executable, "-m", "ruff", "check", "--quiet", "--output-format", "concise", "demo_text.py", "tests"],
        ),
        "format": (
            "format",
            "python.run.ruff",
            [sys.executable, "-m", "ruff", "format", "--quiet", "demo_text.py", "tests"],
        ),
    }
    with (target / "config.toml").open("a") as configuration:
        for name, (kind, permission, argv) in recipes.items():
            configuration.write(f"\n[jobs.{name}]\nkind={json.dumps(kind)}\npermission={json.dumps(permission)}\n")
            configuration.write(f"argv={json.dumps(argv)}\n")
            if kind == "test":
                configuration.write("level='unit'\ndefault_targets=['tests']\n")
    buggy = "def normalize_spaces(text: str) -> str:\n    return text.strip()\n"
    fixed = "def normalize_spaces(text: str) -> str:\n    return ' '.join(text.split())\n"
    test_content = (
        "import pytest\n\nfrom demo_text import normalize_spaces\n\n"
        "@pytest.mark.unit\ndef test_collapses_runs() -> None:\n"
        "    assert normalize_spaces('  a   b  ') == 'a b'\n"
    )

    async def scenario() -> None:
        await initialize(target)
        with (target / ".git/info/exclude").open("a") as excluded:
            excluded.write("\nconfig.toml\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n")
        await git(target, "add", "--", "pyproject.toml")
        await git(target, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "fixture baseline")
        head_before = await git(target, "rev-parse", "HEAD")
        policy = Policy.load(target)
        fastmcp_server = create_server(policy)
        async with Client(
            fastmcp_server._mcp_server,  # type: ignore[reportPrivateUsage]
            read_timeout_seconds=30,
            cache=None,
        ) as client:

            async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result.content
                assert result.structured_content is not None
                return dict(result.structured_content)

            async def run(task: str, request: str) -> dict[str, object]:
                arguments: dict[str, object] = {"task": task, "request_id": request}
                if task == "unit":
                    arguments["targets"] = ["tests/test_demo_text.py::test_collapses_runs"]
                result = await call("start_project_task", arguments)
                async with asyncio.timeout(15):
                    while result["state"] == "running":
                        await asyncio.sleep(0.02)
                        result = await call("get_project_task", {"job_id": result["job_id"]})
                assert result["output_included"] is False
                assert result["stdout"] == result["stderr"] == ""
                if task != "unit" and result["state"] != "succeeded":
                    diagnostic = await call("get_project_task", {"job_id": result["job_id"], "include_output": True})
                    pytest.fail(str(diagnostic))
                return result

            created = await call(
                "create_workspace_file", {"path": "demo_text.py", "content": buggy, "request_id": "create-source"}
            )
            assert created["outcome"] == "applied" and isinstance(created["read_ref"], str)
            test = await call(
                "create_workspace_file",
                {
                    "path": "tests/test_demo_text.py",
                    "content": test_content,
                    "request_id": "create-regression",
                    "create_parents": True,
                },
            )
            assert test["outcome"] == "applied" and test["created_directories"] == ["tests"]
            failed = await run("unit", "unit-red")
            assert failed["state"] == "failed" and failed["exit_code"] == 1
            details = await call("get_project_task", {"job_id": failed["job_id"], "include_output": True})
            assert details["output_included"] is True
            assert "test_collapses_runs" in str(details["stdout"]) and "AssertionError" in str(details["stdout"])
            changed = await call(
                "replace_workspace_file",
                {"path": "demo_text.py", "read_ref": created["read_ref"], "replacement": fixed},
            )
            assert changed["outcome"] == "applied"
            assert (await run("format", "format-fix"))["state"] == "succeeded"
            observed = await call("read_workspace_file", {"path": "demo_text.py"})
            assert isinstance(observed["read_ref"], str) and "text.split()" in str(observed["content"])
            assert (await run("lint", "lint-fix"))["state"] == "succeeded"
            assert (await run("unit", "unit-green"))["state"] == "succeeded"
            repeated = await call(
                "create_workspace_file", {"path": "demo_text.py", "content": buggy, "request_id": "create-source"}
            )
            assert repeated["replayed"] is True
            assert (target / "demo_text.py").read_text() != buggy

            temporary = await call(
                "create_workspace_file",
                {"path": "obsolete.txt", "content": "temporary\n", "request_id": "create-obsolete"},
            )
            deletion = {"path": "obsolete.txt", "read_ref": temporary["read_ref"], "request_id": "delete-obsolete"}
            assert (await call("delete_workspace_file", deletion))["outcome"] == "applied"
            assert (await call("delete_workspace_file", deletion))["replayed"] is True
            assert not (target / "obsolete.txt").exists()

            paths = ["demo_text.py", "tests/test_demo_text.py"]
            assert (await call("stage_project_paths", {"paths": paths}))["success"] is True
            review = await call("get_project_diff", {"staged": True, "paths": paths})
            assert review["complete"] is True
            assert "normalize_spaces" in str(review["patch"]) and "test_collapses_runs" in str(review["patch"])
            assert (await call("unstage_project_paths", {"paths": paths}))["success"] is True
            assert (await call("get_project_head"))["head"] == head_before
        assert await git(target, "diff", "--cached", "--name-only") == ""
        assert await git(target, "rev-parse", "HEAD") == head_before

    asyncio.run(scenario())
    assert (caller / "untouched.txt").read_text() == "caller remains untouched\n"
    assert not (caller / "demo_text.py").exists()
