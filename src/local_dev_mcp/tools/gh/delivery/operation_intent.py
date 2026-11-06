"""Immutable, payload-free evidence for a single versioned Operation Intent.

Digests identify caller-owned canonical input/effect/pre-/postcondition evidence.
They do not preserve raw Webhook Payload, credentials, API arguments, or the
evidence needed to evaluate execution and retry safety.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from ._canonical import decode_json
from .operation_plan import OperationNode

INTENT_VERSION: Literal["b3s-intent:1"] = "b3s-intent:1"
MAX_INTENT_BYTES = 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a qualified SHA-256 digest.")
    return value


def _label(value: object, field: str) -> str:
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded contract identifier.")
    return value


@dataclass(frozen=True, slots=True)
class ConsumedOutput:
    operation_id: str
    checkpoint_digest: str

    def __post_init__(self) -> None:
        _label(self.operation_id, "operation_id")
        _digest(self.checkpoint_digest, "checkpoint_digest")


@dataclass(frozen=True, slots=True)
class OperationIntent:
    operation: OperationNode
    input_digest: str
    effect_digest: str
    preconditions_digest: str
    postconditions_digest: str
    retry_safety_rule: str
    probe_contract: str
    consumed_outputs: tuple[ConsumedOutput, ...] = ()
    intent_version: Literal["b3s-intent:1"] = INTENT_VERSION

    def __post_init__(self) -> None:
        if self.intent_version != INTENT_VERSION:
            raise ValueError("Unsupported Intent version.")
        if type(self.operation) is not OperationNode:
            raise TypeError("operation must be an OperationNode.")
        for field in ("input_digest", "effect_digest", "preconditions_digest", "postconditions_digest"):
            _digest(getattr(self, field), field)
        _label(self.retry_safety_rule, "retry_safety_rule")
        _label(self.probe_contract, "probe_contract")
        if type(self.consumed_outputs) is not tuple:
            raise TypeError("consumed_outputs must be a tuple.")
        seen: set[str] = set()
        for output in self.consumed_outputs:
            if type(output) is not ConsumedOutput or output.operation_id in seen:
                raise ValueError("Invalid or duplicated consumed output.")
            seen.add(output.operation_id)
        if seen != set(self.operation.depends_on):
            raise ValueError("Consumed Output lineage must match all Operation dependencies.")

    def canonical_bytes(self) -> bytes:
        operation = self.operation
        value = {
            "intent_version": self.intent_version,
            "operation": {
                "operation_id": operation.operation_id,
                "consumer_id": operation.consumer_id,
                "operation_type": operation.operation_type,
                "contract_version": operation.contract_version,
                "depends_on": list(operation.depends_on),
            },
            "input_digest": self.input_digest,
            "effect_digest": self.effect_digest,
            "preconditions_digest": self.preconditions_digest,
            "postconditions_digest": self.postconditions_digest,
            "retry_safety_rule": self.retry_safety_rule,
            "probe_contract": self.probe_contract,
            "consumed_outputs": [
                {"operation_id": output.operation_id, "checkpoint_digest": output.checkpoint_digest}
                for output in self.consumed_outputs
            ],
        }
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(result) > MAX_INTENT_BYTES:
            raise ValueError("Intent evidence exceeds size limit.")
        return result

    @property
    def intent_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "OperationIntent":
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_INTENT_BYTES:
            raise ValueError("Invalid Intent bytes.")
        try:
            parsed: object = decode_json(
                raw, duplicate_message="Duplicate key in Intent.", constant_message="Nonfinite value in Intent."
            )
        except (UnicodeError, RecursionError) as error:
            raise ValueError("Invalid Intent encoding.") from error
        if type(parsed) is not dict:
            raise ValueError("Intent must be an object.")
        value = cast(dict[str, object], parsed)
        keys = {
            "intent_version",
            "operation",
            "input_digest",
            "effect_digest",
            "preconditions_digest",
            "postconditions_digest",
            "retry_safety_rule",
            "probe_contract",
            "consumed_outputs",
        }
        if set(value) != keys or value["intent_version"] != INTENT_VERSION:
            raise ValueError("Unsupported Intent fields or version.")
        raw_op = value["operation"]
        if type(raw_op) is not dict:
            raise ValueError("Invalid Intent operation.")
        op = cast(dict[str, object], raw_op)
        if set(op) != {"operation_id", "consumer_id", "operation_type", "contract_version", "depends_on"}:
            raise ValueError("Invalid Intent operation fields.")
        dependencies = op["depends_on"]
        if type(dependencies) is not list:
            raise ValueError("Invalid Intent dependencies.")
        operation = OperationNode(
            operation_id=_label(op["operation_id"], "operation_id"),
            consumer_id=_label(op["consumer_id"], "consumer_id"),
            operation_type=_label(op["operation_type"], "operation_type"),
            contract_version=_label(op["contract_version"], "contract_version"),
            depends_on=tuple(_label(item, "dependency") for item in cast(list[object], dependencies)),
        )
        outputs = value["consumed_outputs"]
        if type(outputs) is not list:
            raise ValueError("Invalid lineage.")
        lineage: list[ConsumedOutput] = []
        for item in cast(list[object], outputs):
            if type(item) is not dict:
                raise ValueError("Invalid lineage record.")
            data = cast(dict[str, object], item)
            if set(data) != {"operation_id", "checkpoint_digest"}:
                raise ValueError("Invalid lineage fields.")
            lineage.append(
                ConsumedOutput(
                    operation_id=_label(data["operation_id"], "operation_id"),
                    checkpoint_digest=_digest(data["checkpoint_digest"], "checkpoint_digest"),
                )
            )
        intent = cls(
            operation=operation,
            input_digest=_digest(value["input_digest"], "input_digest"),
            effect_digest=_digest(value["effect_digest"], "effect_digest"),
            preconditions_digest=_digest(value["preconditions_digest"], "preconditions_digest"),
            postconditions_digest=_digest(value["postconditions_digest"], "postconditions_digest"),
            retry_safety_rule=_label(value["retry_safety_rule"], "retry_safety_rule"),
            probe_contract=_label(value["probe_contract"], "probe_contract"),
            consumed_outputs=tuple(lineage),
        )
        if intent.canonical_bytes() != raw:
            raise ValueError("Noncanonical Intent bytes.")
        return intent
