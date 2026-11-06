"""Derive a project-wide task environment without forwarding server configuration."""

import re
from collections.abc import Mapping
from typing import Final

from ..execution import base_environment

SERVER_ENVIRONMENT_NAMES: Final = frozenset(
    {
        "CONTROL_PLANE_TUNNEL_ID",
        "CONTROL_PLANE_API_KEY",
        "GIT_USER_NAME",
        "GIT_USER_EMAIL",
        "GIT_COMMIT_SIGN",
        "GIT_SIGNING_KEY",
        "GIT_GPG_HOME",
        "GIT_GPG_PROGRAM",
    }
)


def task_environment(values: Mapping[str, str | None]) -> dict[str, str]:
    return {
        **base_environment(),
        **{name: value for name, value in values.items() if value is not None and name not in SERVER_ENVIRONMENT_NAMES},
    }


def public_environment_names(values: Mapping[str, str | None], names: tuple[str, ...]) -> tuple[str, ...]:
    # A project can classify its own operational values, not server credentials.
    public = [name for name in names if name not in SERVER_ENVIRONMENT_NAMES]
    for name in ("GIT_USER_NAME", "GIT_USER_EMAIL"):
        value = values.get(name)
        if value and value == value.strip() and not any(character in value for character in "\0\r\n<>"):
            public.append(name)
    if values.get("GIT_COMMIT_SIGN") in ("true", "false"):
        public.append("GIT_COMMIT_SIGN")
    fingerprint = values.get("GIT_SIGNING_KEY")
    # An unvalidated setting is not automatically a public fingerprint.
    if fingerprint and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})!?", fingerprint):
        public.append("GIT_SIGNING_KEY")
    return tuple(public)
