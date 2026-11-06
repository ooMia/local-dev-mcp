"""Fixed uv version operation used by the explicit developer release command."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from shellous import sh

from ...execution import base_environment
from .. import executable


async def update_version(root: Path, version: str) -> None:
    with TemporaryDirectory(prefix="ldm-release-uv-") as cache:
        program = executable("uv")
        environment = {**base_environment(), "UV_CACHE_DIR": cache, "UV_PYTHON_DOWNLOADS": "never"}
        # --no-config does not disable parent workspace discovery. Verify uv's
        # actual scope before it can modify a lock outside the selected root.
        query = (
            sh.result(program, "workspace", "dir", "--project", str(root), "--no-config", "--offline")
            .set(cwd=root, inherit_env=False, env=environment, timeout=10)
            .stdin(sh.DEVNULL)
            .stdout(sh.CAPTURE)
            .stderr(sh.DEVNULL)
        )
        try:
            async with query as process:
                if process.stdout is None:
                    raise ValueError("uv workspace scope could not be observed; no version write was attempted.")
                scope = await process.stdout.read(16385)
            if process.cancelled:
                raise asyncio.CancelledError
            if process.returncode != 0 or len(scope) > 16384:
                raise ValueError("uv workspace scope could not be confirmed; no version write was attempted.")
            if Path(scope.decode().strip()).resolve() != root.resolve():
                raise ValueError(
                    "The selected Git root belongs to a different uv workspace; no version write was attempted."
                )
        except OSError, RuntimeError, TimeoutError, UnicodeError:
            raise ValueError("uv workspace preflight failed; no version write was attempted.") from None
        command = (
            sh.result(
                program,
                "version",
                version,
                "--project",
                str(root),
                "--no-sync",
                "--no-config",
                "--no-progress",
            )
            .set(
                cwd=root,
                inherit_env=False,
                env=environment,
                timeout=60,
            )
            .stdin(sh.DEVNULL)
            .stdout(sh.DEVNULL)
            .stderr(sh.DEVNULL)
        )
        try:
            async with command as process:
                pass
            if process.cancelled:
                raise asyncio.CancelledError
            if process.returncode != 0:
                raise ValueError(
                    "uv version failed; files may have changed. Inspect status and version before retrying."
                )
        except OSError, RuntimeError, TimeoutError:
            raise ValueError("uv version could not finish; inspect status, version and lock before retrying.") from None


__all__ = ["update_version"]
