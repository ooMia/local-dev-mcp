"""Validate MCP startup settings in an isolated interpreter, without launching a tunnel."""

import asyncio
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from shellous import sh

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


async def check_server(root: Path) -> None:
    """Transport only our safe CLI diagnostics; discard third-party stderr."""
    try:
        result = (
            await sh.result(sys.executable, "-I", "-m", __name__)
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
    from ...core import load_settings
    from ...settings import Policy

    try:
        settings = load_settings(Policy.load(Path.cwd()))
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
