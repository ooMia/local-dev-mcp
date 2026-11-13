"""2026-07-28 MRTR diagnostic is a no-op, not Git approval."""

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fastmcp import Client
from mcp.types import ElicitRequestFormParams, ElicitResult, InputRequiredResult

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.client_elicitation import (
    modern_diagnostic_request,
    modern_diagnostic_response,
)
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def test_mrtr_challenge_is_only_a_form_request() -> None:
    request = modern_diagnostic_request()
    assert isinstance(request, InputRequiredResult)
    assert request.result_type == "input_required"
    inputs = request.input_requests
    assert inputs is not None
    assert set(inputs) == {"diagnostic"}
    assert request.request_state == "mrtr-diagnostic-v1"
    params = inputs["diagnostic"].params
    assert isinstance(params, ElicitRequestFormParams)
    assert params.message.startswith("진단")


@pytest.mark.parametrize(
    ("answer", "confirmed"),
    [
        (ElicitResult(action="accept", content={"confirmed": True}), True),
        (ElicitResult(action="accept", content={"confirmed": False}), False),
        (ElicitResult(action="decline"), False),
        (ElicitResult(action="cancel"), False),
    ],
)
def test_mrtr_answers_do_not_perform_git_operations(answer: ElicitResult, confirmed: bool) -> None:
    response = modern_diagnostic_response({"diagnostic": answer}, "mrtr-diagnostic-v1")
    assert response["status"] == "responded"
    assert response["confirmed"] is confirmed
    assert response["operation_performed"] is False


def test_mrtr_forged_state_is_rejected() -> None:
    response = modern_diagnostic_response(
        {"diagnostic": ElicitResult(action="accept", content={"confirmed": True})},
        "untrusted",
    )
    assert response["status"] == "blocked"
    assert response["confirmed"] is False


def test_mrtr_tool_blocks_missing_form_capability(tmp_path: Path) -> None:
    configure(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            names = {item.name for item in await client.list_tools()}
            assert "probe_client_elicitation_mrtr" in names
            result = await client.call_tool("probe_client_elicitation_mrtr")
            receipt = cast(dict[str, object], result.structured_content)
            assert receipt["status"] == "blocked"
            assert receipt["operation_performed"] is False

    asyncio.run(scenario())


def test_mrtr_round_trip_with_in_process_form_capability(tmp_path: Path) -> None:
    configure(tmp_path)
    asked: list[str] = []

    async def handler(message: str, response_type: object, params: object, context: object) -> ElicitResult:
        asked.append(message)
        return ElicitResult(action="accept", content={"confirmed": True})

    async def scenario() -> None:
        from local_dev_mcp.tools.client_elicitation import ElicitationCapabilities

        declared: ElicitationCapabilities = {
            "protocol_version": "2026-07-28",
            "request_meta_present": True,
            "request_form": True,
            "request_url": False,
            "request_capabilities_kind": "dict",
            "request_elicitation_kind": "dict",
            "request_elicitation_fields": ["form"],
            "session_form": True,
            "session_available": True,
            "request_form_usable": False,
            "next_action": "MRTR supported",
        }
        with patch("local_dev_mcp.tools.client_elicitation._observe", return_value=declared):
            async with Client(create_server(Policy.load(tmp_path)), elicitation_handler=handler) as client:
                result = await client.call_tool("probe_client_elicitation_mrtr")
                receipt = cast(dict[str, object], result.structured_content)
                assert receipt["status"] == "responded"
                assert receipt["confirmed"] is True
                assert receipt["operation_performed"] is False
                assert len(asked) == 1

    asyncio.run(scenario())
