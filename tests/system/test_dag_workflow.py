"""Exercise dependency edges through an in-process MCP server and owned Git repo."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from mcp import Client
from pydantic import TypeAdapter

from local_dev_mcp.mcp.server import TOOLS, create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.get_tool_readiness import ReadinessReport
from local_dev_mcp.tools.git.facades.get_project_status import ProjectStatus
from tests.support.git import git, initialize
from tests.support.workspace import WorkspaceConfigFactory

pytestmark = [pytest.mark.integration, pytest.mark.dag]


@pytest.fixture
def dag_workspace(workspace_config: WorkspaceConfigFactory) -> Path:
    return workspace_config(commands='["git"]')


@pytest.fixture(scope="session")
def dag_session_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("dag-session-workspace")
    return root


def test_stdio_workflow_obeys_dependencies_and_overlaps_independent_reads(
    dag_workspace: Path,
) -> None:
    root = dag_workspace
    source = b"updated through the prepared fixture\n"

    async def scenario() -> None:
        await initialize(root)
        with (root / ".git/info/exclude").open("a") as excluded:
            excluded.write("config.toml\n")
        (root / "data.txt").write_text("original\n")
        await git(root, "add", "--", "data.txt")
        await git(root, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "baseline")
        baseline = await git(root, "rev-parse", "HEAD")
        (root / "data.txt").write_bytes(source)

        policy = Policy.load(root)
        fastmcp_server = create_server(policy)
        session = Client(
            fastmcp_server._mcp_server,  # type: ignore[reportPrivateUsage]
            read_timeout_seconds=15,
            cache=None,
        )
        async with session:
            committed_head: str | None = None

            async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                result = await session.call_tool(name, arguments)
                assert not result.is_error, result.content
                assert result.structured_content is not None
                content: dict[str, object] = dict(result.structured_content)
                assert content["project"] == str(root)
                return content

            async def start() -> None:
                discovered = await session.list_tools()
                assert [tool.name for tool in discovered.tools] == [tool.name for tool in TOOLS]

            async def check_readiness() -> None:
                content = TypeAdapter(ReadinessReport).validate_python(await call("get_tool_readiness"))
                assert content["scope"] == "startup_snapshot_only"
                tools = content["tools"]
                assert len(tools) == len(TOOLS)
                for tool in tools:
                    if tool["name"] == "prepare_project_release":
                        assert tool["ready"] is False and tool["issue"] is not None
                    else:
                        assert tool["ready"] is True and tool["issue"] is None

            async def check_read_file() -> None:
                content = await call("read_workspace_file", {"path": "data.txt"})
                assert content["complete"] is True
                assert content["content"] == source.decode()
                assert content["version"] == hashlib.sha256(source).hexdigest()
                assert isinstance(content["read_ref"], str)

            async def assert_status(*, staged: bool, clean: bool = False) -> None:
                content = TypeAdapter(ProjectStatus).validate_python(await call("get_project_status"))
                assert content["head"] == (committed_head if clean else baseline)
                entries = content["entries"]
                if clean:
                    assert entries == []
                else:
                    assert len(entries) == 1
                    entry = entries[0]
                    assert entry["path"] == "data.txt"
                    assert entry["index_status"] == ("M" if staged else ".")
                    assert entry["worktree_status"] == ("." if staged else "M")

            async def check_status_before() -> None:
                await assert_status(staged=False)

            async def stage() -> None:
                content = await call("stage_project_paths", {"paths": ["data.txt"]})
                assert content["success"] is True
                assert content["paths"] == ["data.txt"]

            async def staged_diff() -> None:
                content = await call("get_project_diff", {"staged": True, "paths": ["data.txt"]})
                assert content["staged"] is True
                patch = content["patch"]
                assert isinstance(patch, str)
                assert "-original\n" in patch and "+" + source.decode() in patch

            async def staged_status() -> None:
                await assert_status(staged=True)

            async def commit() -> None:
                nonlocal committed_head
                content = await call("commit_project_changes", {"message": "DAG stdio commit"})
                assert content["success"] is True
                assert content["head_before"] == baseline
                assert content["head_observed"] is True and content["observation_error"] is None
                assert content["signing_requested"] is False
                assert content["signature_verified"] is None and content["signature_error"] is None
                head = content["head_after"]
                assert isinstance(head, str) and head != baseline
                committed_head = head

            async def head_after() -> None:
                content = await call("get_project_head")
                assert content["head"] == committed_head

            async def status_after() -> None:
                await assert_status(staged=False, clean=True)

            async def git_show() -> None:
                assert await git(root, "show", "HEAD:data.txt") == source.decode().removesuffix("\n")
                assert await git(root, "show", "-s", "--format=%s") == "DAG stdio commit"
                assert await git(root, "rev-parse", "HEAD") == committed_head
                assert await git(root, "rev-list", "--count", "HEAD") == "2"

            # Sequential execution in dependency order.
            await start()
            await check_readiness()
            await check_read_file()
            await check_status_before()
            await stage()
            await staged_diff()
            await staged_status()
            await commit()
            await head_after()
            await status_after()
            await git_show()

    asyncio.run(scenario())
