"""Experimental source-independent event identity and observation contracts."""

import re
from dataclasses import dataclass
from datetime import datetime

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _require_label(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value or value != value.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"{field} must be nonempty and have no boundary whitespace or control characters.")


def _require_type(value: object, expected: type[object], field: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{field} must be a {expected.__name__}.")


@dataclass(frozen=True, slots=True)
class EventIdentity:
    """An explicit source-qualified event key; not derived from its payload."""

    source_instance_id: str
    event_key: str

    def __post_init__(self) -> None:
        _require_label(self.source_instance_id, "source instance ID")
        _require_label(self.event_key, "event key")


@dataclass(frozen=True, slots=True)
class EventObservation:
    """Evidence describing an observed event, without raw payload or delivery state."""

    identity: EventIdentity
    event_type: str
    event_schema_version: str
    observed_at: datetime
    payload_digest: str

    def __post_init__(self) -> None:
        _require_type(self.identity, EventIdentity, "identity")
        _require_label(self.event_type, "event type")
        _require_label(self.event_schema_version, "event schema version")
        _require_type(self.observed_at, datetime, "observed_at")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a time zone.")
        _require_type(self.payload_digest, str, "payload_digest")
        if _SHA256_DIGEST.fullmatch(self.payload_digest) is None:
            raise ValueError("payload_digest must be a lowercase sha256: digest.")
