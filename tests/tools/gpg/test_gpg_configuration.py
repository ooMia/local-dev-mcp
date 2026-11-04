"""Validate signing overrides and installation metadata without using user keys."""

import errno
import os
from pathlib import Path
from typing import Never

import pytest

from local_dev_mcp.errors import ExitCode, LocalIOError, PolicyError
from local_dev_mcp.settings import Policy, read_dotenv
from local_dev_mcp.tools.gpg.configuration import signing_configuration
from local_dev_mcp.tools.gpg.errors import GpgConfigurationError, GpgDiscoveryError, GpgIOError


@pytest.mark.parametrize(
    ("override", "field", "reason"),
    [
        ({"GIT_SIGNING_KEY": None}, "GIT_SIGNING_KEY", "A nonempty signing value"),
        ({"GIT_SIGNING_KEY": "private-canary"}, "GIT_SIGNING_KEY", "Expected a full hexadecimal fingerprint"),
        ({"GIT_GPG_HOME": None}, "GIT_GPG_HOME", "Expected an absolute directory path or an empty value"),
        ({"GIT_GPG_HOME": "relative-private-canary"}, "GIT_GPG_HOME", "Expected an absolute directory path"),
        ({"GIT_GPG_PROGRAM": None}, "GIT_GPG_PROGRAM", "Expected an absolute executable path or an empty value"),
        ({"GIT_GPG_PROGRAM": "relative-private-canary"}, "GIT_GPG_PROGRAM", "Expected an absolute executable path"),
    ],
)
def test_invalid_signing_values_raise_owned_safe_errors(
    tmp_path: Path, override: dict[str, str | None], field: str, reason: str
) -> None:
    values: dict[str, str | None] = {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_HOME": str(tmp_path), **override}
    with pytest.raises(GpgConfigurationError) as caught:
        signing_configuration(values, Policy(tmp_path, (tmp_path,), ("gpg",)))
    assert caught.value.code == ExitCode.CONFIG_ERROR
    assert caught.value.target == field
    assert reason in str(caught.value)
    assert "private-canary" not in str(caught.value)
    assert "restart" in str(caught.value)


def test_valueless_program_declaration_is_not_an_omitted_override(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"GIT_SIGNING_KEY={'A' * 40}\nGIT_GPG_HOME={tmp_path}\nGIT_GPG_PROGRAM\n")
    with pytest.raises(GpgConfigurationError, match="GIT_GPG_PROGRAM"):
        signing_configuration(read_dotenv(tmp_path), Policy(tmp_path, (tmp_path,), ("gpg",)))


def test_home_resolution_os_error_preserves_io_classification_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, strict: bool = False) -> Never:
        raise PermissionError(errno.EACCES, "private-canary")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", denied)
        with pytest.raises(GpgIOError) as caught:
            signing_configuration(
                {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_HOME": str(tmp_path)},
                Policy(tmp_path, (tmp_path,), ("gpg",)),
            )
    assert isinstance(caught.value, LocalIOError)
    assert caught.value.code == ExitCode.IO_ERROR
    assert caught.value.operation == "Resolve GnuPG home"
    assert "Access denied" in str(caught.value)
    assert "private-canary" not in str(caught.value)


def discovery_install(root: Path, home: Path, *, matching: bool = True) -> Path:
    """A disposable GnuPG installation; only fixed metadata queries are supported."""
    import sys
    from urllib.parse import quote

    bindir = root / "bin"
    bindir.mkdir()
    program = bindir / "gpg"
    program.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    program.chmod(0o700)
    reported = program if matching else bindir / "different-gpg"
    config = bindir / "gpgconf"
    config.write_text(
        f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
        "if 'GNUPGHOME' in os.environ: raise SystemExit(44)\n"
        f"with Path({str(root / 'queries')!r}).open('a') as stream: stream.write(' '.join(sys.argv[1:])+'\\n')\n"
        f"if sys.argv[1:] == ['--list-components']: print({'gpg:OpenPGP:' + quote(str(reported), safe='/')!r})\n"
        f"elif sys.argv[1:] == ['--list-dirs', 'homedir']: print({str(home)!r})\n"
        "else: raise SystemExit(99)\n"
    )
    config.chmod(0o700)
    return program


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
@pytest.mark.parametrize("home_value", ["omitted", ""])
def test_default_home_is_discovered_and_frozen_without_extra_command_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home_value: str
) -> None:
    home = tmp_path / "keys"
    home.mkdir(mode=0o700)
    program = discovery_install(tmp_path, home)
    values: dict[str, str | None] = {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}
    if home_value != "omitted":
        values["GIT_GPG_HOME"] = home_value
    monkeypatch.setenv("GNUPGHOME", str(tmp_path / "ambient-must-not-be-used"))
    signing = signing_configuration(values, Policy(tmp_path, (home,), ("gpg",)))
    assert signing.home == home.resolve()
    assert signing.program == program.resolve()
    assert (tmp_path / "queries").read_text().splitlines() == ["--list-components", "--list-dirs homedir"]
    next_home = tmp_path / "keys-next"
    next_home.mkdir()
    config = program.parent / "gpgconf"
    config.write_text(config.read_text().replace(repr(str(home)), repr(str(next_home))))
    assert signing.home == home.resolve()
    assert signing_configuration(values, Policy(tmp_path, (tmp_path,), ("gpg",))).home == next_home.resolve()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_explicit_home_does_not_require_gpgconf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "keys"
    home.mkdir()
    program = discovery_install(tmp_path, home, matching=False)
    (program.parent / "gpgconf").unlink()
    monkeypatch.setenv("PATH", "")
    signing = signing_configuration(
        {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program), "GIT_GPG_HOME": str(home)},
        Policy(tmp_path, (home,), ("gpg",)),
    )
    assert signing.home == home.resolve()
    assert not (tmp_path / "queries").exists()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_mismatched_installation_never_supplies_a_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "must-not-create"
    program = discovery_install(tmp_path, home, matching=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GpgDiscoveryError) as caught:
        signing_configuration(
            {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (tmp_path,), ("gpg",))
        )
    assert caught.value.code == ExitCode.DEPENDENCY
    assert "matching" in str(caught.value)
    assert (tmp_path / "queries").read_text().splitlines() == ["--list-components"]
    assert not home.exists()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_discovered_signing_home_is_not_subject_to_workspace_path_permission(tmp_path: Path) -> None:
    home = tmp_path / "keys"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program = discovery_install(tmp_path, home)
    result = signing_configuration(
        {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(workspace, (workspace,), ("gpg",))
    )
    assert result.home == home.resolve()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_missing_discovered_home_is_not_created(tmp_path: Path) -> None:
    home = tmp_path / "must-not-create"
    program = discovery_install(tmp_path, home)
    with pytest.raises(GpgIOError):
        signing_configuration(
            {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (tmp_path,), ("gpg",))
        )
    assert not home.exists()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_failed_discovery_does_not_expose_output_or_use_another_installation(tmp_path: Path) -> None:
    program = discovery_install(tmp_path, tmp_path / "keys")
    config = program.parent / "gpgconf"
    config.write_text(config.read_text().split("if sys.argv")[0] + "print('private-canary'); raise SystemExit(2)\n")
    with pytest.raises(GpgDiscoveryError) as caught:
        signing_configuration(
            {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (tmp_path,), ("gpg",))
        )
    assert caught.value.code == ExitCode.GENERAL
    assert "private-canary" not in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_path_candidate_must_report_the_selected_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "keys"
    home.mkdir()
    program = discovery_install(tmp_path, home)
    other = tmp_path / "other"
    other.mkdir()
    candidate = other / "gpgconf"
    (program.parent / "gpgconf").rename(candidate)
    monkeypatch.setenv("PATH", str(other))
    result = signing_configuration(
        {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (home,), ("gpg",))
    )
    assert result.home == home.resolve()


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_discovery_timeout_is_safe_and_does_not_select_another_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from local_dev_mcp.tools.gpg import configuration

    program = discovery_install(tmp_path, tmp_path / "keys")

    def timeout(*args: object, **kwargs: object) -> Never:
        raise subprocess.TimeoutExpired("private-canary", 10, output=b"private-canary")

    with monkeypatch.context() as patch:
        patch.setattr(configuration.subprocess, "run", timeout)
        with pytest.raises(GpgDiscoveryError) as caught:
            signing_configuration(
                {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (tmp_path,), ("gpg",))
            )
    assert caught.value.code == ExitCode.GENERAL
    assert "timed out" in str(caught.value)
    assert "private-canary" not in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="executable metadata fixtures use POSIX shebangs")
def test_gpg_permission_is_checked_before_discovery(tmp_path: Path) -> None:
    program = discovery_install(tmp_path, tmp_path / "keys")
    with pytest.raises(PolicyError):
        signing_configuration(
            {"GIT_SIGNING_KEY": "A" * 40, "GIT_GPG_PROGRAM": str(program)}, Policy(tmp_path, (tmp_path,), ())
        )
    assert not (tmp_path / "queries").exists()
