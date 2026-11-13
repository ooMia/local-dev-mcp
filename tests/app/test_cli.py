"""Exercise the CLI interface, including real library-managed child processes."""

import asyncio
import errno
import logging
import os
import re
import signal
import sys
from collections.abc import Coroutine
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Never, Self

import pytest
from shellous import Result, sh

import local_dev_mcp.tools.tunnel_client._client as tunnel
from local_dev_mcp.app import cli
from local_dev_mcp.errors import ExitCode
from local_dev_mcp.execution import base_environment
from local_dev_mcp.settings import Policy

SECRET = "sk-test-canary-never-print"


pytestmark = pytest.mark.integration


def configure(root: Path) -> None:
    (root / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["git", "tunnel-client"]\n'
    )
    (root / ".env").write_text(
        f"CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nCONTROL_PLANE_API_KEY={SECRET}\n"
        "GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n"
    )


def invoke(root: Path, *args: str, env: dict[str, str] | None = None) -> Result:
    async def scenario() -> Result:
        return await sh.result(sys.executable, "-I", "-m", "local_dev_mcp", *args).set(
            cwd=root,
            inherit_env=False,
            env=env or base_environment(),
            timeout=15,
        )

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (None, "configuration file is missing"),
        ('secret="' + SECRET, "Invalid TOML syntax (line"),
        ("[filesystem]\nallowed_paths=[]\n", "system.allowed_commands"),
        (f'filesystem="{SECRET}"\n', "Expected a TOML table"),
        (f'system="{SECRET}"\n[filesystem]\nallowed_paths=[]\n', "Expected a TOML table"),
        (
            '[filesystem]\nallowed_paths="' + SECRET + '"\n[system]\nallowed_commands=[]',
            "list of nonempty strings",
        ),
    ],
)
def test_config_failures_are_contextual_and_redacted(tmp_path: Path, contents: str | None, reason: str) -> None:
    if contents is not None:
        (tmp_path / "config.toml").write_text(contents)
    result = invoke(tmp_path, "run")
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert reason in result.error
    assert "Action:" in result.error
    assert result.output == ""
    assert SECRET not in result.error
    assert "Traceback" not in result.error


def test_configuration_permission_error_is_io_not_executable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "run"])

    def denied(*args: object, **kwargs: object) -> Never:
        raise PermissionError(errno.EACCES, SECRET)

    monkeypatch.setattr(Path, "read_text", denied)
    assert cli.main() == ExitCode.IO_ERROR
    error = capsys.readouterr().err
    assert "Read configuration [config.toml]" in error
    assert "Access denied" in error
    assert SECRET not in error


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_project_resolution_error_is_not_config_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: type[OSError],
) -> None:
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "run"])

    def denied(*args: object, **kwargs: object) -> Never:
        raise failure(SECRET)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", denied)
        code = cli.main()
    assert code == ExitCode.IO_ERROR
    error = capsys.readouterr().err
    assert "Resolve project [workspace]" in error
    assert "Read configuration" not in error
    assert SECRET not in error


def test_missing_env_and_missing_executable_are_different(tmp_path: Path) -> None:
    configure(tmp_path)
    envfile = tmp_path / ".env"
    contents = envfile.read_text()
    envfile.unlink()
    missing_env = invoke(tmp_path, "connect")
    assert missing_env.exit_code == ExitCode.CONFIG_ERROR
    assert "[.env]" in missing_env.error
    assert "file is missing" in missing_env.error
    envfile.write_text(contents)
    missing_binary = invoke(tmp_path, "connect", env={"PATH": str(tmp_path / "no-binaries")})
    assert missing_binary.exit_code == ExitCode.DEPENDENCY
    assert "Resolve executable [tunnel-client]" in missing_binary.error


def fake_client(root: Path, script: str) -> dict[str, str]:
    bindir = root / "bin"
    bindir.mkdir()
    binary = bindir / "tunnel-client"
    binary.write_text(f"#!{sys.executable}\n" + script)
    binary.chmod(0o755)
    return {**base_environment(), "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}


@pytest.mark.skipif(os.name != "posix", reason="executable script fixture uses a POSIX shebang")
@pytest.mark.parametrize(
    ("ending", "child_code"),
    [
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(3)", 3),
        ("import signal; os.kill(os.getpid(), signal.SIGTERM)", -signal.SIGTERM),
    ],
)
def test_external_exit_is_normalized_and_raw_output_discarded(tmp_path: Path, ending: str, child_code: int) -> None:
    configure(tmp_path)
    env = fake_client(
        tmp_path,
        "import os,sys\n"
        "print(os.environ['CONTROL_PLANE_API_KEY'],flush=True)\n"
        "print(os.environ['CONTROL_PLANE_API_KEY'],file=sys.stderr,flush=True)\n" + ending + "\n",
    )
    result = invoke(tmp_path, "connect", env=env)
    assert result.exit_code == (ExitCode.SUCCESS if child_code == 0 else ExitCode.GENERAL)
    assert result.output == ""
    assert SECRET not in result.error
    if child_code == 0:
        assert "Tunnel client exited with code 0" in result.error
        assert "does not confirm a working Chat connection" in result.error
    if child_code:
        assert f"Child exit code: {child_code}" in result.error
        if child_code < 0:
            assert f"Child signal: {-child_code}" in result.error


@pytest.mark.skipif(os.name != "posix", reason="executable script fixture uses a POSIX shebang")
def test_connect_warns_about_commit_preparation_and_launches_tunnel(tmp_path: Path) -> None:
    configure(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["git", "tunnel-client"]\n'
    )
    with (tmp_path / ".env").open("a") as stream:
        stream.write("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\nGIT_COMMIT_SIGN=true\n")
    env = fake_client(tmp_path, "from pathlib import Path\nPath('tunnel-started').touch()\n")
    result = invoke(tmp_path, "connect", env=env)
    assert result.exit_code == ExitCode.SUCCESS
    assert "WARNING" in result.error
    assert "System command gpg is not allowed" in result.error
    assert "system.allowed_commands" in result.error
    assert (tmp_path / "tunnel-started").exists()
    assert result.output == ""
    assert SECRET not in result.error


def test_unexpected_internal_error_is_general_and_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> Never:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main() == ExitCode.GENERAL
    error = capsys.readouterr().err
    assert "RuntimeError" in error
    assert SECRET not in error
    assert "Traceback" not in error


def test_usage_and_help_have_standard_exit_status(tmp_path: Path) -> None:
    assert invoke(tmp_path, "unknown-command").exit_code == ExitCode.USAGE
    assert invoke(tmp_path, "--help").exit_code == ExitCode.SUCCESS


def test_run_warns_about_missing_identity_without_failing_startup(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["git"]\n')
    (tmp_path / ".env").write_text("GIT_COMMIT_SIGN=false\n")
    result = invoke(tmp_path, "run")
    assert result.exit_code == ExitCode.SUCCESS
    assert "GIT_USER_NAME" in result.error
    assert "git config get user.name" in result.error
    assert "WARNING" in result.error
    assert result.output == ""


@pytest.mark.parametrize(("flag", "expected"), [("yes", "GIT_COMMIT_SIGN"), ("true", "gpg is not allowed")])
def test_run_validates_signing_flag_and_conditional_permission_at_startup(
    tmp_path: Path, flag: str, expected: str
) -> None:
    (tmp_path / "config.toml").write_text('[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["git"]\n')
    (tmp_path / ".env").write_text(f"GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\nGIT_COMMIT_SIGN={flag}\n")
    result = invoke(tmp_path, "run")
    assert result.exit_code == ExitCode.SUCCESS
    assert expected in result.error
    assert "WARNING" in result.error
    assert result.output == ""


@pytest.mark.skipif(os.name != "posix", reason="own-process SIGINT fixture")
def test_ctrl_c_confirms_direct_child_exit(tmp_path: Path) -> None:
    configure(tmp_path)
    env = fake_client(
        tmp_path,
        "import os,time\nfrom pathlib import Path\nPath('child.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n",
    )
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    child_pid: int | None = None

    async def scenario() -> Result:
        nonlocal child_pid
        command = (
            sh.result(sys.executable, "-I", "-m", "local_dev_mcp", "connect")
            .set(
                cwd=tmp_path,
                inherit_env=False,
                env=env,
                timeout=25,
            )
            .stdout(stdout)
            .stderr(stderr)
        )
        async with command as process:
            # Interpreters launched under xdist contention may exceed a 5-second startup budget.
            async with asyncio.timeout(15):
                while not (tmp_path / "child.pid").exists():
                    await asyncio.sleep(0.02)
            child_pid = int((tmp_path / "child.pid").read_text())
            process.send_signal(signal.SIGINT)
        return process.result()

    try:
        result = asyncio.run(scenario())
        assert result.exit_code == ExitCode.INTERRUPTED
        assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO ", stderr.read_text())
        assert "direct process has exited" in stderr.read_text()
        assert "Traceback" not in stderr.read_text()
        assert child_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)  # Independent observation, not product process management.
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_unconfirmed_cleanup_is_general_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "connect"])

    class FailedCleanup:
        returncode: int | None = None
        cancelled: bool = True

        def set(self, **kwargs: object) -> Self:
            return self

        def stdin(self, value: object) -> Self:
            return self

        def stdout(self, value: object) -> Self:
            return self

        def stderr(self, value: object) -> Self:
            return self

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> Never:
            raise RuntimeError(SECRET)

    class FakeCommands:
        DEVNULL: int = sh.DEVNULL

        @staticmethod
        def result(*args: object) -> FailedCleanup:
            return FailedCleanup()

    # Fault injection: safely simulate the library's unkillable-process path.
    monkeypatch.setattr(tunnel, "sh", FakeCommands)
    assert cli.main() == ExitCode.GENERAL
    error = capsys.readouterr().err
    assert "termination could not be confirmed" in error
    assert SECRET not in error
    assert "Traceback" not in error


def test_interrupt_bypassing_async_cleanup_cannot_claim_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "connect"])

    def interrupted(coroutine: Coroutine[object, object, int]) -> Never:
        coroutine.close()
        raise KeyboardInterrupt

    # Simulate asyncio.run's direct KeyboardInterrupt path (e.g. repeated SIGINT).
    monkeypatch.setattr(tunnel.asyncio, "run", interrupted)
    assert cli.main() == ExitCode.GENERAL
    error = capsys.readouterr().err
    assert "termination could not be confirmed" in error
    assert "direct process has exited" not in error


@pytest.mark.parametrize("command", [cli.RUN.command, cli.CONNECT.command])
@pytest.mark.parametrize("explicit_workspace", [False, True])
@pytest.mark.parametrize("allow_policy_edits", [False, True])
def test_registered_command_receives_common_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    explicit_workspace: bool,
    allow_policy_edits: bool,
) -> None:
    target = tmp_path / "target project" if explicit_workspace else tmp_path
    target.mkdir(exist_ok=True)
    configure(target)
    monkeypatch.chdir(tmp_path)
    selector = ["--workspace", str(target)] if explicit_workspace else []
    if allow_policy_edits:
        selector.append("--allow-policy-edits")
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", *selector, command])
    called: list[tuple[str, Path]] = []

    def stdio(policy: Policy) -> int:
        assert policy.allow_policy_edits is allow_policy_edits
        called.append(("run", policy.root))
        return ExitCode.SUCCESS

    def connect(policy: Policy) -> int:
        assert policy.allow_policy_edits is allow_policy_edits
        called.append(("connect", policy.root))
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "COMMANDS", (replace(cli.RUN, handler=stdio), replace(cli.CONNECT, handler=connect)))
    assert cli.main() == ExitCode.SUCCESS
    assert called == [(command, target.resolve())]
    assert Path.cwd() == tmp_path


@pytest.mark.parametrize("workspace", ["", "\0" + SECRET])
def test_invalid_workspace_selector_is_a_redacted_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], workspace: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "--workspace", workspace, "run"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == ExitCode.USAGE
    diagnostic = capsys.readouterr().err
    assert "must be nonempty and contain no NUL" in diagnostic
    assert SECRET not in diagnostic


def test_diagnostics_timestamp_stderr_and_logger_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-dev-mcp", "run"])
    root = logging.getLogger()
    outside_output = StringIO()
    outside_handler = logging.StreamHandler(outside_output)
    root.addHandler(outside_handler)
    root_handlers, root_level = list(root.handlers), root.level
    diagnostic_logger = logging.getLogger("local_dev_mcp.diagnostics")
    original_handlers = list(diagnostic_logger.handlers)
    original_level = diagnostic_logger.level
    original_propagation = diagnostic_logger.propagate
    try:
        for _ in range(2):
            assert cli.main() == ExitCode.CONFIG_ERROR
            captured = capsys.readouterr()
            assert captured.out == ""
            assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ERROR ", captured.err)
            assert captured.err.count("Load configuration") == 1
        assert outside_output.getvalue() == ""
        assert root.handlers == root_handlers
        assert root.level == root_level
        assert diagnostic_logger.handlers == original_handlers
        assert diagnostic_logger.level == original_level
        assert diagnostic_logger.propagate == original_propagation
    finally:
        root.removeHandler(outside_handler)
        outside_handler.close()


@pytest.mark.skipif(os.name != "posix", reason="executable script fixture uses a POSIX shebang")
def test_connect_transports_missing_git_warning_once_and_continues(tmp_path: Path) -> None:
    configure(tmp_path)
    env = fake_client(tmp_path, "from pathlib import Path\nPath('tunnel-started').touch()\n")
    env["PATH"] = str(tmp_path / "bin")
    result = invoke(tmp_path, "connect", env=env)
    assert result.exit_code == ExitCode.SUCCESS
    assert result.error.count("WARNING") == 1
    assert "Resolve executable [git]" in result.error
    assert "restart" in result.error.lower()
    assert (tmp_path / "tunnel-started").exists()
    assert SECRET not in result.error
    assert result.output == ""
