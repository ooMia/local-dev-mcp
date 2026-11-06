"""Resolve executable dependencies shared by binary adapters."""

import os
import shutil
from pathlib import Path

from ..errors import DependencyError


def executable(command: str) -> Path:
    found = shutil.which(command)
    if found is None:
        raise DependencyError.unavailable(command)
    try:
        path = Path(found).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DependencyError.unavailable(command)
    except OSError, ValueError:
        raise DependencyError.unavailable(command) from None
    return path
