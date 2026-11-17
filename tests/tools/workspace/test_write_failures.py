"""Inject write-boundary failures and verify observable file and recovery state."""

import asyncio
import os
import threading
from pathlib import Path

import anyio
import pytest

import local_dev_mcp.tools.workspace._writes as writes
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.workspace import WorkspaceFiles
from local_dev_mcp.tools.workspace._reads import Budget
from local_dev_mcp.tools.workspace._response import BODY_BYTES, FileEdit
from tests.support.workspace import configure

pytestmark = pytest.mark.integration
IO_CANARY = "private-os-message-must-not-be-returned"


def prepared(root: Path, content: bytes = b"old\n") -> tuple[Path, WorkspaceFiles]:
    configure(root)
    target = root / "source.txt"
    target.write_bytes(content)
    return target, WorkspaceFiles(Policy.load(root))


async def reference(files: WorkspaceFiles, *, start_line: int = 1, max_lines: int = 200) -> str:
    observed = await files.read_file("source.txt", start_line, max_lines)
    assert observed.read_ref is not None
    return observed.read_ref


def apply(files: WorkspaceFiles, token: str, replacement: str, budget: Budget | None = None) -> FileEdit:
    return writes.replace_file(
        files.workspace,
        files.references,
        writes.WriteLocks(),
        "source.txt",
        token,
        replacement,
        budget or Budget(),
    )


def assert_read_recovery(result: FileEdit) -> None:
    assert result.next_action and result.reason
    assert result.next_call is not None
    assert result.next_call.tool == "read_workspace_file"
    assert result.next_call.arguments["path"] == "source.txt"
    assert IO_CANARY not in result.model_dump_json()


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failure_before_replacement_preserves_original_inode_bytes_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target, files = prepared(tmp_path)
    before = target.stat()

    async def scenario() -> None:
        token = await reference(files)

        def fail_fsync(descriptor: int) -> None:
            raise OSError(IO_CANARY)

        def fail_replace(source: str, destination: Path) -> None:
            raise OSError(IO_CANARY)

        if failure == "fsync":
            monkeypatch.setattr(writes.os, "fsync", fail_fsync)
        else:
            monkeypatch.setattr(writes.os, "replace", fail_replace)
        result = apply(files, token, "new\n")

        assert result.outcome == "rejected" and result.changed is False
        assert result.code == "io_before_replace"
        assert result.version_after is None and result.read_ref is None
        assert_read_recovery(result)
        assert target.read_bytes() == b"old\n"
        assert await asyncio.to_thread(lambda: target.stat().st_ino) == before.st_ino
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))

    asyncio.run(scenario())


def test_user_save_while_temporary_file_is_prepared_rejects_stale_edit_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)
    fsync = os.fsync

    async def scenario() -> None:
        token = await reference(files)

        def save_user_edit(descriptor: int) -> None:
            fsync(descriptor)
            target.write_bytes(b"user saved while preparing\n")

        with monkeypatch.context() as patch:
            patch.setattr(writes.os, "fsync", save_user_edit)
            rejected = apply(files, token, "pending agent edit\n")

        assert rejected.outcome == "rejected" and rejected.code == "stale_version"
        assert rejected.changed is False
        assert_read_recovery(rejected)
        assert target.read_bytes() == b"user saved while preparing\n"
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))
        fresh = await reference(files)
        resumed = apply(files, fresh, "user saved while preparing plus agent\n")
        assert resumed.outcome == "applied" and resumed.read_ref
        assert target.read_bytes() == b"user saved while preparing plus agent\n"

    asyncio.run(scenario())


def test_failed_observation_after_replacement_reports_applied_state_and_recovers_by_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)

    async def scenario() -> None:
        token = await reference(files)

        def fail_observation(*args: object, **kwargs: object) -> None:
            raise OSError(IO_CANARY)

        with monkeypatch.context() as patch:
            patch.setattr(writes, "read_file", fail_observation)
            result = apply(files, token, "written\n")

        assert result.outcome == "applied_unverified" and result.changed is True
        assert result.code == "observation_failed"
        assert result.read_ref is None and result.content is None
        assert_read_recovery(result)
        assert target.read_bytes() == b"written\n"
        fresh = await reference(files)
        resumed = apply(files, fresh, "written then resumed\n")
        assert resumed.outcome == "applied" and resumed.read_ref
        assert target.read_bytes() == b"written then resumed\n"

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["x" * BODY_BYTES, "\x01" * 6000 + "\n"])
def test_edit_that_would_make_changed_line_unreadable_is_rejected_before_publication(
    tmp_path: Path, replacement: str
) -> None:
    target, files = prepared(tmp_path, b"replace me\ntail\n")
    before = target.stat()

    async def scenario() -> None:
        token = await reference(files, max_lines=1)
        result = apply(files, token, replacement)

        assert result.outcome == "rejected" and result.changed is False
        assert result.code == "invalid_replacement"
        assert target.read_bytes() == b"replace me\ntail\n"
        assert await asyncio.to_thread(lambda: target.stat().st_ino) == before.st_ino
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))

    asyncio.run(scenario())


def test_cancellation_after_preparation_propagates_without_replacing_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)
    before = target.stat()
    prepared_file = threading.Event()
    resume_worker = threading.Event()
    fsync = os.fsync
    cancelled: list[bool] = []

    def pause_after_fsync(descriptor: int) -> None:
        fsync(descriptor)
        prepared_file.set()
        if not resume_worker.wait(10):
            raise AssertionError("Test owner did not release the prepared writer.")

    monkeypatch.setattr(writes.os, "fsync", pause_after_fsync)

    async def scenario() -> None:
        token = await reference(files)
        scopes: list[anyio.CancelScope] = []

        def work() -> None:
            try:
                apply(files, token, "cancelled write\n", Budget(anyio.from_thread.check_cancelled))
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            raise AssertionError("Cancelled edit returned a normal result.")

        async def writer() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await anyio.to_thread.run_sync(work)

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(writer)
                assert await anyio.to_thread.run_sync(prepared_file.wait, 10)
                scopes[0].cancel()
                resume_worker.set()
        finally:
            resume_worker.set()

        assert cancelled == [True]
        assert target.read_bytes() == b"old\n"
        assert await asyncio.to_thread(lambda: target.stat().st_ino) == before.st_ino
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))

    asyncio.run(scenario())


def test_cleanup_failure_does_not_turn_completed_replacement_into_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)

    async def scenario() -> None:
        token = await reference(files)

        def fail_cleanup(path: str) -> None:
            raise PermissionError(IO_CANARY)

        with monkeypatch.context() as patch:
            patch.setattr(writes.os, "unlink", fail_cleanup)
            result = apply(files, token, "committed\n")

        assert result.outcome == "applied" and result.changed is True
        assert result.read_ref and result.version_after
        assert result.cleanup_warning
        assert IO_CANARY not in result.model_dump_json()
        assert target.read_bytes() == b"committed\n"

    asyncio.run(scenario())


def test_new_ignore_rule_during_preparation_is_checked_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)
    before = target.stat()
    fsync = os.fsync

    async def scenario() -> None:
        token = await reference(files)

        def protect_source(descriptor: int) -> None:
            fsync(descriptor)
            (tmp_path / ".gitignore").write_text("source.txt\n", encoding="utf-8")

        with monkeypatch.context() as patch:
            patch.setattr(writes.os, "fsync", protect_source)
            result = apply(files, token, "now forbidden\n")

        assert result.outcome == "rejected" and result.code == "ignored_path"
        assert result.changed is False
        assert result.next_call is None and result.next_action
        assert target.read_bytes() == b"old\n"
        assert await asyncio.to_thread(lambda: target.stat().st_ino) == before.st_ino
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))

    asyncio.run(scenario())


def test_cancellation_after_replacement_preserves_applied_file_and_recovers_with_new_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, files = prepared(tmp_path)
    replaced = threading.Event()
    resume_worker = threading.Event()
    replace = os.replace
    cancelled: list[bool] = []

    def pause_after_replace(source: str, destination: Path) -> None:
        replace(source, destination)
        replaced.set()
        if not resume_worker.wait(10):
            raise AssertionError("Test owner did not release the published writer.")

    async def scenario() -> None:
        token = await reference(files)
        scopes: list[anyio.CancelScope] = []

        def work() -> None:
            try:
                apply(files, token, "applied before cancellation\n", Budget(anyio.from_thread.check_cancelled))
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            raise AssertionError("Cancelled edit returned a normal result.")

        async def writer() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await anyio.to_thread.run_sync(work)

        with monkeypatch.context() as patch:
            patch.setattr(writes.os, "replace", pause_after_replace)
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(writer)
                    assert await anyio.to_thread.run_sync(replaced.wait, 10)
                    scopes[0].cancel()
                    resume_worker.set()
            finally:
                resume_worker.set()

        assert cancelled == [True]
        assert target.read_bytes() == b"applied before cancellation\n"
        assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".local-dev-mcp-*")))
        stale = apply(files, token, "must not replay\n")
        assert stale.outcome == "rejected" and stale.code == "stale_version"
        assert target.read_bytes() == b"applied before cancellation\n"
        fresh = await reference(files)
        resumed = apply(files, fresh, "applied and intentionally resumed\n")
        assert resumed.outcome == "applied" and resumed.read_ref
        assert target.read_bytes() == b"applied and intentionally resumed\n"

    asyncio.run(scenario())
