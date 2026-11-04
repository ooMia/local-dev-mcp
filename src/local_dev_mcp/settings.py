"""Explicit configuration file reads and fixed config.toml access policy."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import dotenv_values

from .errors import ConfigurationError, LocalIOError


def read_dotenv(root: Path) -> dict[str, str | None]:
    """Read raw root/.env values; a valueless declaration is None until validation.

    Never change os.environ, search parent directories or interpolate values.
    """
    try:
        with (root / ".env").open(encoding="utf-8") as stream:
            return dotenv_values(stream=stream, interpolate=False)
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

    @classmethod
    def load(cls, directory: Path) -> Policy:
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
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError.invalid_toml(error.lineno, error.colno) from error

        paths = cls._strings(data, "filesystem", "allowed_paths")
        commands = cls._strings(data, "system", "allowed_commands")
        try:
            allowed_paths = tuple((root / p).resolve() for p in paths)
        except OSError as error:
            raise LocalIOError.resolve_configured_paths(error) from error
        except ValueError as error:
            raise ConfigurationError.invalid_path() from error
        return cls(root, allowed_paths, tuple(commands))

    @staticmethod
    def _strings(data: Mapping[str, object], section: str, key: str) -> list[str]:
        section_value = data.get(section)
        target = f"{section}.{key}"
        if section not in data:
            raise ConfigurationError.missing_setting(target)
        if not isinstance(section_value, dict):
            raise ConfigurationError.invalid_section(section)
        if key not in section_value:
            raise ConfigurationError.missing_setting(target)
        # tomllib guarantees string keys; nested values remain untrusted objects.
        values = cast(Mapping[str, object], section_value)[key]
        if not isinstance(values, list):
            raise ConfigurationError.invalid_setting(target)
        strings: list[str] = []
        # The list check establishes the container, not its element types.
        for value in cast(list[object], values):
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError.invalid_setting(target)
            strings.append(value)
        return strings

    def require_project_access(self) -> None:
        if not any(self.root.is_relative_to(path) for path in self.allowed_paths):
            raise PermissionError("Project directory is not allowed by config.toml.")

    def require_command(self, command: str) -> None:
        if command not in self.allowed_commands:
            raise PermissionError(f"System command {command} is not allowed by config.toml.")
