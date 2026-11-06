"""Validate byte-to-text behavior with data inputs and no filesystem fixtures."""

import hashlib
from collections.abc import Iterator

import pytest

from local_dev_mcp.tools.workspace._text import TextDataError, TextLines

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("width", [1, 2, 3, 7, 65536])
def test_text_preserves_utf8_crlf_and_final_partial_line_across_chunks(width: int) -> None:
    data = "첫 줄\r\n\t둘째  \r\nlast\r".encode()
    text = TextLines(len(data))
    chunks = (data[index : index + width] for index in range(0, len(data), width))

    assert list(text.lines(chunks)) == ["첫 줄\r\n", "\t둘째  \r\n", "last\r"]
    assert text.total_lines == 3
    assert text.version == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((), []),
        ((b"", b""), []),
        ((b"\n",), ["\n"]),
        ((b"one\n", b"", b"\n"), ["one\n", "\n"]),
        ((b"one\r", b"\ntwo"), ["one\r\n", "two"]),
    ],
)
def test_empty_input_and_trailing_newlines_have_exact_line_counts(
    chunks: tuple[bytes, ...], expected: list[str]
) -> None:
    text = TextLines(100)

    assert list(text.lines(chunks)) == expected
    assert text.total_lines == len(expected)
    assert text.version == hashlib.sha256(b"".join(chunks)).hexdigest()


@pytest.mark.parametrize("chunks", [(b"\xff",), (b"\xe2", b"\x82"), (b"ok\n", b"\xc0\xaf")])
def test_invalid_or_truncated_utf8_never_publishes_a_version(chunks: tuple[bytes, ...]) -> None:
    text = TextLines(100)

    with pytest.raises(TextDataError) as caught:
        list(text.lines(chunks))

    assert caught.value.kind == "encoding"
    assert text.version == ""


def test_nul_after_valid_lines_rejects_the_entire_observation() -> None:
    text = TextLines(100)
    lines = text.lines((b"valid\n", b"secret\0\n"))

    assert next(lines) == "valid\n"
    with pytest.raises(TextDataError) as caught:
        next(lines)

    assert caught.value.kind == "binary"
    assert text.version == ""


def test_version_waits_for_exhaustion_and_covers_every_raw_byte() -> None:
    text = TextLines(100)
    lines = text.lines((b"a\r\n", b"b\n"))

    assert next(lines) == "a\r\n"
    assert text.version == ""
    assert next(lines) == "b\n"
    assert text.version == ""
    with pytest.raises(StopIteration):
        next(lines)

    assert text.version == hashlib.sha256(b"a\r\nb\n").hexdigest()
    assert text.version != hashlib.sha256(b"a\nb\n").hexdigest()


def test_complete_lines_are_yielded_before_later_chunks_are_consumed() -> None:
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        for index, chunk in enumerate((b"one\n", b"two")):
            consumed.append(index)
            yield chunk

    text = TextLines(100)
    lines = text.lines(chunks())

    assert next(lines) == "one\n"
    assert consumed == [0]
    assert list(lines) == ["two"]
    assert consumed == [0, 1]
    assert text.version == hashlib.sha256(b"one\ntwo").hexdigest()


def test_byte_limit_accepts_the_boundary_and_rejects_a_split_character_overflow() -> None:
    data = "한".encode()
    exact = TextLines(len(data))
    assert list(exact.lines((data[:1], data[1:]))) == ["한"]

    short = TextLines(len(data) - 1)
    with pytest.raises(TextDataError) as caught:
        list(short.lines((data[:1], data[1:])))

    assert caught.value.kind == "too_large"
    assert short.version == ""
