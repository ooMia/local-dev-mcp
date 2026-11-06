"""Q74-Q76: Consumer-owned contract declarations and strict deployment matching."""

from dataclasses import replace
from typing import Any, cast

import pytest

from local_dev_mcp.tools.gh.delivery.operation_checkpoint import OperationCheckpoint
from local_dev_mcp.tools.gh.delivery.operation_contracts import (
    ConcurrencyProtection,
    ConsumerOperationContract,
    ContractCatalog,
    require_version_change_for_protection_relaxation,
)
from local_dev_mcp.tools.gh.delivery.operation_intent import OperationIntent
from local_dev_mcp.tools.gh.delivery.operation_plan import OperationNode, PlanStructure

pytestmark = pytest.mark.unit
DIGEST = "sha256:" + "a" * 64


async def cannot_execute(_: OperationIntent) -> object:
    raise AssertionError("Preflight must never run execute.")


async def cannot_probe(_: OperationIntent) -> object:
    raise AssertionError("Preflight must never run probe.")


def cannot_decode(_: bytes) -> OperationCheckpoint:
    raise AssertionError("Preflight must never run decode.")


async def cannot_verify(_: OperationIntent, __: OperationCheckpoint) -> bool:
    raise AssertionError("Preflight must never run verify.")


async def cannot_lock(_: OperationIntent) -> object:
    raise AssertionError("Static preflight cannot establish runtime fencing.")


def node(operation_id: str = "read", version: str = "read:v2") -> OperationNode:
    return OperationNode(operation_id, "github_issue", "read_current", version)


def contract(
    version: str = "read:v2",
    *,
    protection: ConcurrencyProtection | None = None,
) -> ConsumerOperationContract:
    return ConsumerOperationContract(
        consumer_id="github_issue",
        operation_type="read_current",
        contract_version=version,
        retry_safety_rule="hold_without_probe",
        probe_contract="github_readonly:v1",
        concurrency=protection
        or ConcurrencyProtection(
            "remote_precondition:v1",
            "remote_precondition:v1",
            DIGEST,
            cannot_lock,
        ),
        execute=cannot_execute,
        probe=cannot_probe,
        decode=cannot_decode,
        verify=cannot_verify,
    )


def intent(operation: OperationNode) -> OperationIntent:
    return OperationIntent(
        operation=operation,
        input_digest=DIGEST,
        effect_digest=DIGEST,
        preconditions_digest=DIGEST,
        postconditions_digest=DIGEST,
        retry_safety_rule="hold_without_probe",
        probe_contract="github_readonly:v1",
    )


def test_empty_catalog_fails_closed_without_default_consumers() -> None:
    plan = PlanStructure((node(),))
    report = ContractCatalog().inspect_plan(plan)
    assert report.status == "hold"
    assert report.operations[0].reason == "unsupported_contract"
    assert not report.compatible


def test_exact_version_matches_without_invoking_hooks() -> None:
    catalog = ContractCatalog((contract(),))
    report = catalog.inspect_plan(PlanStructure((node(),)), intents=(intent(node()),))
    assert report.status == "compatible"
    assert report.operations[0].reason is None
    assert catalog.inspect(node(), intent=intent(node())).compatible
    # The above callbacks all raise if called. Compatibility is not an execution permit.


def test_old_delivery_never_falls_back_to_latest_code() -> None:
    catalog = ContractCatalog((contract("read:v2"),))
    previous = node("old", "read:v1")
    current = node("new", "read:v2")
    plan = PlanStructure((previous, current))
    status = catalog.inspect_plan(plan)
    assert status.status == "hold"
    assert [x.reason for x in status.operations] == ["unsupported_contract", None]
    assert catalog.inspect(previous, intent=intent(previous)).reason == "unsupported_contract"
    # Explicitly registering an old fully functional version is required.
    with_legacy = ContractCatalog((contract("read:v1"), contract("read:v2")))
    assert with_legacy.inspect_plan(plan).compatible


@pytest.mark.parametrize("hook", ["execute", "probe", "decode", "verify"])
def test_absent_callable_is_unsupported_contract(hook: str) -> None:
    incomplete = replace(contract(), **{hook: None})
    assert ContractCatalog((incomplete,)).inspect(node()).reason == "unsupported_contract"


@pytest.mark.parametrize(
    "protection",
    [
        ConcurrencyProtection("remote_precondition:v1", "local_lock:v1", DIGEST, cannot_lock),
        ConcurrencyProtection("remote_precondition:v1", "remote_precondition:v1", DIGEST, None),
    ],
)
def test_concurrency_not_verified_means_hold(protection: ConcurrencyProtection) -> None:
    assert ContractCatalog((contract(protection=protection),)).inspect(node()).reason == "concurrency_unverified"


def test_intent_retry_and_probe_mismatch_holds() -> None:
    catalog = ContractCatalog((contract(),))
    value = intent(node())
    assert catalog.inspect(node(), intent=replace(value, probe_contract="different:v1")).reason == "contract_mismatch"
    assert catalog.inspect(node(), intent=replace(value, retry_safety_rule="unsafe:v1")).reason == "contract_mismatch"
    assert catalog.inspect(node(), intent=replace(value, operation=node("other"))).reason == "contract_mismatch"


def test_every_sealed_operation_is_checked_even_without_durable_intent() -> None:
    a = node("a")
    b = OperationNode("b", "other_consumer", "update", "update:v1", ("a",))
    report = ContractCatalog((contract(),)).inspect_plan(PlanStructure((a, b)), intents=(intent(a),))
    assert report.status == "hold"
    assert len(report.operations) == 2
    assert report.operations[1].reason == "unsupported_contract"


def test_conflicts_and_bad_manifest_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        ContractCatalog((contract(), contract()))
    with pytest.raises(TypeError):
        ContractCatalog(cast(Any, [contract()]))
    with pytest.raises(TypeError):
        ContractCatalog((cast(Any, None),))
    with pytest.raises(TypeError):
        replace(contract(), execute=cast(Any, object()))
    with pytest.raises(ValueError):
        replace(contract(), contract_version="")
    with pytest.raises(ValueError):
        ConcurrencyProtection("remote:v1", "remote:v1", "not-a-digest", cannot_lock)
    with pytest.raises(ValueError):
        ContractCatalog((contract(),)).inspect_plan(
            PlanStructure((node(),)),
            intents=(intent(node()), intent(node())),
        )
    with pytest.raises(ValueError):
        ContractCatalog((contract(),)).inspect_plan(
            PlanStructure((node(),)),
            intents=(intent(node("unknown")),),
        )


def test_consumer_guard_changes_require_a_new_version() -> None:
    previous = contract("read:v1")
    weaker = replace(
        previous,
        concurrency=replace(previous.concurrency, required_level="local_only:v1", enforced_level="local_only:v1"),
    )
    with pytest.raises(ValueError, match="new Contract Version"):
        require_version_change_for_protection_relaxation(previous, weaker)
    new_version = replace(weaker, contract_version="read:v2")
    require_version_change_for_protection_relaxation(previous, new_version)
    # Old persisted Intents remain at v1; no implicit migration.
    assert ContractCatalog((new_version,)).inspect(node("old", "read:v1")).reason == "unsupported_contract"
