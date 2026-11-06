"""Public merge tools keep permission, review and result boundaries through MCP."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git._remote import Remote, Result
from tests.support.git import WorkspaceFactory, git

pytestmark = pytest.mark.integration
HEAD = "a" * 40
MERGED = "b" * 40


def test_public_merge_plan_review_write_and_recovery(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[dict[str, object]] = []

    async def gh(self: Remote, *args: str, write: bool = False, input_text: str | None = None) -> Result:
        if args[:2] == ("repo", "view"):
            return Result(0, '{"url":"https://github.com/owner/project"}')
        if args[:2] == ("pr", "view"):
            return Result(
                0,
                json.dumps(
                    {
                        "url": "https://github.com/owner/project/pull/12",
                        "title": "fix: provenance",
                        "headRefOid": HEAD,
                        "baseRefName": "main",
                        "state": "MERGED" if writes else "OPEN",
                        "isDraft": False,
                        "mergeCommit": {"oid": MERGED} if writes else None,
                    }
                ),
            )
        if write:
            assert args == (
                "api",
                "--hostname",
                "github.com",
                "repos/owner/project/pulls/12/merge",
                "--method",
                "PUT",
                "--input",
                "-",
            )
            assert input_text is not None
            writes.append(json.loads(input_text))
            return Result(0, '{"merged":true}')
        assert args[-1] == "repos/owner/project/git/commits/" + MERGED
        return Result(
            0,
            json.dumps(
                {
                    "sha": MERGED,
                    "parents": [{"sha": "c" * 40}],
                    "message": "fix: provenance (#12)\n\n"
                    "Source-PR: https://github.com/owner/project/pull/12\nSource-Head: " + HEAD,
                }
            ),
        )

    monkeypatch.setattr(Remote, "gh", gh)

    async def scenario() -> None:
        await workspace_factory()
        await git(tmp_path, "remote", "add", "origin", "https://github.com/owner/project.git")
        server = create_server(Policy(tmp_path, (tmp_path,), ("git", "gh", "git.remote.read", "gh.pr.merge")))
        async with Client(server) as client:
            declared = {tool.name: tool for tool in await client.list_tools()}
            assert {"get_project_pr_merge_plan", "merge_project_pr"} <= declared.keys()
            schema = declared["merge_project_pr"].input_schema
            assert set(schema["required"]) == {"number", "expected_head", "expected_plan_digest"}
            assert schema["additionalProperties"] is False
            preview = await client.call_tool("get_project_pr_merge_plan", {"number": 12})
            plan: dict[str, Any] = preview.structured_content or {}
            assert plan["head"] == HEAD
            assert plan["subject"] == "fix: provenance (#12)"
            arguments = {"number": 12, "expected_head": HEAD, "expected_plan_digest": plan["plan_digest"]}
            for invalid in (
                {**arguments, "number": True},
                {**arguments, "body": "forged"},
                {**arguments, "expected_plan_digest": "0" * 64},
            ):
                result = await client.call_tool("merge_project_pr", invalid, raise_on_error=False)
                assert result.is_error
            assert not writes
            merged = await client.call_tool("merge_project_pr", arguments)
            assert merged.structured_content is not None
            assert merged.structured_content["status"] == "merged_verified"
            recovered = await client.call_tool("merge_project_pr", arguments)
            assert recovered.structured_content is not None
            assert recovered.structured_content["write_requested"] is False
            assert len(writes) == 1

    asyncio.run(scenario())


def test_new_merge_permission_is_not_implied_by_publication(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        server = create_server(Policy(tmp_path, (tmp_path,), ("git", "gh", "git.remote.write", "gh.pr.create")))
        async with Client(server) as client:
            response = await client.call_tool(
                "merge_project_pr",
                {
                    "number": 12,
                    "expected_head": HEAD,
                    "expected_plan_digest": "0" * 64,
                },
                raise_on_error=False,
            )
            assert response.is_error
            assert "gh.pr.merge" in str(response.content)

    asyncio.run(scenario())
