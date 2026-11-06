"""Pure canonical evidence mechanics shared inside GitHub delivery.

Callers own length/shape/version policy and safe errors. The JSON decoder only
preserves UTF-8, duplicate-key and nonfinite-number rejection mechanics.
"""

import hashlib
import json
import re
from typing import cast

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def checked_label(value: object, field: str) -> str:
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        raise ValueError(f"{field} must be a supported bounded identifier.")
    return value


def checked_digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a qualified SHA-256 digest.")
    return value


def read_object(raw: bytes, limit: int, fields: set[str]) -> dict[str, object]:
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise ValueError("Invalid canonical evidence length.")
    try:
        parsed: object = decode_json(
            raw, duplicate_message="Duplicate canonical evidence field.", constant_message="Nonfinite JSON literal."
        )
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Invalid canonical evidence encoding.") from error
    if type(parsed) is not dict:
        raise ValueError("Evidence must be an object.")
    value = cast(dict[str, object], parsed)
    if set(value) != fields:
        raise ValueError("Unexpected canonical evidence fields.")
    return value


def encode(value: dict[str, object], limit: int) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > limit:
        raise ValueError("Canonical evidence exceeds size limit.")
    return raw


def digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def decode_json(raw: bytes, *, duplicate_message: str, constant_message: str) -> object:
    """Decode strict JSON without imposing a caller's shape or size policy."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(duplicate_message)
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise ValueError(constant_message)

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
