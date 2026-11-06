"""Exercise typed task calls, independent readiness and redacted results over MCP."""

import asyncio
import json
import sys
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.mcp import failure, success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def task_workspace(root: Path, *, permission: str = "python.run.pytest") -> None:
    configure(root, commands=json.dumps([permission]))
    program = (
        "import json,os,sys; from pathlib import Path; "
        "p=Path('invocations'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "print(json.dumps(sys.argv[1:])); print(os.environ['PROJECT_SECRET']); "
        "print('server='+str('CONTROL_PLANE_API_KEY' in os.environ)); "
        "print('flag='+os.environ['PUBLIC_FLAG']); "
        "print(os.environ['PROJECT_SECRET'],file=sys.stderr)"
    )
    with (root / "config.toml").open("a") as config:
        config.write(
            "\n[jobs_environment]\npublic_names=['PUBLIC_FLAG']\n"
            "[jobs.unit]\nkind='test'\nlevel='unit'\npermission='python.run.pytest'\n"
            f"argv={json.dumps([sys.executable, '-c', program])}\ndefault_targets=['tests']\n"
            "[jobs.unavailable]\nkind='lint'\npermission='missing.run.linter'\nargv=['no-such-task-tool']\n"
        )
    (root / ".env").write_text(
        "PROJECT_SECRET=synthetic-long-private-token\nCONTROL_PLANE_API_KEY=synthetic-server-only-key\nPUBLIC_FLAG=1\n"
    )


def test_mcp_task_targets_redaction_and_response_loss_recovery(tmp_path: Path) -> None:
    task_workspace(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            definitions = {tool.name: tool for tool in await client.list_tools()}
            assert definitions["start_project_task"].annotations is not None
            assert definitions["start_project_task"].annotations.read_only_hint is False
            catalogue = await success(client, "list_project_tasks")
            tasks = catalogue["tasks"]
            assert isinstance(tasks, list)
            # The generic MCP assertion helper returns object-valued JSON.
            records = cast(list[dict[str, object]], tasks)
            assert any(task["name"] == "unit" and task["ready"] for task in records)
            assert any(task["name"] == "unavailable" and not task["ready"] for task in records)
            request: dict[str, object] = {
                "task": "unit",
                "request_id": "test-recovery-1",
                "targets": ["tests/test_parser.py::test_empty[with-space and --flag]"],
            }
            initial = await success(client, "start_project_task", request)
            assert initial["output_included"] is False and initial["stdout"] == initial["stderr"] == ""
            job_id = initial["job_id"]
            assert isinstance(job_id, str)
            result = initial
            for _ in range(100):
                if result["state"] != "running":
                    break
                await asyncio.sleep(0.02)
                result = await success(client, "get_project_task", {"job_id": job_id})
            assert result["state"] == "succeeded", result
            assert result["exit_code"] == 0
            assert result["termination_confirmed"] is True
            assert result["targets"] == request["targets"]
            assert result["output_included"] is False and result["stdout"] == result["stderr"] == ""
            result = await success(client, "get_project_task", {"job_id": job_id, "include_output": True})
            assert result["output_included"] is True
            stdout = result["stdout"]
            assert isinstance(stdout, str)
            assert "tests/test_parser.py::test_empty[with-space and --flag]" in stdout
            assert "[REDACTED]" in stdout and "server=False" in stdout and "flag=1" in stdout
            assert "synthetic-long-private-token" not in json.dumps(result)
            assert "synthetic-server-only-key" not in json.dumps(result)
            assert result["stderr"] == "[REDACTED]\n"
            repeated = await success(client, "start_project_task", request)
            assert repeated["job_id"] == job_id
            assert repeated["output_included"] is False and repeated["stdout"] == repeated["stderr"] == ""
            assert (tmp_path / "invocations").read_text() == "x"
            mismatch = await failure(client, "start_project_task", {**request, "targets": ["tests/other.py"]})
            assert "request" in mismatch.lower()
            raw = await client.call_tool("get_project_task", {"job_id": job_id, "include_output": True})
            assert "[REDACTED]" not in str(raw.content)
            assert "succeeded" in str(raw.content)

    asyncio.run(scenario())


def test_invalid_or_implicit_test_selection_never_launches_or_expands_scope(tmp_path: Path) -> None:
    task_workspace(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            for index, selection in enumerate(({}, {"targets": []}, {"targets": ["--help"]}, {"targets": ["@args"]})):
                message = await failure(
                    client, "start_project_task", {"task": "unit", "request_id": f"invalid-{index}", **selection}
                )
                assert message
            bad_type = await failure(
                client, "start_project_task", {"task": "unit", "request_id": "typed-1", "targets": "tests"}
            )
            assert "Invalid tool arguments" in bad_type
            assert not (tmp_path / "invocations").exists()
            result = await success(
                client, "start_project_task", {"task": "unit", "request_id": "explicit-all", "all_tests": True}
            )
            assert result["targets"] == ["tests"]
            assert result["all_tests"] is True

    asyncio.run(scenario())


def test_output_inclusion_requires_an_explicit_json_boolean(tmp_path: Path) -> None:
    task_workspace(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            result = await success(
                client, "start_project_task", {"task": "unit", "request_id": "strict-output", "all_tests": True}
            )
            for invalid in ("true", 1, None):
                message = await failure(
                    client, "get_project_task", {"job_id": result["job_id"], "include_output": invalid}
                )
                assert "Invalid tool arguments" in message and "include_output" in message
            metadata = await success(client, "get_project_task", {"job_id": result["job_id"]})
            details = await success(client, "get_project_task", {"job_id": result["job_id"], "include_output": True})
            assert metadata["output_included"] is False and metadata["stdout"] == ""
            assert details["output_included"] is True and "[REDACTED]" in str(details["stdout"])
            assert (tmp_path / "invocations").read_text() == "x"

    asyncio.run(scenario())


def test_broad_runner_permission_does_not_authorize_registered_operation(tmp_path: Path) -> None:
    task_workspace(tmp_path, permission="python")

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            message = await failure(
                client,
                "start_project_task",
                {"task": "unit", "request_id": "ungranted", "targets": ["tests/test_one.py"]},
            )
            assert "permission" in message.lower() or "allowed" in message.lower()
            assert not (tmp_path / "invocations").exists()

    asyncio.run(scenario())


def test_real_pytest_selects_one_unit_and_reports_marker_mismatch_as_failure(tmp_path: Path) -> None:
    configure(tmp_path, commands='["python.run.pytest"]')
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["unit: pure contracts", "integration: resources"]\n'
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_selection.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.unit\ndef test_selected():\n    Path('selected-ran').touch()\n"
        "@pytest.mark.unit\ndef test_unselected():\n    raise AssertionError('unexpected broad scope')\n"
        "@pytest.mark.integration\ndef test_other_layer():\n    Path('wrong-layer-ran').touch()\n"
    )
    with (tmp_path / "config.toml").open("a") as config:
        config.write(
            "[jobs.unit]\nkind='test'\nlevel='unit'\npermission='python.run.pytest'\n"
            f"argv={json.dumps([sys.executable, '-m', 'pytest', '-q', '-n', '0', '-m', 'unit'])}\n"
            "default_targets=['tests']\n"
        )

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:

            async def run(request_id: str, target: str) -> dict[str, object]:
                result = await success(
                    client, "start_project_task", {"task": "unit", "request_id": request_id, "targets": [target]}
                )
                async with asyncio.timeout(15):
                    while result["state"] == "running":
                        await asyncio.sleep(0.02)
                        result = await success(client, "get_project_task", {"job_id": result["job_id"]})
                return await success(client, "get_project_task", {"job_id": result["job_id"], "include_output": True})

            selected = await run("one-unit", "tests/test_selection.py::test_selected")
            assert selected["state"] == "succeeded" and selected["exit_code"] == 0, selected
            assert "1 passed" in str(selected["stdout"])
            assert (tmp_path / "selected-ran").exists()
            mismatch = await run("wrong-layer", "tests/test_selection.py::test_other_layer")
            assert mismatch["state"] == "failed" and mismatch["exit_code"] == 5
            assert not (tmp_path / "wrong-layer-ran").exists()

    asyncio.run(scenario())


def test_recipe_readiness_is_frozen_before_the_first_tool_call(tmp_path: Path) -> None:
    configure(tmp_path, commands='["project.run.lint"]')
    executable = tmp_path / "runner"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    with (tmp_path / "config.toml").open("a") as config:
        config.write("[jobs.lint]\nkind='lint'\npermission='project.run.lint'\nargv=['./runner']\n")
    server = create_server(Policy.load(tmp_path))
    executable.unlink()

    async def scenario() -> None:
        async with Client(server) as client:
            catalogue = await success(client, "list_project_tasks")
            assert isinstance(catalogue["tasks"], list)
            records = cast(list[dict[str, object]], catalogue["tasks"])
            assert records[0]["ready"] is True
            result = await success(client, "start_project_task", {"task": "lint", "request_id": "deleted-runner"})
            assert result["state"] == "start_failed" and result["termination_confirmed"] is True

    asyncio.run(scenario())
