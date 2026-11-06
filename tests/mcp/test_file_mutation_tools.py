"""Validate public file mutation schemas and request receipt behavior."""

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.mcp import failure, success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def test_creation_and_deletion_have_strict_inputs_and_replay_receipts(tmp_path: Path) -> None:
    configure(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["create_workspace_file"].annotations is not None
            assert tools["create_workspace_file"].annotations.read_only_hint is False
            assert tools["delete_workspace_file"].annotations is not None
            assert tools["delete_workspace_file"].annotations.destructive_hint is True
            invalid: list[dict[str, object]] = [
                {"path": "new.py", "content": 7, "request_id": "bad-type"},
                {"path": "new.py", "content": "", "request_id": "bad-bool", "create_parents": "true"},
                {"path": "new.py", "content": "", "request_id": "unsafe id"},
                {"path": "new.py", "content": "", "request_id": "extra", "CANARY_SECRET": "never-echo-me"},
            ]
            for arguments in invalid:
                message = await failure(client, "create_workspace_file", arguments)
                assert "Invalid tool arguments" in message
                assert "never-echo-me" not in message and "CANARY_SECRET" not in message
            assert not (tmp_path / "new.py").exists()
            created = await success(
                client, "create_workspace_file", {"path": "new.py", "content": "pass\n", "request_id": "create-1"}
            )
            assert created["outcome"] == "applied" and isinstance(created["read_ref"], str)
            arguments: dict[str, object] = {"path": "new.py", "read_ref": created["read_ref"], "request_id": "delete-1"}
            deleted = await success(client, "delete_workspace_file", arguments)
            assert deleted["outcome"] == "applied"
            (tmp_path / "new.py").write_text("user recreated this file\n")
            repeated = await success(client, "delete_workspace_file", arguments)
            assert repeated["replayed"] is True
            assert (tmp_path / "new.py").read_text() == "user recreated this file\n"
            result = await client.call_tool("delete_workspace_file", arguments)
            assert "Replayed receipt" in str(result.content)

    asyncio.run(scenario())


def test_mutation_receipts_and_read_references_are_isolated_between_servers(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
        configure(root)

    async def scenario() -> None:
        async with (
            Client(create_server(Policy.load(roots[0]))) as first,
            Client(create_server(Policy.load(roots[1]))) as second,
        ):
            original = await success(
                first, "create_workspace_file", {"path": "new.py", "content": "first\n", "request_id": "create-1"}
            )
            other = await success(
                second, "create_workspace_file", {"path": "new.py", "content": "second\n", "request_id": "create-1"}
            )
            assert original["outcome"] == other["outcome"] == "applied"
            assert original["replayed"] is other["replayed"] is False
            rejected = await success(
                second,
                "delete_workspace_file",
                {"path": "new.py", "read_ref": original["read_ref"], "request_id": "foreign-read"},
            )
            assert rejected["code"] == "read_ref_unavailable" and rejected["changed"] is False
            assert (roots[1] / "new.py").read_text() == "second\n"
            for client, created in ((first, original), (second, other)):
                deleted = await success(
                    client,
                    "delete_workspace_file",
                    {"path": "new.py", "read_ref": created["read_ref"], "request_id": "delete-1"},
                )
                assert deleted["outcome"] == "applied" and deleted["replayed"] is False

    asyncio.run(scenario())
    assert all(not (root / "new.py").exists() for root in roots)
