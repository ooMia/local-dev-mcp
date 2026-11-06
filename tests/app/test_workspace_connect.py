"""Keep an explicit workspace through real preflight and test-owned Tunnel launches."""

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter

from local_dev_mcp.errors import ExitCode
from local_dev_mcp.execution import base_environment
from local_dev_mcp.settings import Policy
from tests.support.workspace import configure

if TYPE_CHECKING:
    from local_dev_mcp.app.runtime import ServerSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix", reason="test-owned Tunnel executable uses a POSIX shebang"),
]

TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"
TUNNEL_KEY = "test-target-tunnel-key-never-print"


def tunnel_workspace(root: Path, *, allowed: bool = True) -> None:
    configure(root, commands='["tunnel-client"]', paths='["."]' if allowed else "[]")
    (root / ".env").write_text(
        f"CONTROL_PLANE_TUNNEL_ID={TUNNEL_ID}\nCONTROL_PLANE_API_KEY={TUNNEL_KEY}\n", encoding="utf-8"
    )


def fake_tunnel(bindir: Path, launcher: Path, marker: Path) -> dict[str, str]:
    bindir.mkdir()
    binary = bindir / "tunnel-client"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, shlex, subprocess, sys\n"
        "from pathlib import Path\n"
        "arguments = sys.argv[1:]\n"
        "command = arguments[arguments.index('--mcp.command') + 1]\n"
        "assert command.startswith('command=')\n"
        "child = shlex.split(command.removeprefix('command='))\n"
        "observed = {'cwd': str(Path.cwd().resolve()), 'argv': child,\n"
        "            'tunnel_id': os.environ['CONTROL_PLANE_TUNNEL_ID'],\n"
        "            'tunnel_key': os.environ['CONTROL_PLANE_API_KEY']}\n"
        f"completed = subprocess.run(child, cwd={str(launcher)!r}, env=dict(os.environ),\n"
        "                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)\n"
        "observed['child_exit_code'] = completed.returncode\n"
        "observed['child_stderr'] = completed.stderr\n"
        f"Path({str(marker)!r}).write_text(json.dumps(observed), encoding='utf-8')\n"
        "raise SystemExit(completed.returncode)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return {**base_environment(), "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}


def connect(
    launcher: Path, target: str, environment: dict[str, str], *, allow_policy_edits: bool = False
) -> subprocess.CompletedProcess[str]:
    flags = ["--allow-policy-edits"] if allow_policy_edits else []
    return subprocess.run(
        [sys.executable, "-I", "-m", "local_dev_mcp", "--workspace", target, *flags, "connect"],
        cwd=launcher,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("allow_policy_edits", [False, True])
def test_connect_pins_target_for_preflight_tunnel_and_child_even_if_child_cwd_changes(
    tmp_path: Path, relative: bool, allow_policy_edits: bool
) -> None:
    launcher = tmp_path / "launcher"
    target = tmp_path / "작업 대상 'quoted'"
    launcher.mkdir()
    target.mkdir()
    tunnel_workspace(target)
    # Both the parent and a restarted child must ignore the launcher's invalid configuration.
    (launcher / "config.toml").write_text("[invalid caller config", encoding="utf-8")
    (launcher / ".env").write_text("FASTMCP_LOG_LEVEL=invalid-caller-canary\n", encoding="utf-8")
    original = {name: (launcher / name).read_bytes() for name in ("config.toml", ".env")}
    marker = target / "tunnel-observation.json"
    environment = fake_tunnel(tmp_path / "bin", launcher, marker)
    value = os.path.relpath(target, launcher) if relative else str(target)

    result = connect(launcher, value, environment, allow_policy_edits=allow_policy_edits)

    assert result.returncode == ExitCode.SUCCESS, result.stderr
    assert result.stdout == ""
    assert TUNNEL_KEY not in result.stderr
    observed = TypeAdapter(dict[str, object]).validate_json(marker.read_text(encoding="utf-8"))
    assert observed["cwd"] == str(target.resolve())
    assert observed["argv"] == [
        sys.executable,
        "-I",
        "-m",
        "local_dev_mcp",
        "--workspace",
        str(target.resolve()),
        *(["--allow-policy-edits"] if allow_policy_edits else []),
        "run",
    ]
    assert observed["tunnel_id"] == TUNNEL_ID
    assert observed["tunnel_key"] == TUNNEL_KEY
    assert observed["child_exit_code"] == ExitCode.SUCCESS, observed["child_stderr"]
    assert "Checking local MCP server startup configuration" in result.stderr
    assert "Tunnel client started" in result.stderr
    assert {name: (launcher / name).read_bytes() for name in original} == original
    assert not (launcher / marker.name).exists()


@pytest.mark.parametrize("target_failure", ["denied", "missing_env"])
def test_connect_does_not_start_tunnel_or_fall_back_to_valid_launcher(target_failure: str, tmp_path: Path) -> None:
    launcher = tmp_path / "launcher"
    target = tmp_path / "target"
    launcher.mkdir()
    target.mkdir()
    tunnel_workspace(launcher)
    tunnel_workspace(target, allowed=target_failure != "denied")
    if target_failure == "missing_env":
        (target / ".env").unlink()
    marker = target / "tunnel-observation.json"
    environment = fake_tunnel(tmp_path / "bin", launcher, marker)
    original = {name: (launcher / name).read_bytes() for name in ("config.toml", ".env")}

    result = connect(launcher, str(target), environment)

    assert result.returncode == ExitCode.CONFIG_ERROR, result.stderr
    assert result.stdout == ""
    assert "Tunnel client started" not in result.stderr
    assert TUNNEL_KEY not in result.stderr
    assert not marker.exists()
    assert {name: (launcher / name).read_bytes() for name in original} == original
    if target_failure == "denied":
        assert "Checking local MCP server startup configuration" in result.stderr
        assert "project directory" in result.stderr
    else:
        assert "[.env]" in result.stderr and "file is missing" in result.stderr


@pytest.mark.parametrize("allow_policy_edits", [False, True])
def test_preflight_loads_the_same_explicit_policy_edit_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], allow_policy_edits: bool
) -> None:
    from local_dev_mcp.mcp import server
    from local_dev_mcp.tools.tunnel_client import _preflight

    tunnel_workspace(tmp_path)
    flags = ["--allow-policy-edits"] if allow_policy_edits else []
    monkeypatch.setattr(sys, "argv", ["preflight", "--workspace", str(tmp_path), *flags])
    original = server.load_settings
    seen: list[bool] = []

    def observe(policy: Policy) -> ServerSettings:
        seen.append(policy.allow_policy_edits)
        return original(policy)

    monkeypatch.setattr(server, "load_settings", observe)

    assert _preflight.main() == ExitCode.SUCCESS
    assert seen == [allow_policy_edits]
    assert TUNNEL_KEY not in capsys.readouterr().out
