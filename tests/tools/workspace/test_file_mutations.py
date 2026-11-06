"""Exercise bounded file creation/deletion and retries using test-owned resources."""

import asyncio
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import local_dev_mcp.tools.workspace._mutations as mutations
import local_dev_mcp.tools.workspace._receipts as receipts
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.workspace import WorkspaceFiles
from local_dev_mcp.tools.workspace._mutations import FileMutations
from local_dev_mcp.tools.workspace._reads import Budget
from local_dev_mcp.tools.workspace._response import RESPONSE_BYTES
from local_dev_mcp.tools.workspace.errors import WorkspaceError
from tests.support.workspace import configure

pytestmark = pytest.mark.integration
CANARY = "private-system-message-must-not-escape"


def prepared(root: Path, *, policy: Policy | None = None) -> tuple[WorkspaceFiles, FileMutations]:
    if policy is None:
        configure(root)
        policy = Policy.load(root)
    files = WorkspaceFiles(policy)
    return files, FileMutations(files.workspace, files.references, files.write_locks)


def observe(files: WorkspaceFiles, path: str) -> str:
    result = asyncio.run(files.read_file(path, 1, 1))
    assert result.read_ref is not None
    return result.read_ref


@pytest.mark.parametrize("content", ["", "first\n", "no final newline", "\t한글 😀\r\nsecond\r\n"])
def test_create_preserves_exact_text_and_returns_a_read_reference(tmp_path: Path, content: str) -> None:
    files, operations = prepared(tmp_path)
    result = operations.create("new.py", content, "create-1", False, Budget())
    assert result.outcome == "applied" and result.changed
    assert result.content == content and result.read_ref is not None
    assert result.version_after == hashlib.sha256(content.encode()).hexdigest()
    assert (tmp_path / "new.py").read_bytes() == content.encode()
    record = files.references.get(result.read_ref, files.workspace.path("new.py"))
    assert record.version == result.version_after
    assert not list(tmp_path.glob(".local-dev-mcp-*"))


def test_existing_path_is_not_overwritten_and_corrected_arguments_require_a_new_id(tmp_path: Path) -> None:
    _, operations = prepared(tmp_path)
    target = tmp_path / "existing.py"
    target.write_bytes(b"user content\n")
    result = operations.create("existing.py", "agent content\n", "already-there", False, Budget())
    assert result.outcome == "rejected" and result.code == "path_exists"
    assert target.read_bytes() == b"user content\n"
    with pytest.raises(WorkspaceError, match="different"):
        operations.create("other.py", "agent content\n", "already-there", False, Budget())
    assert not (tmp_path / "other.py").exists()


def test_create_parents_is_explicit_and_its_next_call_uses_a_new_request(tmp_path: Path) -> None:
    _, operations = prepared(tmp_path)
    rejected = operations.create("src/new/module.py", "value = 1\n", "no-parents", False, Budget())
    assert rejected.code == "missing_parent" and not rejected.changed
    assert not (tmp_path / "src").exists()
    assert rejected.next_call is not None
    args = rejected.next_call.arguments
    assert args["create_parents"] is True and args["request_id"] != "no-parents"
    with pytest.raises(WorkspaceError, match="different"):
        operations.create("src/new/module.py", "value = 1\n", "no-parents", True, Budget())
    created = operations.create("src/new/module.py", "value = 1\n", "with-parents", True, Budget())
    assert created.outcome == "applied"
    assert created.created_directories == ["src", "src/new"]
    assert (tmp_path / "src/new/module.py").read_bytes() == b"value = 1\n"


def test_parent_side_effects_are_reported_and_never_rolled_back_over_user_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations = prepared(tmp_path)
    mkdir = os.mkdir

    def fail_second(path: Path) -> None:
        if Path(path).name == "later":
            raise OSError(CANARY)
        mkdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(mutations.os, "mkdir", fail_second)
        rejected = operations.create("made/later/file.py", "content", "partial-parents", True, Budget())
    assert rejected.outcome == "rejected" and not rejected.changed
    assert rejected.created_directories == ["made"]
    user_file = tmp_path / "made/user.txt"
    user_file.write_text("user added after failure")
    repeated = operations.create("made/later/file.py", "content", "partial-parents", True, Budget())
    assert repeated.replayed and repeated.created_directories == ["made"]
    assert user_file.read_text() == "user added after failure"
    assert CANARY not in repeated.model_dump_json()


def test_creation_rechecks_destination_before_exclusive_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations = prepared(tmp_path)
    fsync = os.fsync
    target = tmp_path / "new.py"

    def user_save(descriptor: int) -> None:
        fsync(descriptor)
        target.write_bytes(b"created by user\n")

    monkeypatch.setattr(mutations.os, "fsync", user_save)
    result = operations.create("new.py", "agent content", "race-existing", False, Budget())
    assert result.code == "path_exists" and not result.changed
    assert target.read_bytes() == b"created by user\n"
    assert not list(tmp_path.glob(".local-dev-mcp-*"))


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_creation_failure_before_publication_leaves_no_target_or_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, operations = prepared(tmp_path)

    def fail_fsync(descriptor: int) -> None:
        raise OSError(CANARY)

    def fail_link(source: str, target: Path, *, follow_symlinks: bool) -> None:
        raise OSError(CANARY)

    if failure == "fsync":
        monkeypatch.setattr(mutations.os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(mutations.os, "link", fail_link)
    result = operations.create("new.py", "agent content", "failed-publish", False, Budget())
    assert result.outcome == "rejected" and not result.changed
    assert not (tmp_path / "new.py").exists()
    assert not list(tmp_path.glob(".local-dev-mcp-*"))
    assert CANARY not in result.model_dump_json()


def test_failed_readback_keeps_the_published_state_and_retry_never_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations = prepared(tmp_path)

    def fail_read(*args: object) -> None:
        raise WorkspaceError(CANARY)

    with monkeypatch.context() as patch:
        patch.setattr(mutations, "read_file", fail_read)
        result = operations.create("new.py", "agent content", "failed-observation", False, Budget())
    assert result.outcome == "applied_unverified" and result.changed
    assert result.read_ref is None and CANARY not in result.model_dump_json()
    (tmp_path / "new.py").write_text("user revised it")
    replayed = operations.create("new.py", "agent content", "failed-observation", False, Budget())
    assert replayed.replayed and replayed.outcome == "applied_unverified"
    assert (tmp_path / "new.py").read_text() == "user revised it"


@pytest.mark.parametrize("cancel_after_publication", [False, True])
def test_cancellation_receipts_preserve_known_state_and_prevent_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_after_publication: bool
) -> None:
    _, operations = prepared(tmp_path)
    interrupted = False
    fsync, link = os.fsync, os.link

    def cancel() -> None:
        if interrupted:
            raise asyncio.CancelledError

    def finish_fsync(descriptor: int) -> None:
        nonlocal interrupted
        fsync(descriptor)
        interrupted = not cancel_after_publication

    def finish_link(source: str, target: Path, *, follow_symlinks: bool) -> None:
        nonlocal interrupted
        link(source, target, follow_symlinks=follow_symlinks)
        interrupted = True

    with monkeypatch.context() as patch:
        patch.setattr(mutations.os, "fsync", finish_fsync)
        patch.setattr(mutations.os, "link", finish_link)
        with pytest.raises(asyncio.CancelledError):
            operations.create("new.py", "created content", "cancelled-create", False, Budget(cancel))
    receipt = operations.create("new.py", "created content", "cancelled-create", False, Budget())
    assert receipt.replayed and receipt.changed is cancel_after_publication
    assert receipt.outcome == ("applied_unverified" if cancel_after_publication else "rejected")
    assert (tmp_path / "new.py").exists() is cancel_after_publication
    assert not list(tmp_path.glob(".local-dev-mcp-*"))


def test_concurrent_duplicate_submissions_create_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, operations = prepared(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    link = os.link
    calls: list[Path] = []

    def blocked_link(source: str, target: Path, *, follow_symlinks: bool) -> None:
        calls.append(target)
        entered.set()
        assert release.wait(5)
        link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mutations.os, "link", blocked_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operations.create, "new.py", "content", "same-request", False, Budget())
        try:
            assert entered.wait(5)
            second = pool.submit(operations.create, "new.py", "content", "same-request", False, Budget())
        finally:
            release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert len(calls) == 1
    assert sorted(result.replayed for result in results) == [False, True]
    assert all(result.outcome == "applied" for result in results)


def test_deletion_requires_a_current_whole_file_version_and_does_not_mutate_git(tmp_path: Path) -> None:
    files, operations = prepared(tmp_path)
    target = tmp_path / "source.py"
    target.write_bytes(b"first\nsecond\n")
    token = observe(files, "source.py")
    target.write_bytes(b"first\nuser changed unobserved line\n")
    stale = operations.delete("source.py", token, "stale-delete", Budget())
    assert stale.code == "stale_version" and not stale.changed
    assert target.read_bytes() == b"first\nuser changed unobserved line\n"
    fresh = observe(files, "source.py")
    deleted = operations.delete("source.py", fresh, "fresh-delete", Budget())
    assert deleted.outcome == "applied" and deleted.changed and deleted.version_after is None
    assert deleted.content is None and deleted.read_ref is None
    assert not target.exists() and not (tmp_path / ".git").exists()


def test_deletion_retry_after_user_recreation_or_symlink_never_touches_the_new_entry(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    files, operations = prepared(root)
    target = root / "source.py"
    target.write_text("old")
    token = observe(files, "source.py")
    deleted = operations.delete("source.py", token, "delete-once", Budget())
    target.write_text("user recreated")
    replay = operations.delete("source.py", token, "delete-once", Budget())
    assert replay.replayed and replay.changed == deleted.changed
    assert target.read_text() == "user recreated"
    target.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("outside user data")
    target.symlink_to(outside)
    replay = operations.delete("source.py", token, "delete-once", Budget())
    assert replay.replayed and outside.read_text() == "outside user data"
    assert target.is_symlink()


def test_recreated_entry_after_unlink_is_reported_without_deleting_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, operations = prepared(tmp_path)
    target = tmp_path / "source.py"
    target.write_text("old")
    token = observe(files, "source.py")
    unlink = os.unlink

    def recreate(path: Path) -> None:
        unlink(path)
        target.write_text("new user data")

    with monkeypatch.context() as patch:
        patch.setattr(mutations.os, "unlink", recreate)
        result = operations.delete("source.py", token, "delete-recreated", Budget())
    assert result.outcome == "applied_unverified" and result.code == "recreated_after_delete"
    assert result.changed and target.read_text() == "new user data"
    assert operations.delete("source.py", token, "delete-recreated", Budget()).replayed
    assert target.read_text() == "new user data"


def test_receipt_capacity_evicts_results_but_never_replays_old_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations = prepared(tmp_path)
    monkeypatch.setattr(receipts, "RECEIPT_COUNT", 1)
    monkeypatch.setattr(receipts, "REQUEST_COUNT", 2)
    operations.create("first.py", "first", "first", False, Budget())
    operations.create("second.py", "second", "second", False, Budget())
    with pytest.raises(WorkspaceError, match="expired"):
        operations.create("first.py", "first", "first", False, Budget())
    with pytest.raises(WorkspaceError, match="ledger is full"):
        operations.create("third.py", "third", "third", False, Budget())
    assert not (tmp_path / "third.py").exists()


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", ".env", ".git/data", ".", "bad\0name"])
def test_invalid_names_are_safe_errors_before_any_file_operation(tmp_path: Path, path: str) -> None:
    _, operations = prepared(tmp_path)
    with pytest.raises(WorkspaceError):
        operations.create(path, "content", "invalid-name", True, Budget())
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("setting", ["read_only_paths", "denied_paths"])
def test_new_paths_respect_negative_policy_scopes(tmp_path: Path, setting: str) -> None:
    configure(tmp_path)
    (tmp_path / "config.toml").write_text(
        f"[filesystem]\nallowed_paths=['.']\n{setting}=['restricted']\n[system]\nallowed_commands=[]\n"
    )
    _, operations = prepared(tmp_path, policy=Policy.load(tmp_path, allow_policy_edits=True))
    result = operations.create("restricted/deep/file.py", "content", "negative-policy", True, Budget())
    assert result.code == "write_denied" and not (tmp_path / "restricted").exists()


def test_gitignore_and_policy_file_protection_are_consistent_with_existing_edits(tmp_path: Path) -> None:
    files, operations = prepared(tmp_path)
    denied = operations.create(".gitignore", "*.tmp\n!keep.tmp\nignored/\n", "policy-off", False, Budget())
    assert denied.code == "write_denied"
    policy = Policy.load(tmp_path, allow_policy_edits=True)
    files, operations = prepared(tmp_path, policy=policy)
    allowed = operations.create(".gitignore", "*.tmp\n!keep.tmp\nignored/\n", "policy-on", False, Budget())
    assert allowed.outcome == "applied"
    assert operations.create("skip.tmp", "x", "ignored-file", False, Budget()).code == "ignored_path"
    assert operations.create("keep.tmp", "x", "negated-file", False, Budget()).outcome == "applied"
    denied_parent = operations.create("ignored/deep/a.py", "x", "ignored-parent", True, Budget())
    assert denied_parent.code == "ignored_path" and not (tmp_path / "ignored").exists()
    token = observe(files, "skip.tmp") if (tmp_path / "skip.tmp").exists() else None
    assert token is None


def test_new_parent_requires_write_permission_not_only_navigation(tmp_path: Path) -> None:
    configure(tmp_path)
    permitted = tmp_path / "missing" / "only.py"
    policy = Policy(tmp_path, (permitted,), ())
    _, operations = prepared(tmp_path, policy=policy)
    result = operations.create("missing/only.py", "content", "file-only", True, Budget())
    assert result.code == "write_denied"
    assert not (tmp_path / "missing").exists()


def test_invalid_content_and_long_path_fail_without_creating_files(tmp_path: Path) -> None:
    _, operations = prepared(tmp_path)
    for index, content in enumerate(("with\0nul", "x" * 16385, "\ud800", "\x01" * 6000)):
        result = operations.create("invalid.py", content, f"bad-content-{index}", False, Budget())
        assert result.code == "invalid_content" and not result.changed
        assert result.next_call is None
        assert len(result.model_dump_json().encode()) <= RESPONSE_BYTES
        assert not (tmp_path / "invalid.py").exists()
    with pytest.raises(WorkspaceError):
        operations.create("a/" * 520 + "file.py", "content", "long-path", True, Budget())
    assert not (tmp_path / "a").exists()


def test_large_corrective_call_is_omitted_instead_of_exceeding_response_budget(tmp_path: Path) -> None:
    _, operations = prepared(tmp_path)
    content = ("\x01" * 1000 + "\n") * 15
    result = operations.create("missing/new.py", content, "bounded-hint", False, Budget())
    assert result.code == "missing_parent" and result.next_call is None
    assert len(result.model_dump_json().encode()) <= RESPONSE_BYTES
    assert "NEW request_id" in (result.next_action or "")
    assert not (tmp_path / "missing").exists()
