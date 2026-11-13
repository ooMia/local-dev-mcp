"""MCP capability diagnostics and safe handling of unsupported multi-round elicitation."""

import asyncio
from pathlib import Path
from typing import Literal, cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.client_elicitation import classify_elicitation, diagnostic_action
from tests.support.workspace import configure

pytestmark = pytest.mark.integration

FORM: dict[str, object] = {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}}


@pytest.mark.parametrize(
    ("declared", "form", "url"),
    [
        ({"elicitation": {}}, True, False),
        ({"elicitation": {"form": {}}}, True, False),
        ({"elicitation": {"url": {}}}, False, True),
        ({"elicitation": {"form": {}, "url": {}}}, True, True),
        ({}, False, False),
        ({"elicitation": "invalid"}, False, False),
    ],
)
def test_request_capabilities_are_independently_classified(declared: dict[str, object], form: bool, url: bool) -> None:
    capabilities = classify_elicitation(
        {"elicitation": {}},
        {"io.modelcontextprotocol/clientCapabilities": declared},
        "2026-07-28",
    )
    assert capabilities["request_form"] is form
    assert capabilities["request_url"] is url
    assert capabilities["session_form"] is True
    assert capabilities["request_form_usable"] is False
    assert capabilities["protocol_version"] == "2026-07-28"


def test_legacy_mode_does_not_override_missing_per_request_capability() -> None:
    declared = classify_elicitation({"elicitation": {}}, FORM, "2025-11-25")
    assert declared["request_form_usable"] is True
    missing = classify_elicitation({"elicitation": {}}, None, "2025-11-25")
    assert missing["session_form"] is True
    assert missing["request_form_usable"] is False
    unknown_version = classify_elicitation({"elicitation": {}}, FORM, None)
    assert unknown_version["request_form_usable"] is False


@pytest.mark.parametrize(
    ("action", "confirmed"),
    [("accept", True), ("accept", False), ("decline", False), ("cancel", False)],
)
def test_accepted_declined_cancelled_outcomes_never_authorize_git(
    action: Literal["accept", "decline", "cancel"], confirmed: bool
) -> None:
    result = diagnostic_action(action, confirmed)
    assert result["action"] == action
    assert result["status"] == "responded"
    assert result["confirmed"] is (action == "accept" and confirmed)
    assert result["operation_performed"] is False
    assert result["reason"] == "none"


def test_sdk_protocol_diagnostic_blocks_unsupported_elicitation(tmp_path: Path) -> None:
    configure(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            names = {entry.name for entry in await client.list_tools()}
            assert {"diagnose_client_elicitation", "probe_client_elicitation"} <= names
            result = await client.call_tool("diagnose_client_elicitation")
            diagnosis = cast(dict[str, object], result.structured_content)
            assert diagnosis["request_form"] is False
            blocked = await client.call_tool("probe_client_elicitation")
            receipt = cast(dict[str, object], blocked.structured_content)
            assert receipt["status"] == "blocked"
            assert receipt["reason"] == "form_not_declared"
            assert receipt["operation_performed"] is False

            supported_in_metadata = await client.call_tool("diagnose_client_elicitation", meta=FORM)
            observed = cast(dict[str, object], supported_in_metadata.structured_content)
            assert observed["request_form"] is False
            assert observed["request_meta_present"] is True
            # The installed SDK negotiates the 2026 protocol, which requires InputRequiredResult.
            assert observed["request_form_usable"] is False
            blocked_multi = await client.call_tool("probe_client_elicitation", meta=FORM)
            multi = cast(dict[str, object], blocked_multi.structured_content)
            assert multi["status"] == "blocked"
            assert multi["reason"] == "form_not_declared"
            assert multi["operation_performed"] is False

    asyncio.run(scenario())
