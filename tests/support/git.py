"""Git repository, identity and in-process Git client preparation for tests."""

import subprocess
from pathlib import Path
from typing import Protocol

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from shellous import sh

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import command_environment
from tests.support.signing import SigningSetup


class WorkspaceFactory(Protocol):
    async def __call__(
        self, *, signing: SigningSetup | None = None, identity: bool = True, sha256: bool = False
    ) -> Path: ...


async def git(root: Path, *args: str, timeout: float = 10) -> str:
    return (
        await sh("git", "-C", str(root), *args).set(inherit_env=False, env=command_environment(), timeout=timeout)
    ).removesuffix("\n")


def git_sync(root: Path, *args: str, timeout: float = 10) -> str:
    """Run test-owned Git commands without ambient global/system Git settings."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        env=command_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Test Git {args[0]!r} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def configure_identity(root: Path, name: str = "Test Writer", email: str = "writer@example.invalid") -> None:
    existing = (root / ".env").read_text() if (root / ".env").exists() else ""
    signing = "\n".join(
        line for line in existing.splitlines() if line.startswith(("GIT_COMMIT_SIGN=", "GIT_SIGNING_KEY=", "GIT_GPG_"))
    )
    (root / ".env").write_text(f'GIT_USER_NAME="{name}"\nGIT_USER_EMAIL="{email}"\n{signing}\n')


async def initialize(
    root: Path, *, signing: SigningSetup | None = None, identity: bool = True, sha256: bool = False
) -> None:
    await git(root, "init", "--quiet", *(("--object-format=sha256",) if sha256 else ()))
    with (root / ".git/info/exclude").open("a") as excluded:
        excluded.write("\n.env\n")
    if signing is not None:
        (root / ".env").write_text(signing.dotenv())
    if identity:
        configure_identity(root)
        # Fixture setup also uses Git directly, outside the MCP tool contract.
        await git(root, "config", "user.name", "Test Writer")
        await git(root, "config", "user.email", "writer@example.invalid")


def client(
    root: Path, *, signing: SigningSetup | None = None, commands: tuple[str, ...] | None = None, allowed: bool = True
) -> Client[FastMCPTransport]:
    paths = (root,)
    permitted = commands if commands is not None else (("git", "gpg") if signing is not None else ("git",))
    return Client(create_server(Policy(root, paths if allowed else (), permitted)))
