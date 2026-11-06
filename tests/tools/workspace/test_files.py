"""Exercise file contracts through MCP, and retained observations through their internal interface."""

import asyncio
import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import TypeAdapter

import local_dev_mcp.tools.workspace._read_refs as read_refs
import local_dev_mcp.tools.workspace._reads as reads
from local_dev_mcp.mcp.server import TOOLS, create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.workspace import WorkspaceFiles
from local_dev_mcp.tools.workspace._access import ScopedPath, Workspace
from local_dev_mcp.tools.workspace._response import FileRead, Listing, PathMatches, TextMatches
from local_dev_mcp.tools.workspace.errors import WorkspaceError
from tests.support.mcp import failure, success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def server_policy(root: Path) -> Policy:
    configure(root)
    return Policy.load(root)


def test_mcp_discovery_read_and_reference_do_not_require_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = server_policy(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text('def greet():\n\treturn "Hi"\n')
    (tmp_path / "src/b.txt").write_text("HI\n")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == [tool.name for tool in TOOLS]
            for tool in tools[-4:]:
                assert tool.annotations is not None and tool.annotations.read_only_hint
                assert tool.output_schema is not None
            listed = TypeAdapter(Listing).validate_python(await success(client, "list_workspace_entries"))
            assert ".env" not in [entry.path for entry in listed.entries]
            found = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "**/*.py"})
            )
            assert found.paths == ["src/a.py"]
            paths = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "Hi"})
            )
            assert paths.paths == ["src/a.py"] and not paths.matches
            preview = TypeAdapter(TextMatches).validate_python(
                await success(
                    client, "search_workspace_text", {"query": "hi", "case_sensitive": False, "output": "content"}
                )
            )
            assert [match.path for match in preview.matches] == ["src/a.py", "src/b.txt"]
            response = await client.call_tool("read_workspace_file", {"path": "src/a.py", "start_line": 2})
            read = TypeAdapter(FileRead).validate_python(response.structured_content)
            assert read.content == '\treturn "Hi"\n'
            assert read.start_line == read.end_line == 2
            assert read.version == hashlib.sha256((tmp_path / "src/a.py").read_bytes()).hexdigest()
            assert read.read_ref is not None
            assert len(response.content) == 1
            assert "return" not in str(response.content)

    asyncio.run(scenario())


@pytest.mark.parametrize("name", [".git", ".env", ".env.local", ".env.production", ".ENV", ".GIT"])
def test_protected_names_are_absent_and_direct_access_denied(tmp_path: Path, name: str) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / name).write_text("secret-canary")
    (tmp_path / "sub/.env.example").write_text("EXAMPLE=\n")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            listed = await success(client, "list_workspace_entries", {"path": "sub"})
            parsed = TypeAdapter(Listing).validate_python(listed)
            assert f"sub/{name}" not in [entry.path for entry in parsed.entries]
            assert "protected" in await failure(client, "read_workspace_file", {"path": f"sub/{name}"})
            assert "secret-canary" not in str(await success(client, "search_workspace_text", {"query": "canary"}))
            read = await success(client, "read_workspace_file", {"path": "sub/.env.example"})
            assert read["content"] == "EXAMPLE=\n"

    asyncio.run(scenario())


def test_links_and_normalization_cannot_bypass_scope(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "safe").mkdir()
    (tmp_path / "safe/a.txt").write_text("visible")
    (tmp_path / "link").symlink_to(tmp_path / "safe", target_is_directory=True)
    (tmp_path / "secret-link").symlink_to(tmp_path / ".env")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            listed = TypeAdapter(Listing).validate_python(await success(client, "list_workspace_entries"))
            assert next(entry for entry in listed.entries if entry.path == "link").kind == "symlink"
            assert "Symlink" in await failure(client, "read_workspace_file", {"path": "link/a.txt"})
            assert "Symlink" in await failure(client, "read_workspace_file", {"path": "secret-link"})
            assert "Symlink" in await failure(client, "read_workspace_file", {"path": "link/../safe/a.txt"})
            for path in ("../outside", str(tmp_path / "safe/a.txt"), "safe/../../outside", "a\0b"):
                assert await failure(client, "read_workspace_file", {"path": path})
            read = await success(client, "read_workspace_file", {"path": "safe/../safe/a.txt"})
            assert read["path"] == "safe/a.txt"
            found = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "**/*.txt"})
            )
            assert found.paths == ["safe/a.txt"]

    asyncio.run(scenario())


def test_nested_ignore_prunes_parents_but_direct_reads_remain_available(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n!build/keep.txt\n*.log\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/keep.txt").write_text("needle")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/.gitignore").write_text("!keep.log\n")
    (tmp_path / "sub/keep.log").write_text("needle")
    (tmp_path / "sub/hide.log").write_text("needle")
    (tmp_path / "tracked.log").write_text("needle")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            found = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "**/*"})
            )
            assert "build/keep.txt" not in found.paths
            assert "sub/hide.log" not in found.paths and "tracked.log" not in found.paths
            assert "sub/keep.log" in found.paths
            assert (await success(client, "read_workspace_file", {"path": "build/keep.txt"}))["content"] == "needle"
            (tmp_path / ".gitignore").write_text("")
            found = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "**/*.txt"})
            )
            assert found.paths == ["build/keep.txt"]
            (tmp_path / "sub/.gitignore").write_bytes(b"\xff")
            assert ".gitignore" in await failure(client, "find_workspace_paths", {"pattern": "**/*"})

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [b"one\r\n\ttwo  \r\nlast", "첫 줄\n둘째 줄".encode(), b'quote " \\ \t\n'])
def test_raw_lines_hashes_and_stored_ranges_are_identical(tmp_path: Path, data: bytes) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "text").write_bytes(data)
    files = WorkspaceFiles(policy)

    async def scenario() -> None:
        read = await files.read_file("text", 1, 1)
        first = data.split(b"\n", 1)[0] + (b"\n" if b"\n" in data else b"")
        assert read.content.encode() == first
        assert read.version == hashlib.sha256(data).hexdigest()
        assert read.read_ref is not None
        record = files.references.get(read.read_ref, files.workspace.path("text"))
        assert record.content == read.content and record.version == read.version
        if read.next_start_line is not None:
            rest = await files.read_file("text", read.next_start_line, 500)
            assert (read.content + rest.content).encode() == data
        files.clear()
        with pytest.raises(WorkspaceError, match="unavailable"):
            files.references.get(read.read_ref, files.workspace.path("text"))

    asyncio.run(scenario())


def test_empty_eof_long_lines_and_skipped_files(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "binary").write_bytes(b"needle\0")
    (tmp_path / "encoding").write_bytes(b"needle\xff")
    (tmp_path / "long").write_text("x" * 17000 + "\n")
    with (tmp_path / "large").open("wb") as stream:
        stream.truncate(reads.FILE_BYTES + 1)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            empty = TypeAdapter(FileRead).validate_python(
                await success(client, "read_workspace_file", {"path": "empty"})
            )
            assert empty.content == "" and empty.read_ref is not None and empty.end_line is None
            assert empty.version == hashlib.sha256(b"").hexdigest()
            for path in ("binary", "encoding", "long", "large"):
                assert await failure(client, "read_workspace_file", {"path": path})
            search = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "needle"})
            )
            assert search.paths == []
            assert search.skipped.binary == search.skipped.encoding == search.skipped.too_large == 1
            (tmp_path / "short").write_text("first\nsecond")
            eof = TypeAdapter(FileRead).validate_python(
                await success(client, "read_workspace_file", {"path": "short", "start_line": 3})
            )
            assert eof.read_ref is None and eof.total_lines == 2 and eof.next_action is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_workspace_file", {"path": "x", "start_line": True}),
        ("read_workspace_file", {"path": "x", "max_lines": 501}),
        ("list_workspace_entries", {"offset": -1}),
        ("list_workspace_entries", {"extra": "private-canary"}),
        ("find_workspace_paths", {"pattern": "../*"}),
        ("search_workspace_text", {"query": "a\nb"}),
        ("search_workspace_text", {"query": "x", "case_sensitive": "false"}),
        ("search_workspace_text", {"query": "x", "output": "content", "limit": 101}),
    ],
)
def test_bad_arguments_are_redacted_before_file_execution(
    tmp_path: Path, tool: str, arguments: dict[str, object]
) -> None:
    policy = server_policy(tmp_path)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            error = await failure(client, tool, arguments)
            assert "private-canary" not in error
            assert "Traceback" not in error

    asyncio.run(scenario())


def test_output_limits_pagination_and_line_preview_are_explicit(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    for name in ("b.txt", "a.txt", "c.txt"):
        (tmp_path / name).write_text("한" * 500 + " needle\nneedle\n")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            first = TypeAdapter(Listing).validate_python(await success(client, "list_workspace_entries", {"limit": 1}))
            assert first.entries[0].path == "a.txt" and first.next_offset == 1 and not first.complete
            second = TypeAdapter(Listing).validate_python(
                await success(client, "list_workspace_entries", {"offset": first.next_offset, "limit": 1})
            )
            assert second.entries[0].path == "b.txt"
            search = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "needle", "output": "content", "limit": 1})
            )
            assert not search.complete and len(search.matches) == 1
            assert search.matches[0].preview_truncated
            assert len(search.matches[0].preview.encode()) <= 512
            assert len(json.dumps(search.model_dump(), ensure_ascii=False, separators=(",", ":")).encode()) <= 32768

    asyncio.run(scenario())


def test_refs_are_workspace_server_bound_expire_and_evict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "text").write_text("data")
    files = WorkspaceFiles(policy)
    clock = [0.0]
    files.references = read_refs.ReadStore(files.workspace, clock=lambda: clock[0])
    monkeypatch.setattr(read_refs, "REFERENCE_COUNT", 2)

    async def scenario() -> None:
        first = await files.read_file("text", 1, 1)
        second = await files.read_file("text", 1, 1)
        third = await files.read_file("text", 1, 1)
        assert first.read_ref is not None and second.read_ref is not None and third.read_ref is not None
        path = files.workspace.path("text")
        with pytest.raises(WorkspaceError):
            files.references.get(first.read_ref, path)
        with pytest.raises(WorkspaceError):
            WorkspaceFiles(policy).references.get(second.read_ref, path)
        other = tmp_path / "other"
        other.mkdir()
        (other / "text").write_text("data")
        with pytest.raises(WorkspaceError):
            files.references.get(second.read_ref, Workspace(Policy(other, (other,), ())).path("text"))
        assert files.references.get(second.read_ref, path).content == "data"
        clock[0] = read_refs.REFERENCE_SECONDS
        with pytest.raises(WorkspaceError):
            files.references.get(second.read_ref, path)
        assert not (tmp_path / "read_ref").exists()

    asyncio.run(scenario())


def test_path_values_are_immutable_and_revalidate_symlinks(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/text").write_text("data")
    workspace = Workspace(Policy(tmp_path, (tmp_path,), ()))
    path = workspace.path("sub/text")
    with pytest.raises(FrozenInstanceError):
        setattr(path, "relative", "outside")
    (tmp_path / "sub/text").unlink()
    (tmp_path / "sub/text").symlink_to(tmp_path / ".env")
    with pytest.raises(WorkspaceError, match="Symlink"):
        path.checked()
    with pytest.raises(WorkspaceError):
        ScopedPath(workspace, "../outside")


def test_fifo_is_listed_but_never_opened_for_content(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO fixture requires POSIX")
    policy = server_policy(tmp_path)
    os.mkfifo(tmp_path / "pipe")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            assert await failure(client, "read_workspace_file", {"path": "pipe"})
            matches = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "anything"})
            )
            assert matches.skipped.special == 1

    asyncio.run(scenario())


def test_read_change_is_rejected_before_reference_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator

    policy = server_policy(tmp_path)
    target = tmp_path / "text"
    target.write_text("before\nsecond\n")
    original = reads.TextObservation.lines

    def changing(observation: reads.TextObservation) -> Iterator[str]:
        changed = False
        for line in original(observation):
            yield line
            if not changed:
                target.write_text("after\nsecond\n")
                changed = True

    monkeypatch.setattr(reads.TextObservation, "lines", changing)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            error = await failure(client, "read_workspace_file", {"path": "text"})
            assert "changed during reading" in error
            assert "before" not in error and "after" not in error

    asyncio.run(scenario())


def test_scan_budget_and_oversized_json_are_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = server_policy(tmp_path)
    for number in range(200):
        (tmp_path / ("한" * 60 + str(number))).write_text("needle\n")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            result = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "*", "limit": 200})
            )
            assert not result.complete and result.reason == "response_limit"
            assert len(result.model_dump_json().encode()) <= 32768
            monkeypatch.setattr(reads, "SCAN_ENTRIES", 2)
            result = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "*"})
            )
            assert not result.complete and result.reason == "scan_limit"

    asyncio.run(scenario())


def test_search_io_and_encoding_failures_do_not_establish_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import local_dev_mcp.tools.workspace._discovery as discovery

    policy = server_policy(tmp_path)
    (tmp_path / "blocked").mkdir()
    (tmp_path / "blocked/hidden").write_text("needle")
    original = discovery.os.scandir

    def denied(path: str | Path) -> object:
        if Path(path).name == "blocked":
            raise PermissionError("private-error-canary")
        return original(path)

    monkeypatch.setattr(discovery.os, "scandir", denied)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            result = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "needle"})
            )
            assert not result.complete and result.skipped.io == 1 and result.paths == []
            assert "private-error-canary" not in result.model_dump_json()
            assert "Cannot access" in await failure(client, "list_workspace_entries", {"path": "blocked"})

    asyncio.run(scenario())


def test_ignore_symlink_is_not_followed_or_silently_disabled(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "rules").write_text("*.txt\n")
    (tmp_path / ".gitignore").symlink_to(tmp_path / "rules")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            assert ".gitignore" in await failure(client, "find_workspace_paths", {"pattern": "*"})

    asyncio.run(scenario())


def test_reference_byte_eviction_and_lifespan_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import local_dev_mcp.mcp.server as core

    policy = server_policy(tmp_path)
    (tmp_path / "text").write_text("abcd")
    settings = core.load_settings(policy)

    def loaded(policy: Policy) -> core.ServerSettings:
        return settings

    monkeypatch.setattr(core, "load_settings", loaded)
    monkeypatch.setattr(read_refs, "REFERENCE_BYTES", 6)

    async def scenario() -> None:
        path = settings.files.workspace.path("text")
        async with Client(core.create_server(policy)) as client:
            first = TypeAdapter(FileRead).validate_python(
                await success(client, "read_workspace_file", {"path": "text"})
            )
            second = TypeAdapter(FileRead).validate_python(
                await success(client, "read_workspace_file", {"path": "text"})
            )
            assert first.read_ref is not None and second.read_ref is not None
            with pytest.raises(WorkspaceError):
                settings.files.references.get(first.read_ref, path)
            assert settings.files.references.get(second.read_ref, path).content == "abcd"
        with pytest.raises(WorkspaceError):
            settings.files.references.get(second.read_ref, path)

    asyncio.run(scenario())


def test_worker_cancellation_does_not_issue_an_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator
    from threading import Event

    policy = server_policy(tmp_path)
    (tmp_path / "text").write_text("data\n")
    files = WorkspaceFiles(policy)
    entered = Event()
    release = Event()
    original = reads.TextObservation.lines

    def paused(observation: reads.TextObservation) -> Iterator[str]:
        import time

        entered.set()
        while not release.is_set():
            observation.budget.check()
            time.sleep(0.001)
        yield from original(observation)

    monkeypatch.setattr(reads.TextObservation, "lines", paused)

    async def scenario() -> None:
        import anyio

        async with anyio.create_task_group() as group:
            group.start_soon(files.read_file, "text", 1, 1)
            while not entered.is_set():
                await anyio.sleep(0.001)
            group.cancel_scope.cancel()

    asyncio.run(scenario())


def test_nested_ignore_matches_git_for_supported_patterns(tmp_path: Path) -> None:
    from shellous import sh

    from local_dev_mcp.tools.git import command_environment
    from tests.support.git import git

    policy = server_policy(tmp_path)
    patterns = "build/\n!build/keep.txt\n*.log\n/root-only.txt\ncache/**\n!cache/keep/\n"
    (tmp_path / ".gitignore").write_text(patterns)
    paths = ["build/keep.txt", "sub/keep.log", "sub/drop.log", "root-only.txt", "sub/root-only.txt", "cache/x"]
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text("data")
    (tmp_path / "sub/.gitignore").write_text("!keep.log\n")

    async def scenario() -> None:
        await git(tmp_path, "init", "--quiet")
        async with Client(create_server(policy)) as client:
            found = TypeAdapter(PathMatches).validate_python(
                await success(client, "find_workspace_paths", {"pattern": "**/*"})
            )
            for path in paths:
                checked = await sh.result("git", "-C", str(tmp_path), "check-ignore", "--no-index", "-q", path).set(
                    inherit_env=False, env=command_environment(), timeout=10
                )
                assert checked.exit_code in (0, 1)
                assert (path not in found.paths) is (checked.exit_code == 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["SCAN_BYTES", "SCAN_SECONDS"])
def test_search_budget_exhaustion_is_not_a_complete_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "aaa.txt").write_text("needle" * 100)
    monkeypatch.setattr(reads, budget, 1 if budget == "SCAN_BYTES" else 0)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            result = TypeAdapter(TextMatches).validate_python(
                await success(client, "search_workspace_text", {"query": "needle"})
            )
            assert result.paths == [] and not result.complete
            assert result.reason == ("scan_limit" if budget == "SCAN_BYTES" else "deadline")

    asyncio.run(scenario())


def test_read_body_bound_continues_at_a_complete_line(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    original = ("entry\t" + "a" * 90 + "\r\n") * 300
    (tmp_path / "text").write_bytes(original.encode())

    async def scenario() -> None:
        files = WorkspaceFiles(policy)
        first = await files.read_file("text", 1, 500)
        assert not first.complete and first.next_start_line is not None
        assert len(first.content.encode()) <= 16384
        assert first.content.endswith("\r\n") and first.read_ref is not None
        second = await files.read_file("text", first.next_start_line, 500)
        assert first.content + second.content == original
        assert first.version == second.version
        (tmp_path / "escaped").write_bytes(b"\x01" * 6000 + b"\n")
        with pytest.raises(WorkspaceError, match="complete requested line"):
            await files.read_file("escaped", 1, 1)

    asyncio.run(scenario())


def test_parallel_reads_have_distinct_immutable_references(tmp_path: Path) -> None:
    policy = server_policy(tmp_path)
    (tmp_path / "text").write_text("one\ntwo\nthree\n")
    files = WorkspaceFiles(policy)

    async def scenario() -> None:
        results = await asyncio.gather(*(files.read_file("text", number % 3 + 1, 1) for number in range(12)))
        assert len({result.read_ref for result in results}) == 12
        for result in results:
            assert result.read_ref is not None
            record = files.references.get(result.read_ref, files.workspace.path("text"))
            assert record.content == result.content and record.start_line == result.start_line

    asyncio.run(scenario())
