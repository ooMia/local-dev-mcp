import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .errors import CliError, ExecutionError, ExitCode, LocalIOError
from .settings import Policy

type Handler = Callable[[Policy], int]


@dataclass(frozen=True)
class Command:
    command: str
    help: str
    handler: Handler


class _Arguments(argparse.Namespace):
    handler: Handler


def main() -> int:
    from .diagnostics import report, stderr_diagnostics

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


def _select_handler() -> Handler:
    parser = argparse.ArgumentParser(prog="local-dev-mcp")
    commands = parser.add_subparsers(required=True)
    for c in COMMANDS:
        commands.add_parser(c.command, help=c.help).set_defaults(handler=c.handler)
    args = parser.parse_args(namespace=_Arguments())
    return args.handler


def _run() -> int:
    from pathlib import Path

    handler = _select_handler()

    try:
        directory = Path.cwd()
    except OSError as error:
        raise LocalIOError.locate_project(error) from error
    policy = Policy.load(directory)

    return handler(policy)


def _connect_tunnel(policy: Policy) -> int:
    from .tools.tunnel_client import run

    return run(policy)


def _run_stdio_server(policy: Policy) -> int:
    import os

    from .execution import base_environment

    # Remove ambient credentials and SDK settings before importing the server.
    env = base_environment()
    # FastMCP otherwise reads cwd/.env during import. Application startup owns
    # explicit loading; this library control is not a user configuration.
    env["FASTMCP_ENV_FILE"] = os.devnull
    os.environ.clear()
    os.environ.update(env)

    from .core import create_server

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
