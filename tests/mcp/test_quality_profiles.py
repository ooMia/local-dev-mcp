"""Quality profiles are discoverable over MCP without duplicating task recipes."""

import asyncio
import json
import sys
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.tasks.list_project_tasks import QualityProfileInfo
from tests.support.mcp import success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def _workspace(root: Path, *, allowed: bool = True, valid: bool = True) -> None:
    configure(root, commands='["task.quality"]' if allowed else "[]")
    with (root / "config.toml").open("a") as config:
        config.write(
            "[jobs.check]\nkind='lint'\npermission='task.quality'\nargv="
            + json.dumps([sys.executable, "-c", 'print("checked")'])
            + "\n"
        )
    (root / ".config").mkdir()
    (root / ".config" / "quality.toml").write_text(
        'schema_version = 1\n[profiles.pre_merge]\nchecks = ["check"]\nrequire_clean = true\n'
        if valid
        else "schema_version = 2\n"
    )


def test_shared_profile_routes_to_existing_mcp_task(tmp_path: Path) -> None:
    _workspace(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            catalogue = await success(client, "list_project_tasks")
            assert catalogue["profiles_status"] == "available"
            profile = cast(list[QualityProfileInfo], catalogue["profiles"])[0]
            assert profile["name"] == "pre_merge"
            assert profile["checks"] == ["check"]
            assert profile["ready"] is True
            assert len(profile["manifest_digest"]) == 64
            result = await success(client, "start_project_task", {"task": "check", "request_id": "quality-mcp"})
            assert result["state"] == "succeeded" and result["exit_code"] == 0
            assert result["task"] == profile["checks"][0]

    asyncio.run(scenario())


def test_unavailable_recipe_does_not_turn_into_a_pass(tmp_path: Path) -> None:
    _workspace(tmp_path, allowed=False)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            result = await success(client, "list_project_tasks")
            assert result["profiles_status"] == "available"
            profile = cast(list[QualityProfileInfo], result["profiles"])[0]
            assert profile["ready"] is False
            assert profile["reason"] == "registered_check_unavailable"

    asyncio.run(scenario())


def test_invalid_and_absent_profiles_are_distinct(tmp_path: Path) -> None:
    configure(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            missing = await success(client, "list_project_tasks")
            assert missing["profiles_status"] == "absent"
            assert missing["profiles"] == []

    asyncio.run(scenario())
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "quality.toml").write_text("schema_version = 2\n")

    async def invalid() -> None:
        async with Client(create_server(Policy.load(tmp_path))) as client:
            result = await success(client, "list_project_tasks")
            assert result["profiles_status"] == "invalid"
            assert result["profiles"] == []

    asyncio.run(invalid())
