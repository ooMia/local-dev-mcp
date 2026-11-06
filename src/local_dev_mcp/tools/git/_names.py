"""Shared branch grammar for commands and facades, including stable release names."""

import re

_TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9-]*)*")
_RELEASE = re.compile(r"release/(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def branch_name_allowed(value: str) -> bool:
    return len(value) <= 128 and (_TOPIC.fullmatch(value) is not None or _RELEASE.fullmatch(value) is not None)
