"""Exercise observed-range edits and recovery through the public MCP boundary."""

import asyncio
import hashlib
import stat
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from tests.support.mcp import failure, success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def configured(root: Path, *, paths: str = '["."]', extra: str = "") -> Policy:
    configure(root)
    (root / "config.toml").write_text(
        f"[filesystem]\nallowed_paths={paths}\n{extra}[system]\nallowed_commands=[]\n", encoding="utf-8"
    )
    return Policy.load(root)


def reference(result: dict[str, object]) -> str:
    value = result["read_ref"]
    assert isinstance(value, str) and value
    return value


async def replace(client: Client[FastMCPTransport], path: str, read_ref: str, replacement: str) -> dict[str, object]:
    return await success(
        client,
        "replace_workspace_file",
        {"path": path, "read_ref": read_ref, "replacement": replacement},
    )


def assert_recovery(result: dict[str, object]) -> None:
    assert isinstance(result["reason"], str) and result["reason"]
    assert isinstance(result["next_action"], str) and result["next_action"]
    next_call = TypeAdapter(dict[str, object]).validate_python(result["next_call"])
    assert next_call.get("tool") in ("read_workspace_file", "search_workspace_text", "find_workspace_paths")
    assert isinstance(next_call.get("arguments"), dict)


def test_partial_edit_preserves_other_bytes_mode_and_continues_with_new_reference(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_bytes("before\r\n\told 한글\r\nafter".encode())
    target.chmod(0o751)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            tool = next(tool for tool in await client.list_tools() if tool.name == "replace_workspace_file")
            assert tool.input_schema["additionalProperties"] is False
            assert set(tool.input_schema["required"]) == {"path", "read_ref", "replacement"}
            assert set(tool.input_schema["properties"]) == {"path", "read_ref", "replacement"}
            assert tool.output_schema is not None
            assert tool.annotations is not None and tool.annotations.read_only_hint is False
            first = await success(
                client, "read_workspace_file", {"path": "source.txt", "start_line": 2, "max_lines": 1}
            )
            first_ref = reference(first)
            response = await client.call_tool(
                "replace_workspace_file",
                {"path": "source.txt", "read_ref": first_ref, "replacement": "\t새 값 😀\r\n"},
            )
            assert not response.is_error, response.content
            assert response.structured_content is not None
            edited: dict[str, object] = dict(response.structured_content)
            assert edited["outcome"] == "applied" and edited["changed"] is True
            assert edited["version_before"] == first["version"]
            assert edited["version_after"] == hashlib.sha256(target.read_bytes()).hexdigest()
            assert edited["start_line"] == edited["end_line"] == 2
            assert edited["next_call"] is None
            assert edited["content"] == "\t새 값 😀\r\n"
            assert "applied" in str(response.content)
            assert "새 값" not in str(response.content)
            assert reference(edited) != first_ref
            assert target.read_bytes() == "before\r\n\t새 값 😀\r\nafter".encode()
            assert stat.S_IMODE(target.stat().st_mode) == original_mode

            second = await replace(client, "source.txt", reference(edited), "\tfinal\r\n")
            assert second["outcome"] == "applied"
            assert second["version_before"] == edited["version_after"]
            assert reference(second)
            assert target.read_bytes() == b"before\r\n\tfinal\r\nafter"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("original", "replacement"),
    [(b"", "first\n"), (b"one\n", ""), (b"without final newline", "changed without final newline")],
)
def test_existing_empty_and_no_final_newline_files_are_editable(
    tmp_path: Path, original: bytes, replacement: str
) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_bytes(original)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "source.txt"})
            result = await replace(client, "source.txt", reference(observed), replacement)
            assert result["outcome"] == "applied"
            assert target.read_bytes() == replacement.encode()
            assert reference(result)
            assert result["version_after"] == hashlib.sha256(replacement.encode()).hexdigest()

    asyncio.run(scenario())


def test_identical_replacement_is_a_successful_noop(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_bytes(b"keep\n")
    before = target.stat()

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "source.txt"})
            result = await replace(client, "source.txt", reference(observed), "keep\n")
            assert result["outcome"] == "unchanged" and result["changed"] is False
            assert result["version_before"] == result["version_after"] == observed["version"]
            assert reference(result)
            assert target.read_bytes() == b"keep\n"
            assert target.stat().st_ino == before.st_ino

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("user_content", "current_line", "replanned"),
    [
        ("user header\nagent A\nfooter\n", 2, "agent B\n"),
        ("header\nuser replacement\nfooter\n", 2, "user replacement plus agent B\n"),
        ("inserted by user\nheader\nagent A\nfooter\n", 3, "agent B\n"),
    ],
)
def test_user_edits_are_preserved_and_work_resumes_from_a_fresh_observation(
    tmp_path: Path, user_content: str, current_line: int, replanned: str
) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("header\nold\nfooter\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            original = await success(
                client, "read_workspace_file", {"path": "source.txt", "start_line": 2, "max_lines": 1}
            )
            edited = await replace(client, "source.txt", reference(original), "agent A\n")
            assert edited["outcome"] == "applied"
            target.write_text(user_content, encoding="utf-8")

            stale_response = await client.call_tool(
                "replace_workspace_file",
                {"path": "source.txt", "read_ref": reference(edited), "replacement": "agent B\n"},
            )
            assert not stale_response.is_error and stale_response.structured_content is not None
            stale: dict[str, object] = dict(stale_response.structured_content)
            assert "stale_version" in str(stale_response.content)
            assert "Read the current file" in str(stale_response.content)
            assert stale["outcome"] == "rejected" and stale["code"] == "stale_version"
            assert stale["changed"] is False
            assert target.read_text(encoding="utf-8") == user_content
            assert_recovery(stale)

            fresh = await success(
                client,
                "read_workspace_file",
                {"path": "source.txt", "start_line": current_line, "max_lines": 1},
            )
            assert fresh["content"] == user_content.splitlines(keepends=True)[current_line - 1]
            resumed = await replace(client, "source.txt", reference(fresh), replanned)
            assert resumed["outcome"] == "applied"
            expected = user_content.splitlines(keepends=True)
            expected[current_line - 1] = replanned
            assert target.read_text(encoding="utf-8") == "".join(expected)

    asyncio.run(scenario())


def test_concurrent_edits_from_one_reference_only_apply_once(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("old\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "source.txt"})
            read_ref = reference(observed)
            ready = asyncio.Barrier(2)

            async def contender(content: str) -> dict[str, object]:
                await ready.wait()
                return await replace(client, "source.txt", read_ref, content)

            results = await asyncio.gather(contender("first\n"), contender("second\n"))
            assert sorted(str(result["outcome"]) for result in results) == ["applied", "rejected"]
            winner = next(index for index, result in enumerate(results) if result["outcome"] == "applied")
            assert target.read_text(encoding="utf-8") == ("first\n", "second\n")[winner]
            rejected = results[1 - winner]
            assert rejected["code"] == "stale_version" and rejected["changed"] is False
            assert_recovery(rejected)

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["config.toml", ".gitignore", "vendor/keep.txt", "cache.log", "docs/locked.txt"])
def test_readonly_ignored_and_policy_files_cannot_be_directly_replaced(tmp_path: Path, path: str) -> None:
    policy = configured(tmp_path, extra='read_only_paths=["docs"]\n')
    (tmp_path / "vendor").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / ".gitignore").write_text("vendor/\n*.log\n", encoding="utf-8")
    for name in ("vendor/keep.txt", "cache.log", "docs/locked.txt"):
        (tmp_path / name).write_text("original\n", encoding="utf-8")
    target = tmp_path / path
    original = target.read_bytes()

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": path})
            result = await replace(client, path, reference(observed), "must not be written\n")
            assert result["outcome"] == "rejected" and result["changed"] is False
            assert isinstance(result["code"], str) and result["code"]
            assert isinstance(result["next_action"], str) and result["next_action"]
            assert target.read_bytes() == original

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("path", "outcome"), [("build/keep.txt", "rejected"), ("sub/hide.log", "rejected"), ("sub/keep.log", "applied")]
)
def test_write_permission_uses_effective_nested_ignore_rules(tmp_path: Path, path: str, outcome: str) -> None:
    policy = configured(tmp_path)
    for directory in ("build", "sub"):
        (tmp_path / directory).mkdir()
    (tmp_path / ".gitignore").write_text("build/\n!build/keep.txt\n*.log\n", encoding="utf-8")
    (tmp_path / "sub/.gitignore").write_text("!keep.log\n", encoding="utf-8")
    target = tmp_path / path
    target.write_text("old\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": path})
            result = await replace(client, path, reference(observed), "new\n")
            assert result["outcome"] == outcome
            assert target.read_text(encoding="utf-8") == ("new\n" if outcome == "applied" else "old\n")

    asyncio.run(scenario())


def test_subdirectory_permissions_allow_useful_navigation_without_exposing_denied_files(tmp_path: Path) -> None:
    policy = configured(
        tmp_path,
        paths='["src", "docs"]',
        extra='read_only_paths=["docs"]\ndenied_paths=["src/private"]\n',
    )
    for directory in ("src/private", "docs", "other"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "src/main.py").write_text("old\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("other/\n", encoding="utf-8")
    (tmp_path / "src/private/credentials.json").write_text("test-private-canary", encoding="utf-8")
    (tmp_path / "docs/readme.txt").write_text("documentation\n", encoding="utf-8")
    (tmp_path / "other/file.txt").write_text("outside-allowed-canary", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            listing = await success(client, "list_workspace_entries")
            entries = TypeAdapter(list[dict[str, object]]).validate_python(listing["entries"])
            entry_paths = TypeAdapter(list[str]).validate_python([entry["path"] for entry in entries])
            assert sorted(entry_paths) == ["docs", "src"]
            src = await success(client, "list_workspace_entries", {"path": "src"})
            src_entries = TypeAdapter(list[dict[str, object]]).validate_python(src["entries"])
            assert [entry["path"] for entry in src_entries] == ["src/main.py"]
            assert "test-private-canary" not in await failure(
                client, "read_workspace_file", {"path": "src/private/credentials.json"}
            )
            assert "outside-allowed-canary" not in await failure(
                client, "read_workspace_file", {"path": "other/file.txt"}
            )
            observed = await success(client, "read_workspace_file", {"path": "src/main.py"})
            edited = await replace(client, "src/main.py", reference(observed), "new\n")
            assert edited["outcome"] == "applied"
            docs = await success(client, "read_workspace_file", {"path": "docs/readme.txt"})
            denied = await replace(client, "docs/readme.txt", reference(docs), "changed\n")
            assert denied["outcome"] == "rejected" and denied["changed"] is False

    asyncio.run(scenario())


def test_new_server_rejects_old_reference_but_fresh_read_allows_resuming(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("old\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as first:
            previous = await success(first, "read_workspace_file", {"path": "source.txt"})
            previous_ref = reference(previous)
        async with Client(create_server(policy)) as restarted:
            rejected = await replace(restarted, "source.txt", previous_ref, "new\n")
            assert rejected["outcome"] == "rejected" and rejected["changed"] is False
            assert target.read_text(encoding="utf-8") == "old\n"
            assert_recovery(rejected)
            observed = await success(restarted, "read_workspace_file", {"path": "source.txt"})
            assert (await replace(restarted, "source.txt", reference(observed), "new\n"))["outcome"] == "applied"

    asyncio.run(scenario())


def test_reference_cannot_be_used_to_replace_another_existing_file(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("same\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "a.txt"})
            rejected = await replace(client, "b.txt", reference(observed), "changed\n")
            assert rejected["outcome"] == "rejected" and rejected["changed"] is False
            assert_recovery(rejected)
            assert (tmp_path / "a.txt").read_bytes() == (tmp_path / "b.txt").read_bytes() == b"same\n"

    asyncio.run(scenario())


def test_user_deleted_file_is_not_recreated_by_a_pending_edit(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("old\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "source.txt"})
            target.unlink()
            rejected = await replace(client, "source.txt", reference(observed), "must not resurrect\n")
            assert rejected["outcome"] == "rejected" and rejected["changed"] is False
            assert not target.exists()
            assert_recovery(rejected)

    asyncio.run(scenario())


def test_long_replacement_returns_a_bounded_reference_and_the_next_unobserved_line(tmp_path: Path) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("old\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            old = await success(client, "read_workspace_file", {"path": "source.txt"})
            result = await replace(client, "source.txt", reference(old), "new\n" * 600)
            assert result["outcome"] == "applied"
            assert result["start_line"] == 1 and result["end_line"] == 500
            assert result["content"] == "new\n" * 500
            assert reference(result)
            next_call = TypeAdapter(dict[str, object]).validate_python(result["next_call"])
            arguments = TypeAdapter(dict[str, object]).validate_python(next_call["arguments"])
            assert next_call["tool"] == "read_workspace_file" and arguments["start_line"] == 501
            remaining = await success(client, "read_workspace_file", arguments)
            assert remaining["content"] == "new\n" * 100 and reference(remaining)

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["../invalid-path-canary", "/invalid-path-canary", ".env", "invalid-path-canary\0"])
def test_invalid_edit_path_is_a_safe_error_without_changing_the_file(tmp_path: Path, path: str) -> None:
    policy = configured(tmp_path)
    target = tmp_path / "source.txt"
    target.write_text("original\n", encoding="utf-8")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            observed = await success(client, "read_workspace_file", {"path": "source.txt"})
            error = await failure(
                client,
                "replace_workspace_file",
                {"path": path, "read_ref": reference(observed), "replacement": "must not write\n"},
            )
            assert "invalid-path-canary" not in error
            assert target.read_text(encoding="utf-8") == "original\n"

    asyncio.run(scenario())
