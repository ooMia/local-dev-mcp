"""Exercise configured task selection and known-value privacy through real stdio."""

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.execution import base_environment
from local_dev_mcp.redaction import REDACTED
from local_dev_mcp.settings import Policy
from tests.support.workspace import configure

pytestmark = pytest.mark.integration

_PROJECT_SECRET = "synthetic+project/password=token"
_CONTROL_SECRET = "synthetic-control-token-7744"


def _configure_task(root: Path) -> None:
    configure(root, commands='["test.run.pytest"]')
    with (root / "config.toml").open("a") as policy:
        policy.write(
            '\n[jobs_environment]\npublic_names = ["PUBLIC_MODE", "CONTROL_PLANE_API_KEY"]\n'
            '\n[jobs.unit]\nkind = "test"\nlevel = "unit"\npermission = "test.run.pytest"\n'
            f'argv = [{json.dumps(sys.executable)}, "-I", "task_fixture.py"]\n'
            'default_targets = ["tests"]\ntimeout_seconds = 10\n'
        )
    (root / ".env").write_text(
        f"PROJECT_TOKEN={_PROJECT_SECRET}\nCONTROL_PLANE_API_KEY={_CONTROL_SECRET}\nPUBLIC_MODE=readable\n"
    )


def _parameters(target: Path, caller: Path, *, policy_edits: bool = False) -> StdioServerParameters:
    args = ["-I", "-m", "local_dev_mcp", "--workspace", str(target)]
    if policy_edits:
        args.append("--allow-policy-edits")
    return StdioServerParameters(command=sys.executable, args=[*args, "run"], cwd=caller, env=base_environment())


def test_real_stdio_task_selection_environment_redaction_and_request_recovery(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "선택한 workspace 'quoted'"
    caller.mkdir()
    target.mkdir()
    configure(caller)
    (caller / ".env").write_text("FASTMCP_LOG_LEVEL=INVALID_CALLER_CANARY\n")
    (caller / "task_fixture.py").write_text('raise RuntimeError("caller task must never run")\n')
    _configure_task(target)
    (target / "task_fixture.py").write_text(
        "import base64, json, os, sys\n"
        "from pathlib import Path\n"
        "from urllib.parse import quote\n"
        "token = os.environ['PROJECT_TOKEN']\n"
        "source = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines())\n"
        "print(json.dumps({'cwd': str(Path.cwd()), 'targets': sys.argv[1:], 'token': token,\n"
        "    'mode': os.environ['PUBLIC_MODE'], 'control_forwarded': 'CONTROL_PLANE_API_KEY' in os.environ}))\n"
        "print('from-source=' + source['CONTROL_PLANE_API_KEY'])\n"
        "print('quoted=' + quote(token, safe=''))\n"
        "print('encoded=' + base64.b64encode(token.encode()).decode(), file=sys.stderr)\n"
        "with Path('runs.txt').open('a') as runs: runs.write('run\\n')\n"
    )
    settings = ServerSettings.load(Policy.load(target))
    assert _PROJECT_SECRET not in repr(settings)
    assert _CONTROL_SECRET not in repr(settings)

    async def scenario() -> None:
        async with Client(
            _parameters(target, caller, policy_edits=True), read_timeout_seconds=15, cache=None
        ) as session:

            async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                response = await session.call_tool(name, arguments)
                assert not response.is_error, response.content
                assert response.structured_content is not None
                serialized = json.dumps(response.structured_content) + str(response.content)
                for value in (_PROJECT_SECRET, _CONTROL_SECRET, quote(_PROJECT_SECRET, safe="")):
                    assert value not in serialized
                assert base64.b64encode(_PROJECT_SECRET.encode()).decode() not in serialized
                return dict(response.structured_content)

            readiness = await call("get_tool_readiness")
            assert readiness["project"] == str(target.resolve())
            assert readiness["policy_edits_allowed"] is True
            catalogue = await call("list_project_tasks")
            assert catalogue["project"] == str(target.resolve())
            assert catalogue["policy_edits_allowed"] is True

            selector = "tests/unit_new.py::test_new[value]"
            arguments: dict[str, object] = {"task": "unit", "request_id": "selected-1", "targets": [selector]}
            result = await call("start_project_task", arguments)
            assert result["output_included"] is False and result["stdout"] == result["stderr"] == ""
            job_id = result["job_id"]
            assert isinstance(job_id, str)
            for _ in range(200):
                if result["state"] != "running":
                    break
                await asyncio.sleep(0.01)
                result = await call("get_project_task", {"job_id": job_id})
            assert result["state"] == "succeeded"
            assert result["exit_code"] == 0 and result["termination_confirmed"] is True
            assert result["targets"] == [selector] and result["all_tests"] is False
            assert result["output_complete"] is True
            assert result["output_included"] is False and result["stdout"] == result["stderr"] == ""
            result = await call("get_project_task", {"job_id": job_id, "include_output": True})
            assert result["output_included"] is True
            stdout = result["stdout"]
            assert isinstance(stdout, str)
            observed: dict[str, object] = json.loads(stdout.splitlines()[0])
            assert observed == {
                "cwd": str(target.resolve()),
                "targets": [selector],
                "token": REDACTED,
                "mode": "readable",
                "control_forwarded": False,
            }
            assert f"from-source={REDACTED}" in stdout
            assert f"quoted={REDACTED}" in stdout
            assert result["stderr"] == f"encoded={REDACTED}\n"
            repeated = await call("start_project_task", arguments)
            assert repeated["job_id"] == job_id
            assert repeated["output_included"] is False and repeated["stdout"] == repeated["stderr"] == ""
            assert (target / "runs.txt").read_text() == "run\n"

            denied = await session.call_tool("read_workspace_file", {"path": ".env"})
            assert denied.is_error
            assert _PROJECT_SECRET not in str(denied.content)
            assert _CONTROL_SECRET not in str(denied.content)

            # A different server/workspace must not expose retained job state.
            async with Client(_parameters(caller, target), read_timeout_seconds=15, cache=None) as other:
                missing = await other.call_tool("get_project_task", {"job_id": job_id})
                assert missing.is_error
                assert "unknown or expired" in str(missing.content)
                assert _PROJECT_SECRET not in str(missing.content)
                assert _CONTROL_SECRET not in str(missing.content)

    asyncio.run(scenario())
    assert not (caller / "runs.txt").exists()


@pytest.mark.parametrize("field", ["kind", "permission"])
def test_cli_invalid_task_configuration_never_echoes_rejected_values(tmp_path: Path, field: str) -> None:
    _configure_task(tmp_path)
    policy = tmp_path / "config.toml"
    original = 'kind = "test"' if field == "kind" else 'permission = "test.run.pytest"'
    policy.write_text(policy.read_text().replace(original, f'{field} = "{_PROJECT_SECRET}"'))
    result = subprocess.run(
        [sys.executable, "-I", "-m", "local_dev_mcp", "--workspace", str(tmp_path), "run"],
        env=base_environment(),
        input="",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 3
    assert result.stdout == ""
    assert f"jobs.unit.{field}" in result.stderr
    assert _PROJECT_SECRET not in result.stderr and _CONTROL_SECRET not in result.stderr
    assert "Traceback" not in result.stderr
