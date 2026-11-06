"""CLI failures carry safe, operation-specific diagnostics, never raw values."""

from enum import IntEnum
from typing import Self


# Internal tool-definition errors are programming failures, not user policy errors.
class ToolRegistrationError(ValueError):
    pass


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERAL = 1
    USAGE = 2
    CONFIG_ERROR = 3
    IO_ERROR = 4
    DEPENDENCY = 5
    INTERRUPTED = 130


class CliError(Exception):
    code: ExitCode
    operation: str
    target: str
    reason: str
    action: str
    child_exit_code: int | None

    def __init__(
        self,
        code: ExitCode,
        operation: str,
        target: str,
        reason: str,
        action: str,
        *,
        child_exit_code: int | None = None,
    ) -> None:
        self.code = code
        self.operation = operation
        self.target = target
        self.reason = reason
        self.action = action
        self.child_exit_code = child_exit_code
        super().__init__(self.diagnostic())

    def diagnostic(self) -> str:
        lines = [f"{self.operation} [{self.target}]: {self.reason}"]
        if self.child_exit_code is not None:
            lines.append(f"Child exit code: {self.child_exit_code}")
            if self.child_exit_code < 0:
                lines.append(f"Child signal: {-self.child_exit_code}")
        lines.append(f"Action: {self.action}")
        return "\n".join(lines)


# Configuration loading/validation and policy denial share CONFIG_ERROR.
class ConfigurationError(CliError):
    def __init__(self, target: str, reason: str, action: str) -> None:
        super().__init__(ExitCode.CONFIG_ERROR, "Load configuration", target, reason, action)

    @classmethod
    def missing_file(cls, target: str) -> Self:
        reason = "The environment file is missing." if target == ".env" else "The configuration file is missing."
        return cls(target, reason, f"Prepare {target} in the selected workspace.")

    @classmethod
    def invalid_encoding(cls, target: str) -> Self:
        return cls(target, "The file is not valid UTF-8.", f"Save {target} as UTF-8.")

    @classmethod
    def invalid_toml(cls, line: int, column: int) -> Self:
        return cls(
            "config.toml",
            f"Invalid TOML syntax (line {line}, column {column}).",
            "Correct the TOML syntax; configuration values are not printed.",
        )

    @classmethod
    def missing_setting(cls, target: str) -> Self:
        return cls(
            target,
            "Required setting is missing.",
            "Add this setting as a list of strings in config.toml.",
        )

    @classmethod
    def invalid_section(cls, section: str) -> Self:
        return cls(
            section,
            "Expected a TOML table.",
            f"Define [{section}] with its required settings in config.toml.",
        )

    @classmethod
    def invalid_setting(cls, target: str) -> Self:
        return cls(
            target,
            "Expected a list of nonempty strings.",
            "Correct this setting in config.toml.",
        )

    @classmethod
    def invalid_path(cls, target: str = "filesystem.allowed_paths") -> Self:
        return cls(
            target,
            "Invalid path value.",
            "Use valid filesystem paths.",
        )

    @classmethod
    def missing_environment_values(cls, names: list[str]) -> Self:
        return cls(
            ", ".join(names),
            "Required values in .env are missing or empty.",
            "Set the named variables in .env; do not pass credentials as CLI arguments.",
        )

    @classmethod
    def invalid_tunnel_id(cls) -> Self:
        return cls(
            "CONTROL_PLANE_TUNNEL_ID",
            "Invalid Tunnel ID format.",
            "Use tunnel_ followed by 32 lowercase letters or digits; the value is not printed.",
        )

    @classmethod
    def environment_value(cls, name: str, reason: str, guide: str) -> Self:
        return cls(name, reason, f"{guide} Set {name} in .env and restart; values are not printed.")


class PolicyError(CliError):
    @classmethod
    def command_denied(cls, command: str) -> Self:
        return cls(
            ExitCode.CONFIG_ERROR,
            "Check execution policy",
            command,
            f"System command {command} is not allowed by config.toml.",
            "Review system.allowed_commands in config.toml and restart.",
        )

    @classmethod
    def path_denied(cls, target: str) -> Self:
        return cls(
            ExitCode.CONFIG_ERROR,
            "Check execution policy",
            target,
            "The required path is not allowed by config.toml.",
            "Review filesystem.allowed_paths in config.toml and restart.",
        )


# Executables chosen once at startup, separate from external process outcomes.
class DependencyError(CliError):
    @classmethod
    def unavailable(cls, command: str) -> Self:
        return cls(
            ExitCode.DEPENDENCY,
            "Resolve executable",
            command,
            "The executable is missing or cannot be executed.",
            f"Check `command -v {command}`, installation and executable permissions; restart after fixing.",
        )

    @classmethod
    def signing_entry_point(cls) -> Self:
        return cls(
            ExitCode.DEPENDENCY,
            "Resolve signing entry point",
            "local-dev-mcp-gpg",
            "The entry point is missing or not executable in the server's Python environment.",
            "Run `uv sync --locked` in the server environment and restart; PATH alternatives are not used.",
        )


# Local filesystem/transport failures retain their operation, not raw OSError text.
class LocalIOError(CliError):
    def __init__(self, operation: str, target: str, error: OSError) -> None:
        if isinstance(error, PermissionError):
            reason = "Access denied."
            action = "Check access permissions for the target and its parent directories."
        elif isinstance(error, FileNotFoundError):
            reason = "The target no longer exists."
            action = "Check the target and the working directory before restarting."
        else:
            reason = f"Local I/O failed ({type(error).__name__}, errno={error.errno})."
            action = "Check the target's filesystem and device state before restarting."
        super().__init__(ExitCode.IO_ERROR, operation, target, reason, action)

    @classmethod
    def locate_project(cls, error: OSError) -> Self:
        return cls("Locate project", "execution directory", error)

    @classmethod
    def resolve_project(cls, error: OSError) -> Self:
        return cls("Resolve project", "workspace", error)

    @classmethod
    def read_configuration(cls, error: OSError) -> Self:
        return cls("Read configuration", "config.toml", error)

    @classmethod
    def resolve_configured_paths(cls, error: OSError, target: str = "filesystem.allowed_paths") -> Self:
        return cls("Resolve configured paths", target, error)

    @classmethod
    def read_tunnel_environment(cls, error: OSError) -> Self:
        return cls("Read Tunnel configuration", ".env", error)

    @classmethod
    def read_environment(cls, error: OSError) -> Self:
        return cls("Read runtime configuration", ".env", error)

    @classmethod
    def run_stdio_server(cls, error: OSError) -> Self:
        return cls("Run MCP server", "stdio transport", error)

    @classmethod
    def run_tunnel(cls, error: OSError) -> Self:
        return cls("Run Tunnel client", "workspace / process I/O", error)


# Tunnel process outcomes: dependency, child failure, cleanup, and interruption.
# Interruption is an expected outcome; diagnostics log it at INFO, not ERROR.
class TunnelError(CliError):
    @classmethod
    def dependency_unavailable(cls) -> Self:
        return cls(
            ExitCode.DEPENDENCY,
            "Start Tunnel client",
            "tunnel-client",
            "The executable is missing or cannot be executed.",
            "Check the tunnel-client installation, PATH, and executable permissions.",
        )

    @classmethod
    def unsuccessful_exit(cls, child_exit_code: int) -> Self:
        return cls(
            ExitCode.GENERAL,
            "Run Tunnel client",
            "tunnel-client",
            "The client exited unsuccessfully; its failure cause is not classified.",
            "Check Tunnel configuration and connectivity; no automatic retry was attempted.",
            child_exit_code=child_exit_code,
        )

    @classmethod
    def cleanup_unconfirmed(cls) -> Self:
        return cls(
            ExitCode.GENERAL,
            "Stop Tunnel client",
            "directly managed process",
            "Process termination could not be confirmed. Interruption does not imply successful cleanup.",
            "Inspect the Tunnel process before restarting; descendant processes are outside this guarantee.",
        )

    @classmethod
    def interrupted(cls, child_exit_code: int) -> Self:
        return cls(
            ExitCode.INTERRUPTED,
            "Stop Tunnel client",
            "directly managed process",
            "Execution interrupted; the direct process has exited.",
            "No restart is scheduled. This does not confirm termination of all descendants.",
            child_exit_code=child_exit_code,
        )


# CLI-wide outcomes without a child-process result.
class ExecutionError(CliError):
    @classmethod
    def interrupted(cls) -> Self:
        return cls(
            ExitCode.INTERRUPTED,
            "Stop execution",
            "CLI",
            "Execution interrupted.",
            "No restart is scheduled.",
        )

    @classmethod
    def unexpected(cls, error: Exception) -> Self:
        failure = cls(
            ExitCode.GENERAL,
            "Run CLI",
            "execution",
            f"Internal failure ({type(error).__name__}).",
            "Reproduce the failure in tests and inspect the implementation; raw exception details are not printed.",
        )
        failure.__cause__ = error
        return failure
