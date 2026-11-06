"""Apply an observed text span to bytes without normalizing untouched content."""

from collections.abc import Iterator

from ._response import BODY_BYTES, FILE_BYTES


class EditRejected(Exception):
    code: str
    reason: str

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason


def replacement_bytes(replacement: str) -> bytes:
    try:
        data = replacement.encode("utf-8")
    except UnicodeError:
        raise EditRejected("invalid_replacement", "replacement must be valid UTF-8 text.") from None
    if b"\0" in data or len(data) > BODY_BYTES:
        raise EditRejected("invalid_replacement", "replacement must contain no NUL and fit within 16 KiB UTF-8.")
    return data


def replace_span(data: bytes, original: str, start_line: int, end_line: int | None, replacement: bytes) -> bytes:
    if not data and start_line == 1 and end_line is None and original == "":
        return replacement
    if end_line is None or not (1 <= start_line <= end_line):
        raise EditRejected("stale_version", "The observed range is no longer valid. Read and reconstruct the edit.")
    # Find boundaries without allocating one object per line of the whole file.
    before = 0
    for _ in range(start_line - 1):
        marker = data.find(b"\n", before)
        if marker < 0:
            raise EditRejected("stale_version", "The observed range no longer exists. Read the current file.")
        before = marker + 1
    end = before
    for _ in range(end_line - start_line + 1):
        if end >= len(data):
            raise EditRejected("stale_version", "The observed range no longer exists. Read the current file.")
        marker = data.find(b"\n", end)
        end = len(data) if marker < 0 else marker + 1
    if data[before:end] != original.encode("utf-8"):
        raise EditRejected(
            "stale_version", "The observed text differs. Read the current range and reconstruct the edit."
        )
    updated = data[:before] + replacement + data[end:]
    if len(updated) > FILE_BYTES:
        raise EditRejected("invalid_replacement", "The edited file would exceed the 8 MiB file limit.")
    return updated


def following_lines(data: bytes, start_line: int, count: int) -> Iterator[tuple[int, bytes]]:
    """Yield complete LF-delimited lines, falling back to the last line after a tail deletion."""
    offset, number = 0, 1
    while number < start_line:
        marker = data.find(b"\n", offset)
        if marker < 0 or marker + 1 >= len(data):
            break
        offset, number = marker + 1, number + 1
    for _ in range(count):
        marker = data.find(b"\n", offset)
        end = len(data) if marker < 0 else marker + 1
        yield number, data[offset:end]
        if end >= len(data):
            break
        offset, number = end, number + 1
