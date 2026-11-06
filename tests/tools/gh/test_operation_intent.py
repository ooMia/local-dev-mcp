"""Payload-free, immutable Operation Intent evidence (no DB or Job execution)."""

import json
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from local_dev_mcp.tools.gh.delivery.operation_intent import ConsumedOutput, OperationIntent
from local_dev_mcp.tools.gh.delivery.operation_plan import OperationNode

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64


def intent(*, dependencies: tuple[str, ...] = (), outputs: tuple[ConsumedOutput, ...] = ()) -> OperationIntent:
    return OperationIntent(
        operation=OperationNode("read", "github_issue", "read_current", "read:v1", dependencies),
        input_digest=DIGEST,
        effect_digest="sha256:" + "b" * 64,
        preconditions_digest="sha256:" + "c" * 64,
        postconditions_digest="sha256:" + "d" * 64,
        retry_safety_rule="hold_without_probe",
        probe_contract="github_readonly:v1",
        consumed_outputs=outputs,
    )


def test_canonical_intent_is_immutable_and_does_not_embed_payload() -> None:
    evidence = intent()
    raw = evidence.canonical_bytes()
    assert OperationIntent.from_canonical_bytes(raw) == evidence
    assert evidence.intent_digest.startswith("sha256:") and len(evidence.intent_digest) == 71
    data = json.loads(raw)
    assert data["operation"]["operation_id"] == "read"
    assert data["intent_version"] == "b3s-intent:1"
    assert data["consumed_outputs"] == []
    assert "payload" not in data and "secret" not in data and "arguments" not in data
    with pytest.raises(FrozenInstanceError):
        evidence.effect_digest = DIGEST  # type: ignore[misc]
    assert intent().canonical_bytes() == raw


def test_intent_fingerprint_covers_inputs_effects_and_contracts() -> None:
    first = intent()
    changed = [
        OperationIntent(
            operation=first.operation,
            input_digest="sha256:" + "f" * 64,
            effect_digest=first.effect_digest,
            preconditions_digest=first.preconditions_digest,
            postconditions_digest=first.postconditions_digest,
            retry_safety_rule=first.retry_safety_rule,
            probe_contract=first.probe_contract,
        ),
        OperationIntent(
            operation=OperationNode("read", "github_issue", "read_current", "read:v2"),
            input_digest=first.input_digest,
            effect_digest=first.effect_digest,
            preconditions_digest=first.preconditions_digest,
            postconditions_digest=first.postconditions_digest,
            retry_safety_rule=first.retry_safety_rule,
            probe_contract=first.probe_contract,
        ),
    ]
    assert len({first.intent_digest, *(value.intent_digest for value in changed)}) == 3


def test_consumed_output_lineage_is_explicit_and_exact() -> None:
    output = ConsumedOutput("source", DIGEST)
    accepted = intent(dependencies=("source",), outputs=(output,))
    assert OperationIntent.from_canonical_bytes(accepted.canonical_bytes()) == accepted
    with pytest.raises(ValueError, match="lineage"):
        intent(dependencies=("source",))
    with pytest.raises(ValueError, match="duplicated"):
        intent(dependencies=("source",), outputs=(output, output))
    with pytest.raises(ValueError, match="lineage"):
        intent(outputs=(output,))


@pytest.mark.parametrize("field", ["input_digest", "effect_digest", "preconditions_digest", "postconditions_digest"])
def test_invalid_digest_is_rejected(field: str) -> None:
    evidence = intent()
    args: dict[str, Any] = {
        "operation": evidence.operation,
        "input_digest": evidence.input_digest,
        "effect_digest": evidence.effect_digest,
        "preconditions_digest": evidence.preconditions_digest,
        "postconditions_digest": evidence.postconditions_digest,
        "retry_safety_rule": evidence.retry_safety_rule,
        "probe_contract": evidence.probe_contract,
    }
    args[field] = "SHA256:" + "F" * 64
    with pytest.raises(ValueError, match="digest"):
        OperationIntent(**args)


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"{}",
        b"[]",
        b"oops",
        b"\xff",
        b'{"intent_version":"b3s-intent:1","intent_version":"b3s-intent:1"}',
        b'{"intent_version":NaN}',
        b"x" * (1024 * 1024 + 1),
        b" ",
    ],
)
def test_invalid_serialized_evidence_fails_closed(bad: bytes) -> None:
    with pytest.raises(ValueError):
        OperationIntent.from_canonical_bytes(bad)


@pytest.mark.parametrize("mutation", ["extra", "version", "bad_digest", "wrong_dep", "extra_lineage", "op_type"])
def test_rejects_modified_evidence_even_if_valid_json(mutation: str) -> None:
    evidence = intent()
    data: dict[str, Any] = json.loads(evidence.canonical_bytes())
    if mutation == "extra":
        data["payload"] = {}
    elif mutation == "version":
        data["intent_version"] = "b3s-intent:future"
    elif mutation == "bad_digest":
        data["effect_digest"] = "not-digest"
    elif mutation == "wrong_dep":
        data["operation"]["depends_on"] = ["unknown"]
    elif mutation == "extra_lineage":
        data["consumed_outputs"] = [{"operation_id": "unknown", "checkpoint_digest": DIGEST}]
    else:
        data["operation"]["operation_id"] = 12
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError):
        OperationIntent.from_canonical_bytes(raw)


def test_rejects_noncanonical_json_and_mutable_values() -> None:
    raw = intent().canonical_bytes()
    with pytest.raises(ValueError, match="Noncanonical"):
        OperationIntent.from_canonical_bytes(raw + b" ")
    with pytest.raises(TypeError):
        intent().__class__(
            operation=intent().operation,
            input_digest=DIGEST,
            effect_digest=DIGEST,
            preconditions_digest=DIGEST,
            postconditions_digest=DIGEST,
            retry_safety_rule="hold",
            probe_contract="read:v1",
            consumed_outputs=cast(Any, []),
        )
