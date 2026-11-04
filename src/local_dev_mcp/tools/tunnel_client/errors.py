"""Tunnel startup diagnostics, distinct from external client exit status."""

from typing import Self

from ...errors import ExitCode, TunnelError


class TunnelStartupError(TunnelError):
    @classmethod
    def preflight_unconfirmed(cls) -> Self:
        return cls(
            ExitCode.GENERAL,
            "Check MCP startup",
            "local MCP server configuration",
            "The isolated startup check could not be completed; the Tunnel client was not started.",
            "Run `uv run --locked local-dev-mcp run` to inspect local startup diagnostics before connecting. "
            "No external log output was forwarded.",
        )
