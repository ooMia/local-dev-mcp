"""Noninteractive Secure MCP Tunnel launch using shellous process management."""

import asyncio
import errno
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from shellous import Runner, sh

from ...auth_environment import account_locations
from ...diagnostics import status
from ...errors import ConfigurationError, ExitCode, LocalIOError, PolicyError, TunnelError
from ...execution import base_environment
from ...settings import Policy, read_dotenv
from .. import executable


def tunnel_environment(root: Path) -> dict[str, str]:
    values = read_dotenv(root)
    required = ("CONTROL_PLANE_TUNNEL_ID", "CONTROL_PLANE_API_KEY")
    selected: dict[str, str] = {}
    missing: list[str] = []
    for name in required:
        value = values.get(name)
        if value is None or not value.strip():
            missing.append(name)
        else:
            selected[name] = value
    if missing:
        raise ConfigurationError.missing_environment_values(missing)

    if not re.fullmatch(r"tunnel_[a-z0-9]{32}", selected["CONTROL_PLANE_TUNNEL_ID"]):
        # The external client includes invalid IDs in error messages. Validate
        # locally so a misplaced credential is never passed as a tunnel ID.
        raise ConfigurationError.invalid_tunnel_id()
    # The Tunnel daemon launches the MCP child. Keep host credential locations
    # and an explicitly exported GH_TOKEN; do not inherit arbitrary environment
    # values or GITHUB_TOKEN. A protected workspace .env GH_TOKEN takes priority.
    inherited = account_locations()
    token = values.get("GH_TOKEN") or os.environ.get("GH_TOKEN")
    if token is not None and token.strip():
        if token != token.strip() or len(token) > 4096 or any(ord(char) < 33 for char in token):
            raise ConfigurationError.environment_value(
                "GH_TOKEN",
                "Invalid optional GitHub token format.",
                "Use a single-line nonempty token without surrounding whitespace.",
            )
        selected["GH_TOKEN"] = token
    return {**base_environment(), **inherited, **selected}


@dataclass
class _TunnelRun:
    # Observe the library's process handle; lifecycle management stays in shellous.
    process: Runner | None = None


async def connect(root: Path, state: _TunnelRun, *, allow_policy_edits: bool = False) -> int:
    env = tunnel_environment(root)
    binary = executable("tunnel-client")
    from ._preflight import check_server

    status("Checking local MCP server startup configuration before connecting.")
    await check_server(root, allow_policy_edits=allow_policy_edits)
    flags = ["--allow-policy-edits"] if allow_policy_edits else []
    command = shlex.join([sys.executable, "-I", "-m", "local_dev_mcp", "--workspace", str(root), *flags, "run"])
    command = (
        sh.result(
            binary,
            "run",
            "--mcp.command",
            "command=" + command,
            "--mcp.stdio-send-initialized-notification",
            "--health.listen-addr",
            "127.0.0.1:0",
        )
        .set(cwd=root, inherit_env=False, env=env)
        .stdin(sh.DEVNULL)
        .stdout(sh.DEVNULL)
        .stderr(sh.DEVNULL)
    )
    process = None
    try:
        async with command as process:
            state.process = process
            status("Tunnel client started; remote readiness and Chat connectivity have not been confirmed.")
    except asyncio.CancelledError as error:
        if process is None or process.returncode is None:
            raise TunnelError.cleanup_unconfirmed() from error
        raise TunnelError.interrupted(process.returncode) from error
    except OSError as error:
        if process is not None and process.returncode is None:
            raise TunnelError.cleanup_unconfirmed() from error
        if (
            process is None
            and error.filename != str(root)
            and (isinstance(error, (FileNotFoundError, PermissionError)) or error.errno == errno.ENOEXEC)
        ):
            raise TunnelError.dependency_unavailable() from error
        raise LocalIOError.run_tunnel(error) from error
    except Exception as error:
        if process is not None and process.returncode is None:
            raise TunnelError.cleanup_unconfirmed() from error
        raise

    if process.returncode is None:
        raise TunnelError.cleanup_unconfirmed()
    if process.cancelled:
        raise TunnelError.interrupted(process.returncode)
    if process.returncode != 0:
        raise TunnelError.unsuccessful_exit(process.returncode)
    status("Tunnel client exited with code 0; this does not confirm a working Chat connection.")
    return ExitCode.SUCCESS


def run(policy: Policy) -> int:
    try:
        policy.require_command("tunnel-client")
    except PermissionError as error:
        raise PolicyError.command_denied("tunnel-client") from error
    root = policy.root
    # shellous may log raw exceptions during cancellation. CLI diagnostics are
    # the output contract; keep those implementation logs off the console too.
    logger = logging.getLogger("shellous")
    was_disabled = logger.disabled
    logger.disabled = True
    state = _TunnelRun()
    try:
        return asyncio.run(connect(root, state, allow_policy_edits=policy.allow_policy_edits))
    except KeyboardInterrupt as error:
        # A repeated SIGINT can bypass the coroutine's cancellation handler.
        # Never let the generic CLI handler infer that cleanup succeeded.
        if state.process is None or state.process.returncode is None:
            raise TunnelError.cleanup_unconfirmed() from error
        raise TunnelError.interrupted(state.process.returncode) from error
    finally:
        logger.disabled = was_disabled
