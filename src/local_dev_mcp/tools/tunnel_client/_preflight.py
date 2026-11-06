"""Validate MCP startup settings in an isolated interpreter, without launching a tunnel."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from shellous import sh

from ...app import add_policy_edit_argument, add_workspace_argument, load_workspace_policy
from ...diagnostics import warn_preparation
from ...errors import CliError, ExitCode
from ...execution import base_environment
from ...readiness import PreparationIssue
from .errors import TunnelStartupError


class _Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: ExitCode
    operation: str
    target: str
    reason: str
    action: str


class _Prepared(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    warnings: tuple[_Diagnostic, ...]


class _Arguments(argparse.Namespace):
    workspace: Path | None
    allow_policy_edits: bool


async def check_server(root: Path, *, allow_policy_edits: bool = False) -> None:
    """Transport only our safe CLI diagnostics; discard third-party stderr."""
    try:
        flags = ("--allow-policy-edits",) if allow_policy_edits else ()
        result = (
            await sh.result(sys.executable, "-I", "-m", __name__, "--workspace", str(root), *flags)
            .set(
                cwd=root,
                inherit_env=False,
                env={**base_environment(), "FASTMCP_ENV_FILE": os.devnull},
                timeout=10,
            )
            .stdin(sh.DEVNULL)
            .stderr(sh.DEVNULL)
        )
    except OSError, TimeoutError, RuntimeError:
        raise TunnelStartupError.preflight_unconfirmed() from None
    if result.cancelled:
        raise asyncio.CancelledError
    if result.exit_code == 0:
        try:
            prepared = _Prepared.model_validate_json(result.output)
        except ValidationError:
            raise TunnelStartupError.preflight_unconfirmed() from None
        if any(
            item.code not in (ExitCode.GENERAL, ExitCode.CONFIG_ERROR, ExitCode.IO_ERROR, ExitCode.DEPENDENCY)
            for item in prepared.warnings
        ):
            raise TunnelStartupError.preflight_unconfirmed()
        warn_preparation(
            tuple(
                PreparationIssue(item.code, item.operation, item.target, item.reason, item.action)
                for item in prepared.warnings
            )
        )
        return
    try:
        diagnostic = _Diagnostic.model_validate_json(result.output)
    except ValidationError:
        raise TunnelStartupError.preflight_unconfirmed() from None
    if diagnostic.code != result.exit_code or diagnostic.code not in (
        ExitCode.GENERAL,
        ExitCode.CONFIG_ERROR,
        ExitCode.IO_ERROR,
        ExitCode.DEPENDENCY,
    ):
        raise TunnelStartupError.preflight_unconfirmed()
    raise CliError(diagnostic.code, diagnostic.operation, diagnostic.target, diagnostic.reason, diagnostic.action)


def main() -> int:
    # Import only after the parent has supplied the same FastMCP isolation as run.
    from ...mcp.server import load_settings

    parser = argparse.ArgumentParser(prog="local-dev-mcp preflight")
    add_workspace_argument(parser)
    add_policy_edit_argument(parser)
    args = parser.parse_args(namespace=_Arguments())

    try:
        settings = load_settings(load_workspace_policy(args.workspace, allow_policy_edits=args.allow_policy_edits))
    except CliError as error:
        diagnostic = _Diagnostic(
            code=error.code, operation=error.operation, target=error.target, reason=error.reason, action=error.action
        )
        print(diagnostic.model_dump_json())
        return error.code
    except Exception:
        # Never serialize an arbitrary exception or its traceback.
        return ExitCode.GENERAL
    issues = dict.fromkeys(state.issue for state in settings.readiness if state.issue is not None)
    prepared = _Prepared(
        warnings=tuple(
            _Diagnostic(
                code=issue.code,
                operation=issue.operation,
                target=issue.target,
                reason=issue.reason,
                action=issue.action,
            )
            for issue in issues
        )
    )
    print(prepared.model_dump_json())
    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
