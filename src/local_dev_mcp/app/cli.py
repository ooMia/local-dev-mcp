import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..errors import CliError, ExecutionError, ExitCode, LocalIOError
from ..settings import Policy
from .workspace import add_policy_edit_argument, add_workspace_argument, load_workspace_policy

type Handler = Callable[[Policy], int]


@dataclass(frozen=True)
class Command:
    command: str
    help: str
    handler: Handler


class _Arguments(argparse.Namespace):
    handler: Handler
    workspace: Path | None
    allow_policy_edits: bool


def main() -> int:
    from ..diagnostics import report, stderr_diagnostics

    with stderr_diagnostics():
        try:
            return _run()
        except CliError as error:
            report(error)
            return error.code
        except KeyboardInterrupt:
            report(ExecutionError.interrupted())
            return ExitCode.INTERRUPTED
        except Exception as error:
            report(ExecutionError.unexpected(error))
            return ExitCode.GENERAL


def _parse_arguments() -> _Arguments:
    parser = argparse.ArgumentParser(prog="local-dev-mcp")
    add_workspace_argument(parser)
    add_policy_edit_argument(parser)
    commands = parser.add_subparsers(required=True)
    for c in COMMANDS:
        commands.add_parser(c.command, help=c.help).set_defaults(handler=c.handler)
    return parser.parse_args(namespace=_Arguments())


def _run() -> int:
    args = _parse_arguments()
    policy = load_workspace_policy(args.workspace, allow_policy_edits=args.allow_policy_edits)
    return args.handler(policy)


def _connect_tunnel(policy: Policy) -> int:
    from ..tools.tunnel_client import run

    return run(policy)


def _run_stdio_server(policy: Policy) -> int:
    import os

    from ..execution import base_environment

    # Remove ambient credentials and SDK settings before importing the server.
    env = base_environment()
    # FastMCP otherwise reads cwd/.env during import. Application startup owns
    # explicit loading; this library control is not a user configuration.
    env["FASTMCP_ENV_FILE"] = os.devnull
    os.environ.clear()
    os.environ.update(env)

    from ..mcp.server import create_server

    try:
        create_server(policy).run(transport="stdio", show_banner=False, log_level="WARNING")
    except OSError as error:
        raise LocalIOError.run_stdio_server(error) from error
    return ExitCode.SUCCESS


RUN: Final = Command(
    command="run",
    help="Run the stdio MCP server",
    handler=_run_stdio_server,
)
CONNECT: Final = Command(
    command="connect",
    help="Connect the MCP server through Secure MCP Tunnel",
    handler=_connect_tunnel,
)
COMMANDS: Final[tuple[Command, ...]] = (RUN, CONNECT)
