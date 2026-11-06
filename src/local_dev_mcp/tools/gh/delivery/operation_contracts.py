"""Q74–Q76 Consumer-owned versioned Operation Contract compatibility.

This catalog inspects registered callable capabilities and declared concurrency
protection; it NEVER invokes execute, probe, decode, verify or the guard.
Compatibility is NOT a runtime lease, proven remote fencing, or approval to
retry an uncertain GitHub effect. No default/fallback Consumer is registered.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ._canonical import checked_digest, checked_label
from .operation_checkpoint import OperationCheckpoint
from .operation_intent import OperationIntent
from .operation_plan import OperationNode, PlanStructure

ExecutionHook = Callable[[OperationIntent], Awaitable[object]]
ProbeHook = Callable[[OperationIntent], Awaitable[object]]
DecoderHook = Callable[[bytes], OperationCheckpoint]
VerifierHook = Callable[[OperationIntent, OperationCheckpoint], Awaitable[bool]]
ConcurrencyGuardHook = Callable[[OperationIntent], Awaitable[object]]

HoldReason = Literal["unsupported_contract", "concurrency_unverified", "contract_mismatch"]


@dataclass(frozen=True, slots=True)
class ConcurrencyProtection:
    """Consumer's declared required/enforced guarantee and test evidence.

    Levels are opaque, consumer-owned versioned identifiers. The generic layer
    never assumes that one named level subsumes another. An actual operation
    runner must enforce and verify this guarantee again at execution time.
    """

    required_level: str
    enforced_level: str
    test_evidence_digest: str
    guard: ConcurrencyGuardHook | None

    def __post_init__(self) -> None:
        checked_label(self.required_level, "required_level")
        checked_label(self.enforced_level, "enforced_level")
        checked_digest(self.test_evidence_digest, "test_evidence_digest")
        if self.guard is not None and not callable(self.guard):
            raise TypeError("Concurrency guard must be callable.")

    @property
    def declared_compatible(self) -> bool:
        return self.required_level == self.enforced_level and self.guard is not None


@dataclass(frozen=True, slots=True)
class ConsumerOperationContract:
    """Explicit callable registration owned by the implementing Consumer.

    The contract version is a closed-world exact match. Implementations for
    newer versions do not automatically decode, probe or verify old Intents.
    """

    consumer_id: str
    operation_type: str
    contract_version: str
    retry_safety_rule: str
    probe_contract: str
    concurrency: ConcurrencyProtection
    execute: ExecutionHook | None
    probe: ProbeHook | None
    decode: DecoderHook | None
    verify: VerifierHook | None

    def __post_init__(self) -> None:
        for field in (
            "consumer_id",
            "operation_type",
            "contract_version",
            "retry_safety_rule",
            "probe_contract",
        ):
            checked_label(getattr(self, field), field)
        if type(self.concurrency) is not ConcurrencyProtection:
            raise TypeError("Missing versioned Concurrency Protection.")
        for field in ("execute", "probe", "decode", "verify"):
            hook = getattr(self, field)
            if hook is not None and not callable(hook):
                raise TypeError(f"{field} must be callable.")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.consumer_id, self.operation_type, self.contract_version

    @property
    def has_all_capabilities(self) -> bool:
        return all(getattr(self, name) is not None for name in ("execute", "probe", "decode", "verify"))


@dataclass(frozen=True, slots=True)
class ContractObservation:
    operation_id: str
    consumer_id: str
    operation_type: str
    contract_version: str
    status: Literal["compatible", "hold"]
    reason: HoldReason | None

    @property
    def compatible(self) -> bool:
        return self.status == "compatible"


@dataclass(frozen=True, slots=True)
class DeploymentCompatibility:
    status: Literal["compatible", "hold"]
    operations: tuple[ContractObservation, ...]

    @property
    def compatible(self) -> bool:
        return self.status == "compatible"


@dataclass(frozen=True, slots=True)
class ContractCatalog:
    """Immutable explicit inventory, with no automatic legacy-version fallback."""

    contracts: tuple[ConsumerOperationContract, ...] = ()

    def __post_init__(self) -> None:
        if type(self.contracts) is not tuple:
            raise TypeError("Consumer Contract registrations must be a tuple.")
        seen: set[tuple[str, str, str]] = set()
        for contract in self.contracts:
            if type(contract) is not ConsumerOperationContract:
                raise TypeError("Unsupported Consumer Contract registration.")
            if contract.key in seen:
                raise ValueError("Duplicate Consumer Operation Contract version.")
            seen.add(contract.key)

    def inspect(self, operation: OperationNode, *, intent: OperationIntent | None = None) -> ContractObservation:
        """Static compatibility only; no hook is ever invoked."""
        if type(operation) is not OperationNode:
            raise TypeError("Expected a validated OperationNode.")
        if intent is not None and type(intent) is not OperationIntent:
            raise TypeError("Expected a validated OperationIntent.")
        key = operation.consumer_id, operation.operation_type, operation.contract_version
        contract = next((item for item in self.contracts if item.key == key), None)
        reason: HoldReason | None = None
        if contract is None or not contract.has_all_capabilities:
            reason = "unsupported_contract"
        elif not contract.concurrency.declared_compatible:
            reason = "concurrency_unverified"
        elif intent is not None and (
            intent.operation != operation
            or intent.retry_safety_rule != contract.retry_safety_rule
            or intent.probe_contract != contract.probe_contract
        ):
            reason = "contract_mismatch"
        return ContractObservation(
            operation_id=operation.operation_id,
            consumer_id=operation.consumer_id,
            operation_type=operation.operation_type,
            contract_version=operation.contract_version,
            status="compatible" if reason is None else "hold",
            reason=reason,
        )

    def inspect_plan(
        self, plan: PlanStructure, *, intents: tuple[OperationIntent, ...] = ()
    ) -> DeploymentCompatibility:
        """Check all plan nodes and any existing Intents, never just a prefix."""
        if type(plan) is not PlanStructure or type(intents) is not tuple:
            raise TypeError("Expected immutable Plan and Intent tuple.")
        by_id: dict[str, OperationIntent] = {}
        for intent in intents:
            if type(intent) is not OperationIntent or intent.operation.operation_id in by_id:
                raise ValueError("Duplicate or invalid existing Intent.")
            by_id[intent.operation.operation_id] = intent
        if set(by_id) - {node.operation_id for node in plan.operations}:
            raise ValueError("Existing Intent is not in the sealed Plan.")
        observations = tuple(
            self.inspect(operation, intent=by_id.get(operation.operation_id)) for operation in plan.operations
        )
        return DeploymentCompatibility(
            status="compatible" if all(item.compatible for item in observations) else "hold",
            operations=observations,
        )


def require_version_change_for_protection_relaxation(
    previous: ConsumerOperationContract, replacement: ConsumerOperationContract
) -> None:
    """Reject reuse of the same Contract Version after a weaker declaration.

    No global partial order exists for consumer-specific guarantee levels.
    Any change to required/enforced level in a replacement requires a new
    Contract Version; existing persisted Intents are never rewritten.
    """
    if type(previous) is not ConsumerOperationContract or type(replacement) is not ConsumerOperationContract:
        raise TypeError("Expected validated Consumer Contracts.")
    if previous.consumer_id != replacement.consumer_id or previous.operation_type != replacement.operation_type:
        raise ValueError("Contract transition must target the same Consumer and operation type.")
    if previous.contract_version == replacement.contract_version and (
        previous.concurrency.required_level != replacement.concurrency.required_level
        or previous.concurrency.enforced_level != replacement.concurrency.enforced_level
    ):
        raise ValueError("Concurrency protection change requires a new Contract Version.")
