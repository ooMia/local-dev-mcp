"""Exercise dependency edges through an actual stdio server and owned Git repo."""

import asyncio
import hashlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import TypeAdapter

from local_dev_mcp.mcp.server import TOOLS
from local_dev_mcp.tools.get_tool_readiness import ReadinessReport
from local_dev_mcp.tools.git.facades.get_project_status import ProjectStatus
from tests.support.dag import Graph, Node, RunResult, Status
from tests.support.git import git, initialize
from tests.support.workspace import WorkspaceConfigFactory

pytestmark = [pytest.mark.integration, pytest.mark.dag]


@pytest.fixture
def dag_workspace(tmp_path: Path, workspace_config: WorkspaceConfigFactory) -> Path:
    return workspace_config(commands='["git"]')


def test_stdio_workflow_obeys_dependencies_and_overlaps_independent_reads(
    dag_workspace: Path,
    dag_selection: tuple[str, ...] | None,
    dag_workers: int,
    run_dag: Callable[[Graph], Awaitable[RunResult]],
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

        session = Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-I", "-m", "local_dev_mcp", "run"],
                cwd=root,
            ),
            read_timeout_seconds=15,
            cache=None,
        )
        # The SDK's cancel scopes must enter and exit in this owning task.
        async with session:
            committed_head: str | None = None
            initial_reads = {"readiness", "read_file", "status_before"}
            expected_reads: set[str] = set()
            entered_reads: set[str] = set()
            all_reads_entered = asyncio.Event()

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

            async def ready_read(node_id: str, action: Callable[[], Awaitable[None]]) -> None:
                entered_reads.add(node_id)
                if len(entered_reads) >= min(len(expected_reads), dag_workers):
                    all_reads_entered.set()
                # Ready reads share the configured capacity, including a serial run.
                async with asyncio.timeout(5):
                    await all_reads_entered.wait()
                await action()

            async def check_readiness() -> None:
                content = TypeAdapter(ReadinessReport).validate_python(await call("get_tool_readiness"))
                assert content["scope"] == "startup_snapshot_only"
                tools = content["tools"]
                assert len(tools) == len(TOOLS)
                for tool in tools:
                    assert tool["ready"] is True and tool["issue"] is None

            async def readiness() -> None:
                await ready_read("readiness", check_readiness)

            async def check_read_file() -> None:
                content = await call("read_workspace_file", {"path": "data.txt"})
                assert content["complete"] is True
                assert content["content"] == source.decode()
                assert content["version"] == hashlib.sha256(source).hexdigest()
                assert isinstance(content["read_ref"], str)

            async def read_file() -> None:
                await ready_read("read_file", check_read_file)

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

            async def status_before() -> None:
                await ready_read("status_before", check_status_before)

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

            graph = Graph(
                (
                    Node("start", start),
                    Node("readiness", readiness, ("start",)),
                    Node("read_file", read_file, ("start",)),
                    Node("status_before", status_before, ("start",)),
                    Node("stage", stage, ("readiness", "read_file", "status_before")),
                    Node("staged_diff", staged_diff, ("stage",)),
                    Node("staged_status", staged_status, ("stage",)),
                    Node("commit", commit, ("staged_diff", "staged_status")),
                    Node("head_after", head_after, ("commit",)),
                    Node("status_after", status_after, ("commit",)),
                    Node("git_show", git_show, ("commit",)),
                )
            )
            expected_reads = initial_reads.intersection(graph.plan(dag_selection).included)
            report = await run_dag(graph)
            assert report.success, report.to_json()
            assert all(result.status is Status.PASS for result in report.results)

    asyncio.run(scenario())
