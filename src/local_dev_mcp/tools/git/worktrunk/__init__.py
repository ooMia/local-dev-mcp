"""Worktrunk CLI backend, separate from MCP-facing Git facades."""

from .adapter import WorktrunkAdapter

__all__ = ["WorktrunkAdapter"]
