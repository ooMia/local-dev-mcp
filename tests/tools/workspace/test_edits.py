"""Keep observed byte-range replacement independent of filesystem behavior."""

import pytest

from local_dev_mcp.tools.workspace._edits import EditRejected, replace_span, replacement_bytes
from local_dev_mcp.tools.workspace._response import BODY_BYTES, FILE_BYTES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("data", "original", "start", "end", "replacement", "expected"),
    [
        (b"before\nold\nafter", "old\n", 2, 2, "new\n", b"before\nnew\nafter"),
        (b"before\r\nold\r\nafter", "old\r\n", 2, 2, "새 값\r\n", "before\r\n새 값\r\nafter".encode()),
        (b"a\n\nb", "\n", 2, 2, "", b"a\nb"),
        (b"a\nb", "b", 2, 2, "last", b"a\nlast"),
        (b"a\rold\nb", "a\rold\n", 1, 1, "new\n", b"new\nb"),
        (b"a\nold\ntail\n", "old\n", 2, 2, "joined", b"a\njoinedtail\n"),
        (b"", "", 1, None, "first 😀\n", "first 😀\n".encode()),
    ],
)
def test_replacement_preserves_untouched_bytes_and_exact_requested_text(
    data: bytes, original: str, start: int, end: int | None, replacement: str, expected: bytes
) -> None:
    assert replace_span(data, original, start, end, replacement_bytes(replacement)) == expected


@pytest.mark.parametrize(
    ("original", "start", "end"),
    [("different\n", 1, 1), ("one\n", 0, 1), ("one\n", 1, 3), ("one\n", 1, None)],
)
def test_stale_or_invalid_observed_range_is_rejected(original: str, start: int, end: int | None) -> None:
    with pytest.raises(EditRejected) as raised:
        replace_span(b"one\ntwo\n", original, start, end, b"replacement\n")

    assert raised.value.code == "stale_version"


@pytest.mark.parametrize("replacement", ["contains\0nul", "\ud800", "é" * (BODY_BYTES // 2 + 1)])
def test_invalid_replacement_is_rejected_by_utf8_bytes_not_character_count(replacement: str) -> None:
    with pytest.raises(EditRejected) as raised:
        replacement_bytes(replacement)

    assert raised.value.code == "invalid_replacement"
    assert replacement not in raised.value.reason


def test_exact_replacement_byte_limit_is_accepted() -> None:
    replacement = "é" * (BODY_BYTES // 2)
    assert len(replacement_bytes(replacement)) == BODY_BYTES


def test_file_size_limit_applies_to_result_including_untouched_bytes() -> None:
    data = b"a\n" + b"z" * (FILE_BYTES - 2)
    assert len(replace_span(data, "a\n", 1, 1, b"b\n")) == FILE_BYTES

    with pytest.raises(EditRejected) as raised:
        replace_span(data, "a\n", 1, 1, b"bb\n")

    assert raised.value.code == "invalid_replacement"
