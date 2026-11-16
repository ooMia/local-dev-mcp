"""Provide shared workspace resources and validate declared test layers."""

import asyncio
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from shellous import sh

from local_dev_mcp.execution import base_environment
from tests.support.git import WorkspaceFactory, initialize
from tests.support.packaging import installed_package as installed_package
from tests.support.signing import SigningSetup
from tests.support.workspace import WorkspaceConfigFactory, configure


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        layers = [name for name in ("unit", "integration", "e2e") if item.get_closest_marker(name) is not None]
        if len(layers) != 1:
            raise pytest.UsageError(f"Declare exactly one test layer for {item.nodeid}: {layers}")
        if item.get_closest_marker("dag") is not None and layers[0] == "unit":
            raise pytest.UsageError(f"Unit tests cannot declare test-result dependencies: {item.nodeid}")


@pytest.fixture
def hostile_git_global(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Expose ambient Git signing that disposable test repositories must ignore."""
    config = tmp_path_factory.mktemp("hostile-git-global") / "config"
    config.write_text(
        "[commit]\n\tgpgSign = true\n"
        "[gpg]\n\tprogram = nonexistent-gpg-signer-for-test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config


@pytest.fixture
def workspace_config(tmp_path: Path) -> WorkspaceConfigFactory:
    def create(commands: str = "[]", paths: str = '["."]') -> Path:
        configure(tmp_path, commands, paths)
        return tmp_path

    return create


@pytest.fixture
def workspace_factory(tmp_path: Path) -> WorkspaceFactory:
    async def create(*, signing: SigningSetup | None = None, identity: bool = True, sha256: bool = False) -> Path:
        await initialize(tmp_path, signing=signing, identity=identity, sha256=sha256)
        return tmp_path

    return create


@pytest.fixture(scope="session")
def gpg_home_factory() -> Iterator[Callable[[], Path]]:
    """Each worker owns its GnuPG homes/services; consumers finish before cleanup."""
    config = shutil.which("gpgconf")
    if config is None:
        pytest.fail("Real signing tests require installed gpgconf.")

    async def stop(home: Path) -> None:
        await (
            sh(config, "--kill", "all")
            .set(inherit_env=False, env={**base_environment(), "GNUPGHOME": str(home)}, timeout=10)
            .stderr(sh.DEVNULL)
        )

    with ExitStack() as resources:

        def create() -> Path:
            # Short paths avoid the Unix socket path limit even in deep checkouts.
            directory = resources.enter_context(
                TemporaryDirectory(prefix="ldm-gpg-", dir="/tmp" if os.name == "posix" else None)
            )
            home = Path(directory).resolve()
            home.chmod(0o700)
            resources.callback(lambda: asyncio.run(stop(home)))
            return home

        yield create


@pytest.fixture(scope="session")
def signing_material(gpg_home_factory: Callable[[], Path]) -> SigningSetup:
    program = shutil.which("gpg")
    if program is None:
        pytest.fail("Real signing tests require installed gpg.")
    home = gpg_home_factory()
    env = {**base_environment(), "GNUPGHOME": str(home)}

    async def setup() -> SigningSetup:
        await (
            sh(
                program,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-generate-key",
                "MCP Signing Test <signing@example.invalid>",
                "ed25519",
                "sign",
                "0",
            )
            .set(inherit_env=False, env=env, timeout=30)
            .stderr(sh.DEVNULL)
        )
        listing = (
            await sh(program, "--batch", "--with-colons", "--list-secret-keys")
            .set(inherit_env=False, env=env, timeout=10)
            .stderr(sh.DEVNULL)
        )
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        return SigningSetup(Path(program), home, fingerprint)

    return asyncio.run(setup())


@pytest.fixture
def signing_setup(signing_material: SigningSetup) -> SigningSetup:
    return signing_material


@pytest.fixture(scope="session")
def subkey_setup(signing_material: SigningSetup) -> tuple[SigningSetup, str]:
    """A cert-only primary and signing subkey in the test-owned agent/home."""
    env = {**base_environment(), "GNUPGHOME": str(signing_material.home)}

    async def setup() -> tuple[SigningSetup, str]:
        await (
            sh(
                signing_material.program,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-generate-key",
                "MCP Subkey Test <subkey@example.invalid>",
                "ed25519",
                "cert",
                "0",
            )
            .set(inherit_env=False, env=env, timeout=30)
            .stderr(sh.DEVNULL)
        )
        listing = (
            await sh(
                signing_material.program, "--batch", "--with-colons", "--list-secret-keys", "subkey@example.invalid"
            )
            .set(inherit_env=False, env=env, timeout=10)
            .stderr(sh.DEVNULL)
        )
        primary = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        await (
            sh(
                signing_material.program,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-add-key",
                primary,
                "ed25519",
                "sign",
                "0",
            )
            .set(inherit_env=False, env=env, timeout=30)
            .stderr(sh.DEVNULL)
        )
        listing = (
            await sh(signing_material.program, "--batch", "--with-colons", "--list-secret-keys", primary)
            .set(inherit_env=False, env=env, timeout=10)
            .stderr(sh.DEVNULL)
        )
        fingerprints = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
        return SigningSetup(signing_material.program, signing_material.home, primary), fingerprints[1]

    return asyncio.run(setup())
