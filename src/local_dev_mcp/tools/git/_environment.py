"""Restricted environment for Git child processes."""

import os

from ...execution import base_environment


def command_environment() -> dict[str, str]:
    return {
        **base_environment(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
