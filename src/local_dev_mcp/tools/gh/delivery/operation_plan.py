"""Pure, immutable structure of a GitHub automation plan, not an execution permit.

Q72: the graph (IDs, types, dependencies, versioned contracts) is fixed
before execution. Q70: operation Intents are separate, later durable values.
Nothing in this module claims jobs, persists a plan or authorizes mutations.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from ._canonical import decode_json

PLAN_STRUCTURE_VERSION: Literal["b3s-plan:1"] = "b3s-plan:1"
MAX_CANONICAL_PLAN_BYTES = 1024 * 1024


def _string(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string.")
    return value


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _label(value: object, field: str) -> str:
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded, nonempty identifier.")
    return value


@dataclass(frozen=True, slots=True)
class OperationNode:
    """One operation in a fixed graph; no inputs, effects or mutable outputs."""

    operation_id: str
    consumer_id: str
    operation_type: str
    contract_version: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("operation_id", "consumer_id", "operation_type", "contract_version"):
            _label(getattr(self, name), name)
        if type(self.depends_on) is not tuple:
            raise TypeError("dependencies must be a tuple.")
        for dependency in self.depends_on:
            _label(dependency, "dependency")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Duplicate dependency in one operation.")


@dataclass(frozen=True, slots=True)
class PlanStructure:
    """Sealed *in-memory* graph, still requiring durable sealing before use.

    The supplied operation order is preserved in the digest. Consumers must
    not reorder it or reinterpret unknown contract versions during recovery.
    """

    operations: tuple[OperationNode, ...]
    structure_version: Literal["b3s-plan:1"] = PLAN_STRUCTURE_VERSION

    def __post_init__(self) -> None:
        if self.structure_version != PLAN_STRUCTURE_VERSION:
            raise ValueError("Unsupported plan structure version.")
        if type(self.operations) is not tuple or not self.operations:
            raise ValueError("A plan requires a nonempty tuple of operations.")
        ids: set[str] = set()
        for operation in self.operations:
            if type(operation) is not OperationNode:
                raise TypeError("All operations must be OperationNode values.")
            if operation.operation_id in ids:
                raise ValueError("Duplicate operation ID.")
            ids.add(operation.operation_id)

        # Cycles and unknown dependencies fail closed; the graph is never
        # implicitly repaired, truncated or re-ordered.
        graph = {operation.operation_id: operation.depends_on for operation in self.operations}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key not in graph:
                raise ValueError("Unresolved operation dependency.")
            if key in visiting:
                raise ValueError("Cyclic operation dependencies.")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)

    def canonical_bytes(self) -> bytes:
        """Deterministic UTF-8 representation, excluding Intent and Payload."""
        data = {
            "structure_version": self.structure_version,
            "operations": [
                {
                    "operation_id": node.operation_id,
                    "consumer_id": node.consumer_id,
                    "operation_type": node.operation_type,
                    "contract_version": node.contract_version,
                    "depends_on": list(node.depends_on),
                }
                for node in self.operations
            ],
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> PlanStructure:
        """Decode only a recognized, structurally valid, canonical plan.

        A matching SHA-256 alone does not authenticate the contents as a Plan.
        Unknown fields, duplicate JSON keys and alternate encodings fail closed.
        """
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_CANONICAL_PLAN_BYTES:
            raise ValueError("Invalid canonical plan length.")
        try:
            parsed: object = decode_json(
                raw, duplicate_message="Duplicate JSON key in plan.", constant_message="Nonfinite value in plan."
            )
        except (UnicodeError, RecursionError) as error:
            raise ValueError("Invalid canonical plan serialization.") from error
        if type(parsed) is not dict:
            raise ValueError("Plan must be a JSON object.")
        value = cast(dict[str, object], parsed)
        if set(value) != {"structure_version", "operations"}:
            raise ValueError("Unexpected plan structure fields.")
        if value["structure_version"] != PLAN_STRUCTURE_VERSION:
            raise ValueError("Unsupported plan structure version.")
        records_raw = value["operations"]
        if type(records_raw) is not list or not records_raw:
            raise ValueError("Plan operations must be a nonempty array.")

        operations: list[OperationNode] = []
        expected = {"operation_id", "consumer_id", "operation_type", "contract_version", "depends_on"}
        for item in cast(list[object], records_raw):
            if type(item) is not dict:
                raise ValueError("Invalid operation fields.")
            record = cast(dict[str, object], item)
            if set(record) != expected:
                raise ValueError("Invalid operation fields.")
            dependencies_raw = record["depends_on"]
            if type(dependencies_raw) is not list:
                raise ValueError("Operation dependencies must be an array.")
            operations.append(
                OperationNode(
                    operation_id=_string(record["operation_id"], "operation_id"),
                    consumer_id=_string(record["consumer_id"], "consumer_id"),
                    operation_type=_string(record["operation_type"], "operation_type"),
                    contract_version=_string(record["contract_version"], "contract_version"),
                    depends_on=tuple(_string(dep, "dependency") for dep in cast(list[object], dependencies_raw)),
                )
            )
        try:
            plan = cls(tuple(operations))
        except RecursionError as error:
            raise ValueError("Plan dependency graph is too deep.") from error
        if plan.canonical_bytes() != raw:
            raise ValueError("Noncanonical plan representation.")
        return plan

    @property
    def structure_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()
