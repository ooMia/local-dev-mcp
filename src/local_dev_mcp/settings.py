"""Explicit configuration file reads and fixed config.toml access policy."""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values

from .errors import ConfigurationError, LocalIOError

type JobKind = Literal["lint", "format", "test"]
type TestLevel = Literal["unit", "integration", "e2e"]


@dataclass(frozen=True)
class JobDefinition:
    name: str
    kind: JobKind
    permission: str
    argv: tuple[str, ...]
    level: TestLevel | None = None
    default_targets: tuple[str, ...] = ()
    timeout_seconds: int = 300


@dataclass(frozen=True)
class PolicyValues:
    allowed_paths: tuple[str, ...]
    allowed_commands: tuple[str, ...]
    read_only_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    jobs: tuple[JobDefinition, ...] = ()
    public_environment: tuple[str, ...] = ()


def parse_policy(text: str) -> PolicyValues:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError.invalid_toml(error.lineno, error.colno) from error
    return PolicyValues(
        tuple(_strings(data, "filesystem", "allowed_paths")),
        tuple(_strings(data, "system", "allowed_commands")),
        tuple(_strings(data, "filesystem", "read_only_paths", optional=True)),
        tuple(_strings(data, "filesystem", "denied_paths", optional=True)),
        _jobs(data),
        _public_environment(data),
    )


def _job_setting(target: str, reason: str) -> ConfigurationError:
    return ConfigurationError(target, reason, "Correct the named job setting in config.toml and restart.")


def _job_strings(value: object, target: str, *, maximum: int = 128) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _job_setting(target, f"Expected 1..{maximum} nonempty strings.")
    entries = cast(list[object], value)
    if not 1 <= len(entries) <= maximum:
        raise _job_setting(target, f"Expected 1..{maximum} nonempty strings.")
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip() or "\0" in entry or len(entry) > 4096:
            raise _job_setting(target, "Expected nonempty strings of at most 4096 characters, without NUL.")
        result.append(entry)
    if sum(len(entry.encode("utf-8")) for entry in result) > 32768:
        raise _job_setting(target, "The combined argument data exceeds 32 KiB.")
    return tuple(result)


def _jobs(data: Mapping[str, object]) -> tuple[JobDefinition, ...]:
    if "jobs" not in data:
        return ()
    raw = data["jobs"]
    if not isinstance(raw, dict):
        raise _job_setting("jobs", "Expected a TOML table with at most 64 named jobs.")
    definitions = cast(dict[str, object], raw)
    if len(definitions) > 64:
        raise _job_setting("jobs", "Expected a TOML table with at most 64 named jobs.")
    jobs: list[JobDefinition] = []
    fields = {"kind", "permission", "argv", "level", "default_targets", "timeout_seconds"}
    for name, entry in definitions.items():
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            raise _job_setting("jobs", "Job names must use lowercase snake_case and contain at most 64 characters.")
        target = f"jobs.{name}"
        if not isinstance(entry, dict):
            raise _job_setting(target, "Expected a TOML table.")
        values = cast(dict[str, object], entry)
        if set(values) - fields:
            raise _job_setting(target, "Unknown job fields are not supported.")
        kind = values.get("kind")
        if kind not in ("lint", "format", "test"):
            raise _job_setting(f"{target}.kind", "Expected lint, format or test.")
        permission = values.get("permission")
        if (
            not isinstance(permission, str)
            or len(permission) > 128
            or re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+", permission) is None
        ):
            raise _job_setting(
                f"{target}.permission", "Expected a dotted logical permission of at most 128 characters."
            )
        argv = _job_strings(values.get("argv"), f"{target}.argv")
        level = values.get("level")
        targets: tuple[str, ...] = ()
        if kind == "test":
            if level not in ("unit", "integration", "e2e"):
                raise _job_setting(f"{target}.level", "Test jobs require unit, integration or e2e.")
            targets = _job_strings(values.get("default_targets"), f"{target}.default_targets")
        elif "level" in values or "default_targets" in values:
            raise _job_setting(target, "Only test jobs accept level and default_targets.")
        timeout = values.get("timeout_seconds", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise _job_setting(f"{target}.timeout_seconds", "Expected an integer from 1 to 3600.")
        jobs.append(JobDefinition(name, kind, permission, argv, cast(TestLevel | None, level), targets, timeout))
    return tuple(jobs)


def _public_environment(data: Mapping[str, object]) -> tuple[str, ...]:
    if "jobs_environment" not in data:
        return ()
    raw = data["jobs_environment"]
    if not isinstance(raw, dict):
        raise _job_setting("jobs_environment", "Expected a TOML table.")
    values = cast(dict[str, object], raw)
    if set(values) - {"public_names"}:
        raise _job_setting("jobs_environment", "Unknown environment fields are not supported.")
    names = values.get("public_names", [])
    if not isinstance(names, list):
        raise _job_setting("jobs_environment.public_names", "Expected a list of at most 128 environment names.")
    entries = cast(list[object], names)
    if len(entries) > 128:
        raise _job_setting("jobs_environment.public_names", "Expected a list of at most 128 environment names.")
    result: list[str] = []
    for name in entries:
        if not isinstance(name, str) or len(name) > 128 or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise _job_setting("jobs_environment.public_names", "Expected valid environment variable names.")
        if name in result:
            raise _job_setting("jobs_environment.public_names", "Duplicate environment names are not supported.")
        result.append(name)
    return tuple(result)


def _strings(data: Mapping[str, object], section: str, key: str, *, optional: bool = False) -> list[str]:
    section_value = data.get(section)
    target = f"{section}.{key}"
    if section not in data:
        raise ConfigurationError.missing_setting(target)
    if not isinstance(section_value, dict):
        raise ConfigurationError.invalid_section(section)
    if key not in section_value:
        if optional:
            return []
        raise ConfigurationError.missing_setting(target)
    values = cast(Mapping[str, object], section_value)[key]
    if not isinstance(values, list):
        raise ConfigurationError.invalid_setting(target)
    strings: list[str] = []
    for value in cast(list[object], values):
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError.invalid_setting(target)
        strings.append(value)
    return strings


def protected_name(name: str) -> bool:
    folded = name.casefold()
    return name != ".env.example" and (folded == ".git" or folded == ".env" or folded.startswith(".env."))


def _restricted(path: Path, scopes: tuple[Path, ...]) -> bool:
    # Negative rules also cover case aliases on case-insensitive filesystems;
    # positive grants retain exact spelling and do not broaden Linux access.
    parts = tuple(part.casefold() for part in path.parts)
    return any(parts[: len(scope.parts)] == tuple(part.casefold() for part in scope.parts) for scope in scopes)


def _configured_paths(root: Path, values: tuple[str, ...], target: str) -> tuple[Path, ...]:
    try:
        return tuple((root / value).resolve() for value in values)
    except OSError as error:
        raise LocalIOError.resolve_configured_paths(error, target) from error
    except ValueError as error:
        raise ConfigurationError.invalid_path(target) from error


def parse_dotenv(text: str) -> dict[str, str | None]:
    return dotenv_values(stream=StringIO(text), interpolate=False)


def read_dotenv(root: Path) -> dict[str, str | None]:
    """Read raw root/.env values; a valueless declaration is None until validation.

    Never change os.environ, search parent directories or interpolate values.
    """
    try:
        with (root / ".env").open(encoding="utf-8") as stream:
            return parse_dotenv(stream.read())
    except FileNotFoundError:
        raise ConfigurationError.missing_file(".env") from None
    except OSError as error:
        raise LocalIOError.read_environment(error) from None
    except UnicodeError:
        raise ConfigurationError.invalid_encoding(".env") from None


@dataclass(frozen=True)
class Policy:
    root: Path
    allowed_paths: tuple[Path, ...]
    allowed_commands: tuple[str, ...]
    read_only_paths: tuple[Path, ...] = ()
    denied_paths: tuple[Path, ...] = ()
    allow_policy_edits: bool = False
    jobs: tuple[JobDefinition, ...] = ()
    public_environment: tuple[str, ...] = ()

    @classmethod
    def load(cls, directory: Path, *, allow_policy_edits: bool = False) -> Policy:
        try:
            root = directory.resolve()
        except OSError as error:
            raise LocalIOError.resolve_project(error) from error
        try:
            text = (root / "config.toml").read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ConfigurationError.missing_file("config.toml") from error
        except OSError as error:
            raise LocalIOError.read_configuration(error) from error
        except UnicodeDecodeError as error:
            raise ConfigurationError.invalid_encoding("config.toml") from error
        values = parse_policy(text)
        return cls(
            root,
            _configured_paths(root, values.allowed_paths, "filesystem.allowed_paths"),
            values.allowed_commands,
            _configured_paths(root, values.read_only_paths, "filesystem.read_only_paths"),
            _configured_paths(root, values.denied_paths, "filesystem.denied_paths"),
            allow_policy_edits,
            values.jobs,
            values.public_environment,
        )

    def require_project_access(self) -> None:
        if not self.allows_path(self.root, traverse=True):
            raise PermissionError("Project directory is not allowed by config.toml.")

    def allows_path(self, path: Path, *, write: bool = False, traverse: bool = False) -> bool:
        """Check a normalized absolute path without touching the filesystem.

        Traversal exposes only ancestors needed to reach allowed paths. Callers
        still verify actual directory entries and symlinks at the I/O boundary.
        """
        if not path.is_absolute() or any(part == ".." or "\0" in part for part in path.parts):
            return False
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        if any(protected_name(part) for part in relative.parts) or _restricted(path, self.denied_paths):
            return False
        allowed = any(path.is_relative_to(scope) for scope in self.allowed_paths)
        if not allowed and traverse and not write:
            allowed = any(scope.is_relative_to(path) and self.allows_path(scope) for scope in self.allowed_paths)
        if not allowed:
            return False
        if write:
            if _restricted(path, self.read_only_paths):
                return False
            if not self.allow_policy_edits:
                if any(part.casefold() == ".gitignore" for part in relative.parts):
                    return False
                if len(relative.parts) == 1 and relative.name.casefold() == "config.toml":
                    return False
        return True

    def require_path_access(self, path: Path, *, write: bool = False, traverse: bool = False) -> None:
        if not self.allows_path(path, write=write, traverse=traverse):
            setting = "allowed_paths, read_only_paths and denied_paths" if write else "allowed_paths and denied_paths"
            operation = "Direct file writing" if write else "Path access"
            raise PermissionError(
                f"{operation} is not allowed by config.toml or the protected-path policy. Review filesystem.{setting}."
            )

    def require_command(self, command: str) -> None:
        if command not in self.allowed_commands:
            raise PermissionError(f"System command {command} is not allowed by config.toml.")
