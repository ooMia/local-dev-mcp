"""GPG support; import configuration explicitly to keep the wrapper lightweight."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import configuration as configuration

__all__ = ["configuration"]
