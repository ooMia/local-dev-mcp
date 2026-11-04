"""Minimal environment shared by application startup and binary adapters."""

import os


def base_environment() -> dict[str, str]:
    return {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP") if key in os.environ
    }
