"""Decode bounded byte chunks without normalizing text or consulting the OS."""

import codecs
import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal


class TextDataError(Exception):
    kind: Literal["binary", "encoding", "too_large"]

    def __init__(self, kind: Literal["binary", "encoding", "too_large"]) -> None:
        self.kind = kind


@dataclass
class TextLines:
    max_bytes: int
    version: str = ""
    total_lines: int = 0

    def lines(self, chunks: Iterable[bytes]) -> Iterator[str]:
        """Publish a raw-byte version only after the entire input validates."""
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        count = 0
        self.version = ""
        self.total_lines = 0
        try:
            for chunk in chunks:
                count += len(chunk)
                if count > self.max_bytes:
                    raise TextDataError("too_large")
                if b"\0" in chunk:
                    raise TextDataError("binary")
                digest.update(chunk)
                pieces = (pending + decoder.decode(chunk)).split("\n")
                pending = pieces.pop()
                for line in pieces:
                    self.total_lines += 1
                    yield line + "\n"
            pending += decoder.decode(b"", final=True)
            if pending:
                self.total_lines += 1
                yield pending
        except UnicodeDecodeError:
            raise TextDataError("encoding") from None
        self.version = digest.hexdigest()
