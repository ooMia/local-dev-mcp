"""Bounded read-only Git execution for the developer release CLI."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from shellous import sh

from ...execution import base_environment
from ._models import COMMIT_ID


@dataclass(frozen=True)
class LocalGit:
    root: Path

    async def read(self, *args: str) -> bytes:
        command = (
            sh.result("git", "--literal-pathspecs", "-C", str(self.root), *args)
            .set(
                inherit_env=False,
                env={
                    **base_environment(),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                },
                timeout=10,
            )
            .stdin(sh.DEVNULL)
            .stdout(sh.CAPTURE)
            .stderr(sh.DEVNULL)
        )
        output = bytearray()
        exceeded = False
        try:
            async with command as process:
                if process.stdout is None:
                    raise ValueError("Release Git output is unavailable.")
                while chunk := await process.stdout.read(65536):
                    if len(output) + len(chunk) > 1024 * 1024:
                        exceeded = True
                    if not exceeded:
                        output.extend(chunk)
            if process.cancelled:
                raise asyncio.CancelledError
            if process.returncode != 0 or exceeded:
                raise ValueError("Release Git query failed or exceeded the 1 MiB output limit.")
        except OSError, RuntimeError, TimeoutError:
            raise ValueError("Release Git query could not complete.") from None
        return bytes(output)

    async def head(self) -> str:
        root = (await self.read("rev-parse", "--show-toplevel")).decode().strip()
        if Path(root).resolve() != self.root.resolve():
            raise ValueError("The selected release workspace must itself be the Git root.")
        head = (await self.read("rev-parse", "--verify", "HEAD^{commit}")).decode().strip()
        if COMMIT_ID.fullmatch(head) is None:
            raise ValueError("A complete observed HEAD SHA is required.")
        return head
