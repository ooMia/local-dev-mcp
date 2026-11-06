"""Q71-Q73 pure Checkpoint/Lineage evidence, independent of live execution."""

import json
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest

from local_dev_mcp.tools.gh.delivery.operation_checkpoint import (
    CheckpointField,
    OperationCheckpoint,
)
from local_dev_mcp.tools.gh.delivery.operation_lineage import LineageBinding, OperationLineage

pytestmark = pytest.mark.unit
D = "sha256:" + "a" * 64
E = "sha256:" + "b" * 64


def checkpoint() -> OperationCheckpoint:
    return OperationCheckpoint(
        operation_id="read",
        intent_digest=D,
        output_schema_version="issue_read:v1",
        fields=(CheckpointField("status", E), CheckpointField("revision", D)),
        verification_contract="github_get_issue:v1",
        verification_evidence_digest=D,
    )


def binding() -> LineageBinding:
    c = checkpoint()
    return LineageBinding(
        source_operation_id="read",
        source_checkpoint_digest=c.checkpoint_digest,
        source_field_id="status",
        source_field_digest=E,
        source_schema_version=c.output_schema_version,
        target_field_id="desired_status",
        binding_version="issue_status:v1",
    )


def test_checkpoint_canonical_fields_and_digest_are_immutable() -> None:
    value = checkpoint()
    raw = value.canonical_bytes()
    assert OperationCheckpoint.from_canonical_bytes(raw) == value
    assert value.checkpoint_digest == OperationCheckpoint.from_canonical_bytes(raw).checkpoint_digest
    assert len(value.checkpoint_digest) == 71
    data = json.loads(raw)
    assert data["checkpoint_version"] == "b3s-checkpoint:1"
    assert data["fields"][0]["field_id"] == "status"
    assert "payload" not in data and "credentials" not in data
    with pytest.raises(FrozenInstanceError):
        value.intent_digest = E  # type: ignore[misc]
    assert replace(value, fields=()).checkpoint_digest != value.checkpoint_digest
    assert replace(value, verification_evidence_digest=E).checkpoint_digest != value.checkpoint_digest


def test_lineage_canonical_binding_is_explicit_and_digest_bound() -> None:
    lineage = OperationLineage((binding(),))
    raw = lineage.canonical_bytes()
    assert OperationLineage.from_canonical_bytes(raw) == lineage
    assert len(lineage.lineage_digest) == 71
    data = json.loads(raw)
    assert data["lineage_version"] == "b3s-lineage:1"
    assert data["bindings"][0]["source_field_id"] == "status"
    assert data["bindings"][0]["target_field_id"] == "desired_status"
    assert data["bindings"][0]["binding_version"] == "issue_status:v1"
    assert (
        replace(lineage, bindings=(replace(binding(), binding_version="issue_status:v2"),)).lineage_digest
        != lineage.lineage_digest
    )


@pytest.mark.parametrize("variant", ["duplicate_field", "mutable_fields", "invalid_digest", "invalid_version"])
def test_invalid_checkpoint_rejected(variant: str) -> None:
    value = checkpoint()
    if variant == "duplicate_field":
        with pytest.raises(ValueError, match="duplicated"):
            replace(value, fields=(CheckpointField("status", D), CheckpointField("status", E)))
    elif variant == "mutable_fields":
        with pytest.raises(TypeError):
            replace(value, fields=cast(Any, [CheckpointField("status", D)]))
    elif variant == "invalid_digest":
        with pytest.raises(ValueError):
            replace(value, verification_evidence_digest="SHA256:BAD")
    else:
        with pytest.raises(ValueError):
            replace(value, checkpoint_version=cast(Any, "b3s-checkpoint:2"))


def test_invalid_lineage_rejected() -> None:
    with pytest.raises(ValueError, match="duplicated"):
        OperationLineage((binding(), replace(binding(), source_field_id="revision")))
    with pytest.raises(ValueError):
        replace(binding(), source_field_digest="invalid")
    with pytest.raises(TypeError):
        OperationLineage(cast(Any, [binding()]))
    with pytest.raises(ValueError):
        OperationLineage((binding(),), lineage_version=cast(Any, "b3s-lineage:2"))


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"[]",
        b"{}",
        b"\xff",
        b"oops",
        b'{"checkpoint_version":"b3s-checkpoint:1","checkpoint_version":"b3s-checkpoint:1"}',
        b'{"checkpoint_version":NaN}',
        b" " + checkpoint().canonical_bytes(),
        checkpoint().canonical_bytes() + b" ",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_noncanonical_checkpoint_fails_closed(bad: bytes) -> None:
    with pytest.raises(ValueError):
        OperationCheckpoint.from_canonical_bytes(bad)


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"[]",
        b"{}",
        b"\xff",
        b"oops",
        b'{"lineage_version":"b3s-lineage:1","lineage_version":"b3s-lineage:1"}',
        b'{"bindings":NaN}',
        b" " + OperationLineage((binding(),)).canonical_bytes(),
        OperationLineage((binding(),)).canonical_bytes() + b" ",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_noncanonical_lineage_fails_closed(bad: bytes) -> None:
    with pytest.raises(ValueError):
        OperationLineage.from_canonical_bytes(bad)


@pytest.mark.parametrize("kind", ["checkpoint", "lineage"])
@pytest.mark.parametrize("mutation", ["extra", "wrong_field_shape", "wrong_type", "new_version"])
def test_serialized_evidence_cannot_be_reinterpreted(kind: str, mutation: str) -> None:
    cls: type[OperationCheckpoint] | type[OperationLineage]
    if kind == "checkpoint":
        obj = checkpoint()
        cls = OperationCheckpoint
        data: dict[str, Any] = json.loads(obj.canonical_bytes())
        key, version = "checkpoint_version", "b3s-checkpoint:2"
        entries = "fields"
    else:
        obj = OperationLineage((binding(),))
        cls = OperationLineage
        data = json.loads(obj.canonical_bytes())
        key, version = "lineage_version", "b3s-lineage:2"
        entries = "bindings"
    if mutation == "extra":
        data["secret"] = "no"
    elif mutation == "wrong_field_shape":
        data[entries][0]["unexpected"] = True
    elif mutation == "wrong_type":
        data[entries] = {}
    else:
        data[key] = version
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        cls.from_canonical_bytes(raw)
