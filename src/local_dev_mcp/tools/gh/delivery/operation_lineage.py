"""Immutable field-level lineage binding a dependent Intent to Checkpoints.

Each edge names the exact producing operation, checkpoint, output field and
schema, plus its consuming input field and mapping-contract version. This is
provenance evidence, not authorization to execute the dependent operation.
"""

from dataclasses import dataclass
from typing import Literal, cast

from ._canonical import checked_digest, checked_label, digest_bytes, encode, read_object

LINEAGE_VERSION: Literal["b3s-lineage:1"] = "b3s-lineage:1"
MAX_LINEAGE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LineageBinding:
    source_operation_id: str
    source_checkpoint_digest: str
    source_field_id: str
    source_field_digest: str
    source_schema_version: str
    target_field_id: str
    binding_version: str

    def __post_init__(self) -> None:
        for key in (
            "source_operation_id",
            "source_field_id",
            "source_schema_version",
            "target_field_id",
            "binding_version",
        ):
            checked_label(getattr(self, key), key)
        checked_digest(self.source_checkpoint_digest, "source_checkpoint_digest")
        checked_digest(self.source_field_digest, "source_field_digest")


@dataclass(frozen=True, slots=True)
class OperationLineage:
    bindings: tuple[LineageBinding, ...]
    lineage_version: Literal["b3s-lineage:1"] = LINEAGE_VERSION

    def __post_init__(self) -> None:
        if self.lineage_version != LINEAGE_VERSION:
            raise ValueError("Unsupported Lineage version.")
        if type(self.bindings) is not tuple:
            raise TypeError("bindings must be a tuple.")
        consumers: set[str] = set()
        for binding in self.bindings:
            if type(binding) is not LineageBinding or binding.target_field_id in consumers:
                raise ValueError("Invalid or duplicated target input binding.")
            consumers.add(binding.target_field_id)

    def canonical_bytes(self) -> bytes:
        return encode(
            {
                "lineage_version": self.lineage_version,
                "bindings": [
                    {
                        "source_operation_id": b.source_operation_id,
                        "source_checkpoint_digest": b.source_checkpoint_digest,
                        "source_field_id": b.source_field_id,
                        "source_field_digest": b.source_field_digest,
                        "source_schema_version": b.source_schema_version,
                        "target_field_id": b.target_field_id,
                        "binding_version": b.binding_version,
                    }
                    for b in self.bindings
                ],
            },
            MAX_LINEAGE_BYTES,
        )

    @property
    def lineage_digest(self) -> str:
        return digest_bytes(self.canonical_bytes())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "OperationLineage":
        value = read_object(raw, MAX_LINEAGE_BYTES, {"lineage_version", "bindings"})
        if value["lineage_version"] != LINEAGE_VERSION or type(value["bindings"]) is not list:
            raise ValueError("Unsupported Lineage fields or version.")
        bindings: list[LineageBinding] = []
        expected = {
            "source_operation_id",
            "source_checkpoint_digest",
            "source_field_id",
            "source_field_digest",
            "source_schema_version",
            "target_field_id",
            "binding_version",
        }
        for item in cast(list[object], value["bindings"]):
            if type(item) is not dict:
                raise ValueError("Invalid Lineage binding.")
            record = cast(dict[str, object], item)
            if set(record) != expected:
                raise ValueError("Unsupported Lineage binding fields.")
            bindings.append(
                LineageBinding(
                    source_operation_id=checked_label(record["source_operation_id"], "source_operation_id"),
                    source_checkpoint_digest=checked_digest(
                        record["source_checkpoint_digest"], "source_checkpoint_digest"
                    ),
                    source_field_id=checked_label(record["source_field_id"], "source_field_id"),
                    source_field_digest=checked_digest(record["source_field_digest"], "source_field_digest"),
                    source_schema_version=checked_label(record["source_schema_version"], "source_schema_version"),
                    target_field_id=checked_label(record["target_field_id"], "target_field_id"),
                    binding_version=checked_label(record["binding_version"], "binding_version"),
                )
            )
        instance = cls(tuple(bindings))
        if instance.canonical_bytes() != raw:
            raise ValueError("Noncanonical Lineage bytes.")
        return instance
