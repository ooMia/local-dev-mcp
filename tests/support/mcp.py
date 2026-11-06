"""Result assertions for the in-process FastMCP test client."""

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent


async def success(
    session: Client[FastMCPTransport], name: str, arguments: dict[str, object] | None = None
) -> dict[str, object]:
    result = await session.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


async def failure(session: Client[FastMCPTransport], name: str, arguments: dict[str, object] | None = None) -> str:
    result = await session.call_tool(name, arguments, raise_on_error=False)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text
