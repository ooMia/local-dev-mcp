"""Decode porcelain-v2 byte records without a repository or process fixture."""

import pytest
from fastmcp.exceptions import ToolError

# Same-tool tests exercise malformed records a real Git process would not emit.
from local_dev_mcp.tools.git.commands.status import _parse  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.unit

HEAD = "a" * 40


def records(*items: str) -> bytes:
    return ("\0".join(items) + "\0").encode("utf-8")


def status(*items: str) -> bytes:
    return records(f"# branch.oid {HEAD}", "# branch.head main", *items)


@pytest.mark.parametrize(
    ("oid", "label", "expected_head", "expected_branch"),
    [
        ("(initial)", "main", None, "main"),
        (HEAD, "(detached)", HEAD, None),
        ("b" * 64, "feature/sha256", "b" * 64, "feature/sha256"),
    ],
)
def test_branch_headers_preserve_unborn_detached_and_full_sha256_observations(
    oid: str, label: str, expected_head: str | None, expected_branch: str | None
) -> None:
    data = records(f"# branch.oid {oid}", f"# branch.head {label}")

    assert _parse(data) == (expected_branch, expected_head, [])


def test_ordinary_status_preserves_both_change_columns_and_submodule_state() -> None:
    data = status(f"1 .M S.M. 100644 100644 100644 {HEAD} {HEAD} src/한 글.py")

    branch, head, entries = _parse(data)

    assert (branch, head) == ("main", HEAD)
    assert entries == [
        {
            "kind": "ordinary",
            "path": "src/한 글.py",
            "original_path": None,
            "index_status": ".",
            "worktree_status": "M",
            "submodule": "S.M.",
        }
    ]


def test_rename_consumes_the_nul_delimited_original_path_without_splitting_names() -> None:
    new = "-new [name]*\n한 글.txt"
    old = 'old "name"\nwith spaces.txt'
    data = status(f"2 R. N... 100644 100644 100644 {HEAD} {HEAD} R100 {new}", old, "? later.txt")

    _, _, entries = _parse(data)

    assert entries[0] == {
        "kind": "renamed",
        "path": new,
        "original_path": old,
        "index_status": "R",
        "worktree_status": ".",
        "submodule": "N...",
    }
    assert entries[1]["kind"] == "untracked"
    assert entries[1]["path"] == "later.txt"


def test_unmerged_status_is_distinct_from_ordinary_and_keeps_the_conflict_columns() -> None:
    data = status(f"u UU N... 100644 100644 100644 100644 {HEAD} {HEAD} {HEAD} conflict.txt")

    _, _, entries = _parse(data)

    assert entries == [
        {
            "kind": "unmerged",
            "path": "conflict.txt",
            "original_path": None,
            "index_status": "U",
            "worktree_status": "U",
            "submodule": "N...",
        }
    ]


@pytest.mark.parametrize(("prefix", "kind"), [("?", "untracked"), ("!", "ignored")])
def test_untracked_and_ignored_names_are_literal_data(prefix: str, kind: str) -> None:
    name = 'folder/a "quoted" [name]*\n한 글.txt'

    _, _, entries = _parse(status(f"{prefix} {name}"))

    assert entries == [
        {
            "kind": kind,
            "path": name,
            "original_path": None,
            "index_status": prefix,
            "worktree_status": prefix,
            "submodule": None,
        }
    ]


def test_unknown_headers_do_not_hide_following_records_or_require_a_fixed_header_order() -> None:
    data = records(
        "# branch.head main",
        "# future.header opaque value",
        "? before.txt",
        f"# branch.oid {HEAD}",
        "# branch.ab +2 -1",
        "? after.txt",
    )

    branch, head, entries = _parse(data)

    assert (branch, head) == ("main", HEAD)
    assert [entry["path"] for entry in entries] == ["before.txt", "after.txt"]


@pytest.mark.parametrize("data", [b"", status("? file.txt")[:-1]])
def test_truncated_records_are_rejected_instead_of_returning_partial_status(data: bytes) -> None:
    with pytest.raises(ToolError, match="incomplete status record"):
        _parse(data)


@pytest.mark.parametrize("header", [f"# branch.oid {HEAD}", "# branch.head main"])
def test_both_head_and_branch_headers_are_required(header: str) -> None:
    with pytest.raises(ToolError, match="did not provide branch and HEAD observations"):
        _parse(records(header))


@pytest.mark.parametrize("oid", ["not-a-sha", "a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40])
def test_invalid_head_headers_are_rejected_without_coercion(oid: str) -> None:
    with pytest.raises(ToolError, match="invalid status HEAD"):
        _parse(records(f"# branch.oid {oid}", "# branch.head main"))


@pytest.mark.parametrize(
    "record",
    [
        "1 M",
        f"1 M N... 100644 100644 100644 {HEAD} {HEAD} file.txt",
        "u UU N... 100644 conflict.txt",
    ],
)
def test_malformed_change_records_are_rejected(record: str) -> None:
    with pytest.raises(ToolError, match="malformed status record"):
        _parse(status(record))


def test_rename_requires_its_separate_original_path_record() -> None:
    data = status(f"2 R. N... 100644 100644 100644 {HEAD} {HEAD} R100 new.txt")

    with pytest.raises(ToolError, match="incomplete rename status record"):
        _parse(data)


def test_unknown_nonheader_record_is_rejected() -> None:
    with pytest.raises(ToolError, match="unsupported status record"):
        _parse(status("x unsupported"))


@pytest.mark.parametrize("name", [b"bad\xff", b"truncated\xe2\x82"])
def test_invalid_utf8_filenames_are_never_replaced_or_returned_as_partial_status(name: bytes) -> None:
    data = status("? valid.txt") + b"? " + name + b"\0"

    with pytest.raises(ToolError, match="not valid UTF-8; no lossy"):
        _parse(data)
