"""Strict public lifecycle schemas and independently permissioned Ready writes."""

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


def test_chat_status_and_ready_use_reviewed_head_without_duplicate_writes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr: dict[str, Any] = {
        "url": "https://github.com/owner/project/pull/6",
        "title": "feat: lifecycle",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "state": "OPEN",
        "isDraft": True,
        "mergeCommit": None,
    }
    writes: list[tuple[str, ...]] = []

    async def gh(self: Remote, *args: str, write: bool = False, input_text: str | None = None) -> Result:
        if args[:2] == ("repo", "view"):
            return Result(0, '{"url":"https://github.com/owner/project"}')
        if args[:2] == ("pr", "view"):
            return Result(0, json.dumps(pr))
        if args[:2] == ("pr", "ready"):
            assert write and args[-1] == "https://github.com/owner/project"
            writes.append(args)
            pr["isDraft"] = False
            return Result(0, "")
        assert not write and input_text is not None
        assert json.loads(input_text)["variables"]["number"] == 6
        return Result(
            0,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                **pr,
                                "mergeable": "MERGEABLE",
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": HEAD,
                                                "statusCheckRollup": {
                                                    "state": "SUCCESS",
                                                    "contexts": {
                                                        "totalCount": 1,
                                                        "pageInfo": {"hasNextPage": False},
                                                        "nodes": [
                                                            {
                                                                "__typename": "CheckRun",
                                                                "name": "Project quality",
                                                                "status": "COMPLETED",
                                                                "conclusion": "SUCCESS",
                                                                "detailsUrl": None,
                                                                "checkSuite": {"app": {"slug": "github-actions"}},
                                                            }
                                                        ],
                                                    },
                                                },
                                            }
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            ),
        )

    monkeypatch.setattr(Remote, "gh", gh)

    async def scenario() -> None:
        await workspace_factory()
        await git(tmp_path, "remote", "add", "origin", "https://github.com/owner/project.git")
        policy = Policy(tmp_path, (tmp_path,), ("git", "gh", "git.remote.read", "gh.pr.ready"))
        async with Client(create_server(policy)) as client:
            declared = {tool.name: tool for tool in await client.list_tools()}
            assert {"get_project_pr_status", "mark_project_pr_ready"} <= declared.keys()
            assert set(declared["get_project_pr_status"].input_schema["required"]) == {"number", "expected_head"}
            assert declared["mark_project_pr_ready"].input_schema["additionalProperties"] is False
            status = await client.call_tool("get_project_pr_status", {"number": 6, "expected_head": HEAD})
            data = status.structured_content
            assert data is not None and data["quality_state"] == "success" and data["complete"]
            args = {"number": 6, "expected_head": HEAD, "expected_plan_digest": data["plan_digest"]}
            refused = await client.call_tool(
                "mark_project_pr_ready", {**args, "expected_head": "b" * 40}, raise_on_error=False
            )
            assert refused.is_error and not writes
            result = await client.call_tool("mark_project_pr_ready", args)
            assert result.structured_content is not None and result.structured_content["status"] == "ready_verified"
            again = await client.call_tool("mark_project_pr_ready", args)
            assert again.structured_content is not None and not again.structured_content["write_requested"]
            assert len(writes) == 1

    asyncio.run(scenario())


def test_ready_permission_is_not_implied_by_merge(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        async with Client(create_server(Policy(tmp_path, (tmp_path,), ("git", "gh", "gh.pr.merge")))) as client:
            result = await client.call_tool(
                "mark_project_pr_ready",
                {
                    "number": 6,
                    "expected_head": HEAD,
                    "expected_plan_digest": "0" * 64,
                },
                raise_on_error=False,
            )
            assert result.is_error
            assert "gh.pr.ready" in str(result.content)

    asyncio.run(scenario())
