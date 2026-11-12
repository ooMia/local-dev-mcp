"""Strict, declarative quality profiles over registered project Task recipes.

This is deliberately not another command catalogue. The existing config.toml
owns exact argv, timeout, cwd and permissions; the manifest only selects checks.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..settings import JobDefinition

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_MANIFEST_BYTES = 65536


@dataclass(frozen=True)
class QualityProfile:
    name: str
    checks: tuple[str, ...]
    require_clean: bool


@dataclass(frozen=True)
class QualityManifest:
    digest: str
    profiles: tuple[QualityProfile, ...]

    def select(self, name: str, recipes: tuple[JobDefinition, ...]) -> tuple[QualityProfile, tuple[JobDefinition, ...]]:
        profile = next((entry for entry in self.profiles if entry.name == name), None)
        if profile is None:
            raise ValueError("Unknown quality profile.")
        registered = {job.name: job for job in recipes}
        selected: list[JobDefinition] = []
        for check in profile.checks:
            recipe = registered.get(check)
            if recipe is None:
                raise ValueError(f"Quality check {check} is not a registered project task.")
            if recipe.kind == "format":
                raise ValueError(f"Quality check {check} modifies files; use a non-modifying check.")
            selected.append(recipe)
        return profile, tuple(selected)


def parse_quality_manifest(text: str) -> QualityManifest:
    if len(text.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise ValueError("Quality manifest exceeds the 64 KiB limit.")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError("Invalid quality TOML.") from error
    if (
        set(raw) != {"schema_version", "profiles"}
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
    ):
        raise ValueError("Unsupported quality manifest schema or top-level fields.")
    profiles = raw["profiles"]
    if not isinstance(profiles, dict):
        raise ValueError("Quality profiles must contain a named table.")
    typed_profiles = cast(dict[str, object], profiles)
    if not 1 <= len(typed_profiles) <= 32:
        raise ValueError("Quality profiles must contain 1..32 named entries.")
    parsed: list[QualityProfile] = []
    for name, value in typed_profiles.items():
        if _NAME.fullmatch(name) is None or not isinstance(value, dict):
            raise ValueError("Invalid quality profile name or content.")
        options = cast(dict[str, object], value)
        if set(options) != {"checks", "require_clean"} or type(options["require_clean"]) is not bool:
            raise ValueError("Quality profile requires checks and a boolean require_clean.")
        checks = options["checks"]
        if not isinstance(checks, list):
            raise ValueError("Quality profile checks must be a list.")
        names = cast(list[object], checks)
        if not 1 <= len(names) <= 32:
            raise ValueError("Quality profile checks must contain 1..32 task names.")
        if any(not isinstance(item, str) or _NAME.fullmatch(item) is None for item in names):
            raise ValueError("Quality check names must be lowercase task identifiers.")
        named = cast(list[str], names)
        if len(set(named)) != len(named):
            raise ValueError("Duplicate checks are not supported.")
        parsed.append(QualityProfile(name, tuple(named), options["require_clean"]))
    return QualityManifest(hashlib.sha256(text.encode("utf-8")).hexdigest(), tuple(parsed))


def load_quality_manifest(root: Path) -> QualityManifest:
    file = root / ".config" / "quality.toml"
    if file.is_symlink():
        raise ValueError("Quality manifest symlinks are not supported.")
    try:
        data = file.read_bytes()
    except OSError as error:
        raise ValueError("Quality manifest is unavailable.") from error
    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("Quality manifest exceeds the 64 KiB limit.")
    try:
        return parse_quality_manifest(data.decode("utf-8"))
    except UnicodeError as error:
        raise ValueError("Quality manifest must be UTF-8.") from error
