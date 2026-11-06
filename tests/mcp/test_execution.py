import asyncio

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.mcp.registry import safe_execution

pytestmark = pytest.mark.unit


def test_safe_execution_preserves_cancellation() -> None:
    @safe_execution
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, PermissionError])
def test_safe_execution_redacts_body_failure(error_type: type[Exception]) -> None:
    @safe_execution
    async def broken() -> None:
        raise error_type("private-canary")

    with pytest.raises(ToolError) as result:
        asyncio.run(broken())
    assert error_type.__name__ in str(result.value)
    assert "private-canary" not in str(result.value)
