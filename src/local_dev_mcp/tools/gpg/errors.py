"""GPG-specific startup diagnostics, without reading settings or key material."""

from typing import Self

from ...errors import CliError, ConfigurationError, ExitCode, LocalIOError


class GpgConfigurationError(ConfigurationError):
    # Required signing values and key selection.
    @classmethod
    def invalid_signing_key_value(cls) -> Self:
        return cls.environment_value(
            "GIT_SIGNING_KEY",
            "A nonempty signing value without surrounding whitespace or control characters is required.",
            "Check `gpg --homedir <HOME> --with-subkey-fingerprint --list-secret-keys`.",
        )

    @classmethod
    def invalid_signing_fingerprint(cls) -> Self:
        return cls.environment_value(
            "GIT_SIGNING_KEY",
            "Expected a full hexadecimal fingerprint, optionally ending in !.",
            "Check `gpg --homedir <HOME> --with-subkey-fingerprint --list-secret-keys`.",
        )

    # Explicit executable override: blank is allowed; malformed and relative values are not.
    @classmethod
    def invalid_program_value(cls) -> Self:
        return cls.environment_value(
            "GIT_GPG_PROGRAM", "Expected an absolute executable path or an empty value.", "Check `command -v gpg`."
        )

    @classmethod
    def relative_program_path(cls) -> Self:
        return cls.environment_value(
            "GIT_GPG_PROGRAM", "Expected an absolute executable path.", "Check `command -v gpg`."
        )

    # Key-store location: distinguish format, directory type and OS resolution failures.
    @classmethod
    def invalid_home_value(cls) -> Self:
        return cls.environment_value(
            "GIT_GPG_HOME",
            "Expected an absolute directory path or an empty value.",
            "Check `gpgconf --list-dirs homedir`.",
        )

    @classmethod
    def relative_home_path(cls) -> Self:
        return cls.environment_value(
            "GIT_GPG_HOME", "Expected an absolute directory path.", "Check `gpgconf --list-dirs homedir`."
        )

    @classmethod
    def home_not_directory(cls) -> Self:
        return cls.environment_value(
            "GIT_GPG_HOME", "Expected an existing directory.", "Check `gpgconf --list-dirs homedir`."
        )

    @classmethod
    def invalid_home_path(cls) -> Self:
        return cls.environment_value("GIT_GPG_HOME", "Invalid path.", "Check GnuPG home.")


class GpgIOError(LocalIOError):
    @classmethod
    def resolve_gnupg_home(cls, error: OSError) -> Self:
        return cls("Resolve GnuPG home", "GIT_GPG_HOME", error)


class GpgDiscoveryError(CliError):
    # Metadata discovery is part of the logical gpg capability, never arbitrary gpgconf execution.
    @classmethod
    def unavailable(cls) -> Self:
        return cls(
            ExitCode.DEPENDENCY,
            "Discover GnuPG home",
            "gpgconf",
            "No executable gpgconf matching the selected gpg installation was found.",
            "Check the GnuPG installation or set GIT_GPG_HOME explicitly in .env and restart. "
            "No key store was created and no other installation was adopted.",
        )

    @classmethod
    def query_failed(cls) -> Self:
        return cls(
            ExitCode.GENERAL,
            "Discover GnuPG home",
            "gpgconf",
            "The fixed GnuPG metadata query failed, timed out or returned invalid output.",
            "Check `gpgconf --list-components` and `gpgconf --list-dirs homedir` for the selected installation, "
            "or set GIT_GPG_HOME explicitly and restart. Raw process output is not returned.",
        )
