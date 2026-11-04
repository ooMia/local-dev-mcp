"""Shared tool support; import registry explicitly when MCP registration is needed."""

from typing import TYPE_CHECKING

from ._execution import executable

if TYPE_CHECKING:
    from . import registry as registry

__all__ = ["executable", "registry"]
