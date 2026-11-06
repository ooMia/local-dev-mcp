"""Bound known-value diagnostics without claiming general secret detection."""

import base64
import codecs
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote, quote_plus

REDACTED: Final = "[REDACTED]"
WITHHELD: Final = "[diagnostics withheld]"
MAX_SECRET_VALUES: Final = 256
MAX_SECRET_BYTES: Final = 8192
MAX_CATALOG_BYTES: Final = 1024 * 1024
MAX_VARIANTS: Final = 4096
_CHUNK_BYTES: Final = 16384


def _variants(value: str, encoded: bytes) -> set[str]:
    quoted = (quote(value, safe=""), quote_plus(value, safe=""))
    variants = {
        value,
        json.dumps(value, ensure_ascii=True)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        *quoted,
    }
    variants.update(re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), item) for item in quoted)
    for data in (base64.b64encode(encoded), base64.urlsafe_b64encode(encoded)):
        text = data.decode("ascii")
        variants.update((text, text.rstrip("=")))
    return variants


def _merge(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass(frozen=True)
class Redactor:
    """A bounded startup catalog; unknown transformations remain outside its scope."""

    _pattern: re.Pattern[str] | None = field(default=None, repr=False)
    _max_chars: int = field(default=1, repr=False)
    reason: str | None = None

    @property
    def safe(self) -> bool:
        return self.reason is None

    @property
    def overlap_chars(self) -> int:
        return self._max_chars - 1

    @classmethod
    def from_values(cls, values: Mapping[str, str | None], public_names: tuple[str, ...] = ()) -> Redactor:
        public = frozenset(public_names)
        private: set[str] = set()
        patterns: set[str] = set()
        catalog_bytes = 0
        for name, value in values.items():
            if name in public or value is None or value == "" or value in private:
                continue
            if len(value) <= 3:
                return cls(reason="private_value_too_short")
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                return cls(reason="invalid_private_value")
            if len(encoded) > MAX_SECRET_BYTES:
                return cls(reason="private_value_limit")
            private.add(value)
            if len(private) > MAX_SECRET_VALUES:
                return cls(reason="private_value_count_limit")
            for variant in _variants(value, encoded):
                if variant in patterns:
                    continue
                patterns.add(variant)
                catalog_bytes += len(variant.encode("utf-8"))
                if len(patterns) > MAX_VARIANTS or catalog_bytes > MAX_CATALOG_BYTES:
                    return cls(reason="redaction_catalog_limit")
        if not patterns:
            return cls()
        # Lookahead finds overlapping values; longest first covers equal starts.
        ordered = sorted(patterns, key=lambda item: (-len(item), item))
        expression = "(?=(" + "|".join(re.escape(item) for item in ordered) + "))"
        return cls(re.compile(expression), max(map(len, patterns)))

    def matching_spans(self, text: str) -> tuple[tuple[int, int], ...]:
        if self._pattern is None:
            return ()
        return _merge([match.span(1) for match in self._pattern.finditer(text)])

    def sanitize(self, text: str) -> str:
        if not self.safe:
            return WITHHELD
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return WITHHELD
        pieces: list[str] = []
        position = 0
        for start, end in self.matching_spans(text):
            pieces.extend((text[position:start], REDACTED))
            position = end
        pieces.append(text[position:])
        return "".join(pieces)


class BoundedRedactedOutput:
    """Sanitize before retaining output, including matches split across byte chunks."""

    _redactor: Redactor
    _limit: int
    _decoder: codecs.IncrementalDecoder
    _pending: str
    _covered: tuple[tuple[int, int], ...]
    _pieces: list[str]
    _bytes: int
    _open_mask: bool
    _finished: bool
    _reason: str | None

    def __init__(self, redactor: Redactor, limit: int = 65536) -> None:
        if limit < 0:
            raise ValueError("Output limit must be nonnegative.")
        self._redactor = redactor
        self._limit = limit
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._pending = ""
        self._covered = ()
        self._pieces = []
        self._bytes = 0
        self._open_mask = False
        self._finished = False
        self._reason = None
        if not redactor.safe:
            self._withhold(redactor.reason or "unsafe_redaction_catalog")

    @property
    def text(self) -> str:
        return "".join(self._pieces)

    @property
    def complete(self) -> bool:
        return self._finished and self._reason is None

    @property
    def reason(self) -> str | None:
        return self._reason

    def _clear_pending(self) -> None:
        self._pending = ""
        self._covered = ()

    def _withhold(self, reason: str) -> None:
        self._reason = reason
        self._clear_pending()
        self._decoder.reset()
        marker = WITHHELD if len(WITHHELD.encode()) <= self._limit else ""
        self._pieces = [marker] if marker else []
        self._bytes = len(marker.encode())

    def _append(self, text: str, *, marker: bool = False) -> None:
        if not text or self._reason is not None:
            return
        encoded = text.encode("utf-8")
        available = self._limit - self._bytes
        if len(encoded) <= available:
            self._pieces.append(text)
            self._bytes += len(encoded)
            return
        # A redaction marker is atomic; truncation cannot expose raw prefixes.
        prefix = "" if marker else encoded[:available].decode("utf-8", errors="ignore")
        if prefix:
            self._pieces.append(prefix)
            self._bytes += len(prefix.encode("utf-8"))
        self._reason = "output_limit"

    def _process(self, decoded: str, *, final: bool = False) -> None:
        if self._reason is not None:
            return
        text = self._pending + decoded
        spans = _merge([*self._covered, *self._redactor.matching_spans(text)])
        cutoff = len(text) if final else max(0, len(text) - self._redactor.overlap_chars)
        position = 0
        for start, end in spans:
            if start >= cutoff:
                break
            if start > position:
                self._append(text[position:start])
                self._open_mask = False
            if not self._open_mask:
                self._append(REDACTED, marker=True)
                self._open_mask = True
            position = min(end, cutoff)
        if position < cutoff:
            self._append(text[position:cutoff])
            self._open_mask = False
        if self._reason is not None or final:
            self._clear_pending()
        else:
            self._pending = text[cutoff:]
            self._covered = tuple((max(start, cutoff) - cutoff, end - cutoff) for start, end in spans if end > cutoff)

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            raise ValueError("Output collection is already finished.")
        if self._reason is not None and self._reason != "output_limit":
            return
        for offset in range(0, len(chunk), _CHUNK_BYTES):
            try:
                decoded = self._decoder.decode(chunk[offset : offset + _CHUNK_BYTES])
            except UnicodeDecodeError:
                self._withhold("invalid_utf8")
                return
            self._process(decoded)

    def finish(self) -> None:
        if self._finished:
            return
        if self._reason is None or self._reason == "output_limit":
            try:
                decoded = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._withhold("invalid_utf8")
            else:
                self._process(decoded, final=True)
        self._clear_pending()
        self._decoder.reset()
        self._finished = True
