"""GitHub-specific internal adapters (no implicit MCP registration)."""

from . import pr_merge, pr_ready, pr_status

__all__ = ["pr_merge", "pr_ready", "pr_status"]
