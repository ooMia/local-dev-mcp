"""Immutable, payload-free evidence for one operation's output checkpoint.

No GitHub mutation or result verification occurs here. Evidence digests are
caller assertions; execution policy must independently validate their origin.
"""

from dataclasses import dataclass
from typing import Literal, cast

from ._canonical import checked_digest, checked_label, digest_bytes, encode, read_object

CHECKPOINT_VERSION: Literal["b3s-checkpoint:1"] = "b3s-checkpoint:1"
MAX_CHECKPOINT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CheckpointField:
    field_id: str
    value_digest: str

    def __post_init__(self) -> None:
        checked_label(self.field_id, "field_id")
        checked_digest(self.value_digest, "value_digest")


@dataclass(frozen=True, slots=True)
class OperationCheckpoint:
    operation_id: str
    intent_digest: str
    output_schema_version: str
    fields: tuple[CheckpointField, ...]
    verification_contract: str
    verification_evidence_digest: str
    checkpoint_version: Literal["b3s-checkpoint:1"] = CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        if self.checkpoint_version != CHECKPOINT_VERSION:
            raise ValueError("Unsupported Checkpoint version.")
        checked_label(self.operation_id, "operation_id")
        checked_digest(self.intent_digest, "intent_digest")
        checked_label(self.output_schema_version, "output_schema_version")
        checked_label(self.verification_contract, "verification_contract")
        checked_digest(self.verification_evidence_digest, "verification_evidence_digest")
        if type(self.fields) is not tuple:
            raise TypeError("fields must be an immutable tuple.")
        seen: set[str] = set()
        for field in self.fields:
            if type(field) is not CheckpointField or field.field_id in seen:
                raise ValueError("Invalid or duplicated Checkpoint output field.")
            seen.add(field.field_id)

    def canonical_bytes(self) -> bytes:
        return encode(
            {
                "checkpoint_version": self.checkpoint_version,
                "operation_id": self.operation_id,
                "intent_digest": self.intent_digest,
                "output_schema_version": self.output_schema_version,
                "fields": [{"field_id": f.field_id, "value_digest": f.value_digest} for f in self.fields],
                "verification_contract": self.verification_contract,
                "verification_evidence_digest": self.verification_evidence_digest,
            },
            MAX_CHECKPOINT_BYTES,
        )

    @property
    def checkpoint_digest(self) -> str:
        return digest_bytes(self.canonical_bytes())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "OperationCheckpoint":
        value = read_object(
            raw,
            MAX_CHECKPOINT_BYTES,
            {
                "checkpoint_version",
                "operation_id",
                "intent_digest",
                "output_schema_version",
                "fields",
                "verification_contract",
                "verification_evidence_digest",
            },
        )
        if value["checkpoint_version"] != CHECKPOINT_VERSION or type(value["fields"]) is not list:
            raise ValueError("Unsupported Checkpoint fields or version.")
        fields: list[CheckpointField] = []
        for item in cast(list[object], value["fields"]):
            if type(item) is not dict:
                raise ValueError("Invalid output field.")
            record = cast(dict[str, object], item)
            if set(record) != {"field_id", "value_digest"}:
                raise ValueError("Invalid output field shape.")
            fields.append(
                CheckpointField(
                    checked_label(record["field_id"], "field_id"),
                    checked_digest(record["value_digest"], "value_digest"),
                )
            )
        instance = cls(
            operation_id=checked_label(value["operation_id"], "operation_id"),
            intent_digest=checked_digest(value["intent_digest"], "intent_digest"),
            output_schema_version=checked_label(value["output_schema_version"], "output_schema_version"),
            fields=tuple(fields),
            verification_contract=checked_label(value["verification_contract"], "verification_contract"),
            verification_evidence_digest=checked_digest(
                value["verification_evidence_digest"], "verification_evidence_digest"
            ),
        )
        if instance.canonical_bytes() != raw:
            raise ValueError("Noncanonical Checkpoint bytes.")
        return instance
