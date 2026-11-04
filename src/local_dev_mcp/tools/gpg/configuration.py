"""Startup OpenPGP configuration, never a key or agent manager."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from fastmcp.exceptions import ToolError
from shellous import sh

from ...errors import DependencyError, PolicyError
from ...execution import base_environment
from ...settings import Policy
from .. import executable
from ._wrapper import GPG_OPTIONS
from .errors import GpgConfigurationError, GpgDiscoveryError, GpgIOError


@dataclass(frozen=True)
class GpgSigning:
    fingerprint: str = field(repr=False)
    program: Path = field(repr=False)
    home: Path = field(repr=False)
    wrapper: Path = field(repr=False)

    def environment(self) -> dict[str, str]:
        return {"GNUPGHOME": str(self.home), "LOCAL_DEV_MCP_GPG_PROGRAM": str(self.program)}

    async def require_available(self, environment: Mapping[str, str]) -> None:
        """Check key access before hooks; GnuPG may start shared daemons, never prompt."""
        try:
            result = (
                await sh.result(self.program, *GPG_OPTIONS, "--list-secret-keys", self.fingerprint.removesuffix("!"))
                .set(inherit_env=False, env={**environment, **self.environment()}, timeout=10)
                .stdin(sh.DEVNULL)
                .stdout(sh.DEVNULL)
                .stderr(sh.DEVNULL)
            )
        except OSError, TimeoutError, RuntimeError:
            raise ToolError(
                "Signing preparation could not check GnuPG key access. "
                "Check `command -v gpg`, the GnuPG installation and selected home; no commit was attempted. "
                "GnuPG may start shared daemons, but cannot prompt for input. Settings changes require restart."
            ) from None
        if result.cancelled:
            raise asyncio.CancelledError
        if result.exit_code != 0:
            raise ToolError(
                "Signing preparation cannot access the selected secret key or required GnuPG services. "
                "Check `gpg --homedir <HOME> --with-subkey-fingerprint --list-secret-keys`. "
                "No commit was attempted; automatic daemon startup does not unlock a key or permit input. "
                "The cause is not classified as a locked key; settings changes require restart."
            )


def _query(config: Path, *args: str) -> str:
    """Use the synchronous startup seam; the library owns timeout and child reaping."""
    try:
        result = subprocess.run(
            [str(config), *args],
            env=base_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except OSError:
        raise GpgDiscoveryError.unavailable() from None
    except subprocess.TimeoutExpired:
        raise GpgDiscoveryError.query_failed() from None
    if result.returncode != 0:
        raise GpgDiscoveryError.query_failed()
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        raise GpgDiscoveryError.query_failed() from None


def _default_home(program: Path) -> str:
    """Only a matching installation may provide the default home; never search keys."""
    candidates = [program.parent / ("gpgconf.exe" if os.name == "nt" else "gpgconf")]
    discovered = shutil.which("gpgconf")
    if discovered is not None:
        candidates.append(Path(discovered))
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve(strict=True)
            if candidate in seen or not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            seen.add(candidate)
        except OSError, ValueError:
            continue
        matches = False
        for line in _query(candidate, "--list-components").splitlines():
            fields = line.split(":")
            if len(fields) >= 3 and fields[0] == "gpg":
                try:
                    reported = Path(unquote(fields[2], errors="strict"))
                    matches = reported.is_absolute() and reported.resolve(strict=True) == program
                except OSError, ValueError, UnicodeError:
                    matches = False
                break
        if not matches:
            continue
        lines = _query(candidate, "--list-dirs", "homedir").splitlines()
        if len(lines) != 1 or not lines[0] or "\0" in lines[0]:
            raise GpgDiscoveryError.query_failed()
        return lines[0]
    raise GpgDiscoveryError.unavailable()


def _home(value: str) -> Path:
    try:
        home = Path(value)
        if not home.is_absolute():
            raise GpgConfigurationError.relative_home_path()
        home = home.resolve(strict=True)
        if not home.is_dir():
            raise GpgConfigurationError.home_not_directory()
        return home
    except OSError as error:
        raise GpgIOError.resolve_gnupg_home(error) from None
    except ValueError:
        raise GpgConfigurationError.invalid_home_path() from None


def _program(values: Mapping[str, str | None]) -> Path:
    override = values.get("GIT_GPG_PROGRAM", "")
    if override is None or override != override.strip() or any(c in override for c in "\0\r\n"):
        raise GpgConfigurationError.invalid_program_value()
    if not override:
        return executable("gpg")
    try:
        program = Path(override)
        if not program.is_absolute():
            raise GpgConfigurationError.relative_program_path()
        program = program.resolve(strict=True)
        if not program.is_file() or not os.access(program, os.X_OK):
            raise DependencyError.unavailable("gpg")
        return program
    except OSError, ValueError:
        raise DependencyError.unavailable("gpg") from None


def signing_configuration(values: Mapping[str, str | None], policy: Policy) -> GpgSigning:
    try:
        policy.require_command("gpg")
    except PermissionError:
        raise PolicyError.command_denied("gpg") from None
    fingerprint = values.get("GIT_SIGNING_KEY")
    if (
        fingerprint is None
        or not fingerprint.strip()
        or fingerprint != fingerprint.strip()
        or any(c in fingerprint for c in "\0\r\n")
    ):
        raise GpgConfigurationError.invalid_signing_key_value()
    # Full v4/v5+ fingerprints only; preserve an explicitly supplied exact-key !.
    if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})!?", fingerprint) is None:
        raise GpgConfigurationError.invalid_signing_fingerprint()
    override = values.get("GIT_GPG_HOME", "")
    if override is None or override != override.strip() or any(c in override for c in "\0\r\n"):
        raise GpgConfigurationError.invalid_home_value()
    # Validate explicit homes without requiring discovery or visiting a default home.
    home = _home(override) if override else None
    program = _program(values)
    if home is None:
        home = _home(_default_home(program))
    wrapper = Path(sys.executable).parent / ("local-dev-mcp-gpg.exe" if os.name == "nt" else "local-dev-mcp-gpg")
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise DependencyError.signing_entry_point()
    return GpgSigning(fingerprint, program, home, wrapper)
