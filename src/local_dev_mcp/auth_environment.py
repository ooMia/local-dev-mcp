"""Non-secret host account locations shared across isolated GitHub CLI processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_AUTH_LOCATIONS = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "GH_CONFIG_DIR",
)


def os_account_home() -> str | None:
    """Get the operating-system account's home without trusting a stripped HOME."""
    if os.name == "posix":
        try:
            import pwd

            value = pwd.getpwuid(os.geteuid()).pw_dir
            return value if Path(value).is_absolute() else None
        except ImportError, KeyError, OSError:
            pass
    if os.name == "nt":
        value = os.environ.get("USERPROFILE")
        return value if value and Path(value).is_absolute() else None
    return None


def account_locations(values: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve explicit host paths; restore a missing HOME from the OS account.

    A non-absolute path is deliberately not propagated: gh would otherwise
    create configuration or state in the workspace's current directory.
    """
    source = os.environ if values is None else values
    selected = {key: source[key] for key in _AUTH_LOCATIONS if source.get(key) and Path(source[key]).is_absolute()}
    if "HOME" not in selected:
        account = os_account_home()
        if account is not None:
            selected["HOME"] = account
    return selected
