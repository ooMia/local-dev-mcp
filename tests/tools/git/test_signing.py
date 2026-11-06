"""Signed commit guarantees through MCP, with disposable GnuPG resources."""

import asyncio
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from fastmcp import Client
from shellous import sh

from local_dev_mcp.execution import base_environment
from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.git import WorkspaceFactory, client, git
from tests.support.mcp import failure, success
from tests.support.signing import SigningSetup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("selector", ["primary", "subkey-exact", "primary-exact"])
def test_primary_and_exact_subkey_selection(
    tmp_path: Path, subkey_setup: tuple[SigningSetup, str], selector: str, workspace_factory: WorkspaceFactory
) -> None:
    material, subkey = subkey_setup
    fingerprint = {
        "primary": material.fingerprint,
        "subkey-exact": subkey + "!",
        "primary-exact": material.fingerprint + "!",
    }[selector]
    selected = SigningSetup(material.program, material.home, fingerprint)

    async def scenario() -> None:
        await workspace_factory(signing=selected)
        (tmp_path / "data").write_text("subkey signature")
        async with client(tmp_path, signing=selected) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            if selector == "primary-exact":
                await failure(session, "commit_project_changes", {"message": "cannot sign with cert-only primary"})
                assert (await success(session, "get_project_status"))["head"] is None
            else:
                result = await success(session, "commit_project_changes", {"message": "subkey"})
                assert result["signature_verified"] is True

    asyncio.run(scenario())


def test_valid_signature_from_other_key_is_not_verified_as_configured_key(
    tmp_path: Path,
    signing_setup: SigningSetup,
    subkey_setup: tuple[SigningSetup, str],
    workspace_factory: WorkspaceFactory,
) -> None:
    other, _ = subkey_setup

    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        signer = tmp_path / ".git/other-key-signer"
        signer.write_text(
            f"#!{sys.executable}\nimport os,sys\nargs=sys.argv[1:]\n"
            "if '-bsau' in args:\n"
            f"    args[args.index('-bsau')+1]={other.fingerprint!r}\n"
            f"os.execv({str(signing_setup.program)!r}, [{str(signing_setup.program)!r}, *args])\n"
        )
        signer.chmod(0o700)
        with (tmp_path / ".env").open("a") as stream:
            stream.write(f"GIT_GPG_PROGRAM='{signer}'\n")
        (tmp_path / "data").write_text("other valid key")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "other key"})
            assert result["success"] is True
            assert result["signature_verified"] is False
            assert result["signature_error"]
        # Establish that the refusal was about key identity, not a corrupt signature.
        await (
            sh("git", "-C", tmp_path, "-c", f"gpg.program={signing_setup.program}", "verify-commit", "HEAD")
            .set(inherit_env=False, env={**base_environment(), "GNUPGHOME": str(signing_setup.home)}, timeout=10)
            .stderr(sh.DEVNULL)
        )

    asyncio.run(scenario())


def test_signing_snapshot_survives_hook_changes_until_restart(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        hook = tmp_path / ".git/hooks/pre-commit"
        hook.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            "with Path('.env').open('a') as stream:\n"
            "    stream.write('GIT_SIGNING_KEY=invalid-after-hook\\n"
            "GIT_GPG_PROGRAM=/missing-after-hook\\nGIT_GPG_HOME=/missing-home-after-hook\\n')\n"
        )
        hook.chmod(0o700)
        file = tmp_path / "data"
        file.write_text("first")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "snapshot"})
            assert result["signature_verified"] is True
            file.write_text("second")
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "same snapshot"})
            assert result["signature_verified"] is True
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert "GIT_SIGNING_KEY" in error
            assert "invalid-after-hook" not in error

    asyncio.run(scenario())


def test_enabled_gpg_permission_is_checked_at_startup(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        async with client(tmp_path, signing=signing_setup, commands=("git",)) as session:
            assert "gpg is not allowed" in await failure(session, "commit_project_changes", {"message": "blocked"})
            await success(session, "get_project_status")

    asyncio.run(scenario())


def test_verification_failure_does_not_hide_successful_commit(
    tmp_path: Path, signing_setup: SigningSetup, caplog: pytest.LogCaptureFixture, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        signer = tmp_path / ".git/test-signer"
        signer.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            "if '--verify' in sys.argv:\n"
            "    print('test-secret-signing-output', file=sys.stderr)\n"
            "    raise SystemExit(1)\n"
            f"os.execv({str(signing_setup.program)!r}, [{str(signing_setup.program)!r}, *sys.argv[1:]])\n"
        )
        signer.chmod(0o700)
        with (tmp_path / ".env").open("a") as stream:
            stream.write(f"GIT_GPG_PROGRAM='{signer}'\n")
        (tmp_path / "data").write_text("signed but verification unavailable")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "created"})
            assert result["success"] is True
            assert result["signature_verified"] is False
            assert result["signature_error"]
            assert (await success(session, "get_project_head"))["head"] == result["head_after"]
            assert "test-secret-signing-output" not in str(result)
        assert "test-secret-signing-output" not in caplog.text
        assert await git(tmp_path, "show", "--format=%s", "--no-patch") == "created"

    asyncio.run(scenario())


def test_lost_signer_is_dynamic_failure_without_path_fallback(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        signer = tmp_path / ".git/selected-signer"
        signer.write_text(
            f"#!{sys.executable}\nimport os,sys\nos.execv({str(signing_setup.program)!r}, "
            f"[{str(signing_setup.program)!r}, *sys.argv[1:]])\n"
        )
        signer.chmod(0o700)
        with (tmp_path / ".env").open("a") as stream:
            stream.write(f"GIT_GPG_PROGRAM='{signer}'\n")
        server_client = client(tmp_path, signing=signing_setup)
        signer.unlink()
        (tmp_path / "data").write_text("keep staged")
        async with server_client as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            error = await failure(session, "commit_project_changes", {"message": "missing selected signer"})
            assert "Signing preparation" in error
            assert "command -v gpg" in error
            assert (await success(session, "get_project_status"))["head"] is None
            assert "+keep staged" in str((await success(session, "get_project_diff", {"staged": True}))["patch"])

    asyncio.run(scenario())


def test_locked_key_fails_without_pinentry_or_unsigned_fallback(
    tmp_path: Path,
    signing_setup: SigningSetup,
    workspace_factory: WorkspaceFactory,
    gpg_home_factory: Callable[[], Path],
) -> None:
    config = shutil.which("gpgconf")
    assert config is not None
    home = gpg_home_factory()
    env = {**base_environment(), "GNUPGHOME": str(home)}
    marker = home / "pinentry-ran"
    pinentry = home / "test-pinentry"
    pinentry.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    pinentry.chmod(0o700)
    (home / "gpg-agent.conf").write_text(f"pinentry-program {pinentry}\n")

    async def scenario() -> None:
        await (
            sh(
                signing_setup.program,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "test-only-passphrase",
                "--quick-generate-key",
                "Locked Test <locked@example.invalid>",
                "ed25519",
                "sign",
                "0",
            )
            .set(inherit_env=False, env=env, timeout=30)
            .stderr(sh.DEVNULL)
        )
        listing = (
            await sh(signing_setup.program, "--batch", "--with-colons", "--list-secret-keys")
            .set(inherit_env=False, env=env, timeout=10)
            .stderr(sh.DEVNULL)
        )
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        await sh(config, "--kill", "gpg-agent").set(inherit_env=False, env=env, timeout=10)
        await sh(config, "--launch", "gpg-agent").set(inherit_env=False, env=env, timeout=10)
        locked = SigningSetup(signing_setup.program, home, fingerprint)
        await workspace_factory(signing=locked)
        (tmp_path / "data").write_text("keep staged")
        async with client(tmp_path, signing=locked) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            error = await failure(session, "commit_project_changes", {"message": "locked"})
            assert "failed" in error
            assert (await success(session, "get_project_status"))["head"] is None
            assert "+keep staged" in str((await success(session, "get_project_diff", {"staged": True}))["patch"])
        assert not marker.exists()

    asyncio.run(scenario())


def test_host_signing_does_not_expand_workspace_file_access(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        assert not signing_setup.home.is_relative_to(tmp_path)
        (tmp_path / "data").write_text("host signing with workspace-only paths")
        server = create_server(Policy(tmp_path, (tmp_path,), ("git", "gpg")))
        async with Client(server) as session:
            # The signing home is still not a valid Git working path.
            await failure(session, "stage_project_paths", {"paths": [str(signing_setup.home)]})
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "host signing"})
            assert result["signature_verified"] is True

    asyncio.run(scenario())


def test_signing_settings_change_only_after_restart(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        file = tmp_path / "data"
        file.write_text("first")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            first = await success(session, "commit_project_changes", {"message": "first"})
            assert first["signature_verified"] is True
            with (tmp_path / ".env").open("a") as stream:
                stream.write("GIT_COMMIT_SIGN=false\nGIT_SIGNING_KEY=invalid-test-key-value\n")
            file.write_text("second")
            await success(session, "stage_project_paths", {"paths": ["data"]})
            second = await success(session, "commit_project_changes", {"message": "still signed"})
            assert second["signature_verified"] is True
        file.write_text("third")
        async with client(tmp_path, commands=("git",)) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            third = await success(session, "commit_project_changes", {"message": "unsigned after restart"})
            assert third["signing_requested"] is False
            assert third["signature_verified"] is None

    asyncio.run(scenario())


def test_missing_gpg_on_path_fails_before_commit(
    tmp_path: Path, signing_setup: SigningSetup, monkeypatch: pytest.MonkeyPatch, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(line for line in env_file.read_text().splitlines() if not line.startswith("GIT_GPG_PROGRAM="))
            + "\n"
        )
        git_program = shutil.which("git")
        assert git_program is not None
        binaries = tmp_path / ".git/bin"
        binaries.mkdir()
        (binaries / "git").symlink_to(git_program)
        monkeypatch.setenv("PATH", str(binaries))
        (tmp_path / "data").write_text("keep staged")
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert "command -v gpg" in error

    asyncio.run(scenario())


def test_signing_starts_required_shared_daemons_without_manual_launch(
    tmp_path: Path, gpg_home_factory: Callable[[], Path], workspace_factory: WorkspaceFactory
) -> None:
    program, config = shutil.which("gpg"), shutil.which("gpgconf")
    assert program is not None and config is not None
    home = gpg_home_factory()
    (home / "common.conf").write_text("use-keyboxd\n")
    env = {**base_environment(), "GNUPGHOME": str(home)}

    async def scenario() -> None:
        await (
            sh(
                program,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-generate-key",
                "MCP Autostart Test <autostart@example.invalid>",
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
        await sh(config, "--kill", "all").set(inherit_env=False, env=env, timeout=10).stderr(sh.DEVNULL)
        # Both services are stopped; only the MCP operation may start them again.
        absent = (
            await sh.result(program, "--no-autostart", "--list-secret-keys")
            .set(inherit_env=False, env=env, timeout=10)
            .stdout(sh.DEVNULL)
            .stderr(sh.DEVNULL)
        )
        assert absent.exit_code != 0
        material = SigningSetup(Path(program), home, fingerprint)
        await workspace_factory(signing=material)
        (tmp_path / "data").write_text("automatically prepared signing")
        async with client(tmp_path, signing=material) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "autostart"})
            assert result["signature_verified"] is True
        # Server disconnect must not kill the shared GnuPG services.
        available = (
            await sh.result(program, "--no-autostart", "--list-secret-keys")
            .set(inherit_env=False, env=env, timeout=10)
            .stdout(sh.DEVNULL)
            .stderr(sh.DEVNULL)
        )
        assert available.exit_code == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("selection", ["omitted", "empty"])
def test_discovered_home_supports_mcp_signing_and_stays_fixed(
    tmp_path: Path,
    signing_setup: SigningSetup,
    workspace_factory: WorkspaceFactory,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    import subprocess

    from local_dev_mcp.tools.gpg import configuration

    original_run = subprocess.run
    homes: list[Path] = []

    def query(
        args: list[str], *, env: dict[str, str], stdin: int, stdout: int, stderr: int, timeout: int, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        if args[1:] == ["--list-dirs", "homedir"]:
            homes.append(Path(args[0]))
            home = signing_setup.home if len(homes) == 1 else tmp_path / "must-not-reload"
            return subprocess.CompletedProcess(args, 0, str(home).encode())
        return original_run(args, env=env, stdin=stdin, stdout=stdout, stderr=stderr, timeout=timeout, check=check)

    # Intercept only default-home metadata; installation matching and all crypto are real.
    monkeypatch.setattr(configuration.subprocess, "run", query)

    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(line for line in env_file.read_text().splitlines() if not line.startswith("GIT_GPG_HOME=")) + "\n"
        )
        if selection == "empty":
            with env_file.open("a") as stream:
                stream.write("GIT_GPG_HOME=\n")
        server_client = client(tmp_path, signing=signing_setup)
        (tmp_path / "data").write_text("discovered home")
        async with server_client as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            committed = await success(session, "commit_project_changes", {"message": "discovery"})
            assert committed["signature_verified"] is True
        assert len(homes) == 1

    asyncio.run(scenario())
