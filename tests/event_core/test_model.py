"""Stage 0 acceptance for source-independent event evidence value objects."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from local_dev_mcp.event_core import EventIdentity, EventObservation

pytestmark = pytest.mark.unit

OBSERVED_AT = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def observation(
    identity: EventIdentity,
    *,
    digest: str = DIGEST,
    event_type: str = "issue.updated",
    version: str = "github:v1",
    observed_at: datetime = OBSERVED_AT,
) -> EventObservation:
    return EventObservation(
        identity=identity,
        event_type=event_type,
        event_schema_version=version,
        observed_at=observed_at,
        payload_digest=digest,
    )


def test_source_instance_separates_reused_event_keys() -> None:
    first = EventIdentity("github:org-one", "delivery-42")
    second = EventIdentity("github:org-two", "delivery-42")
    assert first != second
    assert len({first, second}) == 2


def test_identical_payload_digest_does_not_establish_event_identity() -> None:
    first = observation(EventIdentity("github:one", "delivery-1"))
    second = observation(EventIdentity("github:one", "delivery-2"))
    assert first.payload_digest == second.payload_digest
    assert first.identity != second.identity


def test_payload_change_does_not_change_explicit_event_identity() -> None:
    identity = EventIdentity("github:one", "delivery-1")
    first = observation(identity)
    changed = observation(identity, digest="sha256:" + "b" * 64)
    assert first.identity == changed.identity
    assert first != changed


def test_event_type_and_schema_version_are_independent_opaque_labels() -> None:
    identity = EventIdentity("local:runner-one", "task-3")
    first = observation(identity, event_type="task.completed", version="local:2")
    second = observation(identity, event_type="task.completed", version="local:3")
    assert first.event_type == "task.completed"
    assert first.event_schema_version == "local:2"
    assert second.event_schema_version == "local:3"
    assert first != second


def test_preserves_timezone_aware_observation_instead_of_normalizing_it() -> None:
    observed_at = datetime(2026, 11, 2, 14, 0, tzinfo=timezone(timedelta(hours=9)))
    record = observation(EventIdentity("local:runner", "task-3"), observed_at=observed_at)
    assert record.observed_at is observed_at


def test_observation_contains_no_raw_payload_or_durable_delivery_identifier() -> None:
    record = observation(EventIdentity("github:one", "delivery-1"))
    assert set(record.__dataclass_fields__) == {
        "identity",
        "event_type",
        "event_schema_version",
        "observed_at",
        "payload_digest",
    }
    assert not hasattr(record, "event_id")
    assert not hasattr(record, "payload")


def test_identity_and_observation_are_immutable() -> None:
    identity = EventIdentity("github:one", "delivery-1")
    record = observation(identity)
    with pytest.raises(FrozenInstanceError):
        identity.event_key = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.event_type = "other"  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", " ", " x", "x ", "x\nnext", "x\x7f"])
def test_identity_rejects_invalid_source_labels(value: str) -> None:
    with pytest.raises(ValueError):
        EventIdentity(value, "delivery")


@pytest.mark.parametrize("value", ["", " ", " key", "key ", "new\nline", "new\x7fline"])
def test_identity_rejects_invalid_event_keys(value: str) -> None:
    with pytest.raises(ValueError):
        EventIdentity("github:one", value)


@pytest.mark.parametrize("field", ["event_type", "event_schema_version"])
def test_observation_rejects_empty_event_labels(field: str) -> None:
    identity = EventIdentity("github:one", "delivery")
    with pytest.raises(ValueError):
        if field == "event_type":
            observation(identity, event_type="")
        else:
            observation(identity, version=" ")


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63, "md5:" + "a" * 32],
)
def test_observation_rejects_unqualified_or_malformed_digests(digest: str) -> None:
    with pytest.raises(ValueError):
        observation(EventIdentity("github:one", "delivery"), digest=digest)


def test_observation_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError):
        observation(EventIdentity("local:runner", "task"), observed_at=datetime(2026, 11, 2, 5, 0))


def test_value_objects_reject_runtime_type_mismatches() -> None:
    with pytest.raises(TypeError):
        EventIdentity(17, "delivery")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        observation("not an identity")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        observation(EventIdentity("github:one", "delivery"), observed_at="2026-11-02")  # type: ignore[arg-type]
