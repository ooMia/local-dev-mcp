"""Q79 append-only Attempt evidence is minimal, canonical and not a POST result."""

import json
from dataclasses import replace

import pytest

from local_dev_mcp.tools.gh.delivery.operation_attempt import (
    ATTEMPT_VERSION,
    MAX_ATTEMPT_BYTES,
    AttemptIntegrityError,
    OperationAttempt,
    validate_attempt_row,
)

pytestmark = pytest.mark.unit
DIGEST = "sha256:" + "c" * 64


def attempt() -> OperationAttempt:
    return OperationAttempt(
        event_id=123,
        operation_id="label",
        origin_generation=7,
        intent_digest=DIGEST,
    )


def test_attempt_is_canonical_payload_free_and_generation_bound() -> None:
    value = attempt()
    raw = value.canonical_bytes()
    assert len(raw) < MAX_ATTEMPT_BYTES
    assert OperationAttempt.from_canonical_bytes(raw) == value
    assert value.version == ATTEMPT_VERSION
    assert value.attempt_digest.startswith("sha256:")
    assert b"token" not in raw and b"triaged" not in raw and b"POST" not in raw
    assert replace(value, origin_generation=8).attempt_digest != value.attempt_digest
    assert replace(value, intent_digest="sha256:" + "d" * 64).attempt_digest != value.attempt_digest


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{" + b"x" * MAX_ATTEMPT_BYTES + b"}",
        b"not json",
        b'{"event_id":123}',
        b'{"event_id":123,"event_id":123}',
        b'{"version":"other","event_id":123,"operation_id":"label","origin_generation":7,"intent_digest":"'
        + DIGEST.encode()
        + b'"}',
    ],
)
def test_reject_invalid_or_noncanonical_bytes(raw: bytes) -> None:
    with pytest.raises(ValueError):
        OperationAttempt.from_canonical_bytes(raw)


def test_attempt_roundtrip_rejects_tampering_and_extra_data() -> None:
    value = attempt()
    original = json.loads(value.canonical_bytes())
    for field, replacement in [
        ("event_id", True),
        ("origin_generation", -1),
        ("operation_id", ""),
        ("intent_digest", "bad"),
    ]:
        changed = dict(original)
        changed[field] = replacement
        with pytest.raises(ValueError):
            OperationAttempt.from_canonical_bytes(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode())
    changed = dict(original)
    changed["api_token"] = "secret"
    with pytest.raises(ValueError):
        OperationAttempt.from_canonical_bytes(json.dumps(changed).encode())
    with pytest.raises(ValueError):
        OperationAttempt.from_canonical_bytes(value.canonical_bytes() + b" ")


def test_row_validator_rejects_wrong_generation_intent_digest_or_bytes() -> None:
    value = attempt()
    row = (value.origin_generation, value.intent_digest, value.version, value.attempt_digest, value.canonical_bytes())
    assert validate_attempt_row(value.event_id, value.operation_id, row).attempt == value
    for corrupt in [
        (value.origin_generation + 1, *row[1:]),
        (row[0], "sha256:" + "a" * 64, *row[2:]),
        (row[0], row[1], "other", *row[3:]),
        (row[0], row[1], row[2], "sha256:" + "a" * 64, row[4]),
        (row[0], row[1], row[2], row[3], b"corrupt"),
    ]:
        with pytest.raises(AttemptIntegrityError):
            validate_attempt_row(value.event_id, value.operation_id, corrupt)
