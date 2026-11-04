"""Shared Git workspace builders and MCP assertions used by fixtures/tests."""

from pathlib import Path
from typing import Protocol

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent
from shellous import sh

from local_dev_mcp.core import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import command_environment
from tests.support.signing import SigningSetup


class WorkspaceFactory(Protocol):
    async def __call__(
        self, *, signing: SigningSetup | None = None, identity: bool = True, sha256: bool = False
    ) -> Path: ...


class WorkspaceConfigFactory(Protocol):
    def __call__(self, commands: str = '["git"]', paths: str = '["."]') -> Path: ...


def configure(root: Path, commands: str = '["git"]', paths: str = '["."]') -> None:
    (root / "config.toml").write_text(f"[filesystem]\nallowed_paths={paths}\n[system]\nallowed_commands={commands}\n")
    (root / ".env").write_text("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n")


async def git(root: Path, *args: str) -> str:
    return (
        await sh("git", "-C", str(root), *args).set(inherit_env=False, env=command_environment(), timeout=10)
    ).removesuffix("\n")


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


async def success(
    session: Client[FastMCPTransport], name: str, arguments: dict[str, object] | None = None
) -> dict[str, object]:
    result = await session.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


async def failure(session: Client[FastMCPTransport], name: str, arguments: dict[str, object] | None = None) -> str:
    result = await session.call_tool(name, arguments, raise_on_error=False)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text
