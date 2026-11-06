"""Q72 structural-plan invariants: pure and execution-free."""

import json
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from local_dev_mcp.tools.gh.delivery.operation_plan import OperationNode, PlanStructure

pytestmark = pytest.mark.unit


def node(
    key: str = "read_issue",
    *,
    depends_on: tuple[str, ...] = (),
    version: str = "read:v1",
) -> OperationNode:
    return OperationNode(
        operation_id=key,
        consumer_id="github_issue",
        operation_type="read_current_state",
        contract_version=version,
        depends_on=depends_on,
    )


def test_plan_fixes_graph_and_has_stable_canonical_digest() -> None:
    operations = (node(), node("prepare", depends_on=("read_issue",), version="prepare:v2"))
    first = PlanStructure(operations)
    second = PlanStructure(tuple(operations))
    assert first == second
    assert first.structure_digest == second.structure_digest
    assert len(first.structure_digest) == 71
    parsed = json.loads(first.canonical_bytes())
    assert parsed["structure_version"] == "b3s-plan:1"
    assert [op["operation_id"] for op in parsed["operations"]] == ["read_issue", "prepare"]
    assert parsed["operations"][1]["depends_on"] == ["read_issue"]
    assert "payload" not in parsed
    assert "intent" not in parsed
    assert "claim" not in parsed


def test_structural_digest_changes_when_any_structural_decision_changes() -> None:
    baseline = PlanStructure((node(), node("prepare", depends_on=("read_issue",))))
    changed = [
        PlanStructure((node("prepare", depends_on=("read_issue",)), node())),
        PlanStructure((node(version="read:v2"), node("prepare", depends_on=("read_issue",)))),
        PlanStructure((node(), node("prepare"))),
        PlanStructure((node(), node("other", depends_on=("read_issue",)))),
        PlanStructure((node(), node("prepare", depends_on=("read_issue",), version="prepare:v2"))),
    ]
    assert len({baseline.structure_digest, *(x.structure_digest for x in changed)}) == len(changed) + 1


def test_caller_list_cannot_mutate_graph_after_creation() -> None:
    source = [node()]
    plan = PlanStructure(tuple(source))
    source.append(node("other"))
    assert len(plan.operations) == 1
    with pytest.raises(FrozenInstanceError):
        plan.operations = (node("replacement"),)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.operations[0].contract_version = "new"  # type: ignore[misc]


@pytest.mark.parametrize(
    "ops",
    [
        (),
        (node("same"), node("same")),
        (node("read_issue", depends_on=("missing",)),),
        (node("read_issue", depends_on=("read_issue",)),),
        (node("one", depends_on=("two",)), node("two", depends_on=("one",))),
        (node("one", depends_on=("two",)), node("two", depends_on=("three",)), node("three", depends_on=("one",))),
    ],
)
def test_invalid_operation_graph_is_not_sealed(ops: tuple[OperationNode, ...]) -> None:
    with pytest.raises(ValueError):
        PlanStructure(ops)


def test_out_of_order_acyclic_graph_is_valid_without_implicit_reordering() -> None:
    plan = PlanStructure((node("downstream", depends_on=("upstream",)), node("upstream")))
    assert tuple(op.operation_id for op in plan.operations) == ("downstream", "upstream")


@pytest.mark.parametrize("bad", ["", " x", "bad/name", "x\n", "a" * 129])
def test_invalid_labels_fail_closed(bad: str) -> None:
    with pytest.raises(ValueError):
        node(bad)
    with pytest.raises(ValueError):
        OperationNode("valid", bad, "type", "v1")


def test_unknown_version_and_mutable_or_untyped_inputs_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        PlanStructure((node(),), structure_version=cast(Any, "b3s-plan:2"))
    with pytest.raises(ValueError):
        PlanStructure(cast(Any, [node()]))
    with pytest.raises(TypeError):
        PlanStructure((cast(Any, {"operation_id": "foo"}),))
    with pytest.raises(TypeError):
        OperationNode("x", "c", "t", "v1", depends_on=cast(Any, ["y"]))
    with pytest.raises(ValueError):
        OperationNode("x", "c", "t", "v1", depends_on=("y", "y"))


def test_decodes_only_valid_canonical_plan_evidence() -> None:
    plan = PlanStructure((node(), node("prepare", depends_on=("read_issue",))))
    assert PlanStructure.from_canonical_bytes(plan.canonical_bytes()) == plan


@pytest.mark.parametrize(
    "variant",
    ["extra_field", "version", "missing_fields", "missing_dependency", "cycle", "extra_node_field", "wrong_type"],
)
def test_rejects_invalid_serialized_plan_structure(variant: str) -> None:
    data: dict[str, Any] = json.loads(PlanStructure((node(),)).canonical_bytes())
    operations: list[dict[str, Any]] = data["operations"]
    first = operations[0]
    if variant == "extra_field":
        data["unknown"] = "field"
    elif variant == "version":
        data["structure_version"] = "b3s-plan:2"
    elif variant == "missing_fields":
        data["operations"] = [{"operation_id": "invalid"}]
    elif variant == "missing_dependency":
        first["depends_on"] = ["missing"]
    elif variant == "cycle":
        first["depends_on"] = ["read_issue"]
    elif variant == "extra_node_field":
        first["action"] = "delete"
    elif variant == "wrong_type":
        first["depends_on"] = "wrong"
    else:
        raise AssertionError("Unknown test variant.")
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        PlanStructure.from_canonical_bytes(raw)


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"{}",
        b"[]",
        b"not-json",
        b"\xff",
        b'{"structure_version":"b3s-plan:1","operations":[],"operations":[]}',
        b'{"structure_version":"b3s-plan:1","operations":NaN}',
        b" " + PlanStructure((node(),)).canonical_bytes(),
        PlanStructure((node(),)).canonical_bytes() + b" ",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_rejects_noncanonical_or_ambiguous_plan_bytes(bad: bytes) -> None:
    with pytest.raises(ValueError):
        PlanStructure.from_canonical_bytes(bad)
