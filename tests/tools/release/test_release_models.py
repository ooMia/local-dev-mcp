"""Declared compatibility decisions, cumulative causes and stable plan identities."""

from typing import Literal

import pytest
from pydantic import ValidationError

from local_dev_mcp.tools.release import Assessment, WorkflowConfig, build_plan
from local_dev_mcp.tools.release._models import ReleaseReceipt

pytestmark = pytest.mark.unit


def assessment(
    impact: Literal["major", "minor", "patch", "none"] = "minor", *, supersedes: tuple[str, ...] = ()
) -> Assessment:
    return Assessment(
        impact=impact,
        summary="Describe the change",
        reason="Existing public calls remain valid" if impact != "major" else "Existing required input changed",
        contract="MCP",
        assessed_by="agent",
        migration="Update the required input" if impact == "major" else "",
        supersedes=supersedes,
    )


@pytest.mark.parametrize(
    ("version", "impact", "expected"),
    [
        ("1.2.3", "major", "2.0.0"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("0.1.0", "major", "0.2.0"),
        ("0.1.0", "minor", "0.2.0"),
        ("0.1.0", "patch", "0.1.1"),
        ("1.2.3", "none", None),
    ],
)
def test_versions_follow_declared_stability_policy(
    version: str, impact: Literal["major", "minor", "patch", "none"], expected: str | None
) -> None:
    plan = build_plan("a" * 40, version, {"change": assessment(impact)})
    assert plan["next_version"] == expected
    assert plan["maximum_impact"] == impact
    assert plan["causes"] == ["change"]


def test_plan_explains_maximum_and_has_deterministic_identity() -> None:
    records = {"feature": assessment(), "fix": assessment("patch"), "break": assessment("major")}
    plan = build_plan("a" * 40, "1.2.3", records)
    assert plan["counts"] == {"major": 1, "minor": 1, "patch": 1, "none": 0}
    assert plan["causes"] == ["break"]
    assert plan["digest"] == build_plan("a" * 40, "1.2.3", dict(reversed(list(records.items()))))["digest"]
    assert plan["digest"] != build_plan("b" * 40, "1.2.3", records)["digest"]


def test_none_does_not_silently_exempt_missing_assessment() -> None:
    plan = build_plan("a" * 40, "1.0.0", {})
    assert plan["next_version"] is None
    assert plan["changes"] == []
    assert plan["causes"] == []


@pytest.mark.parametrize("field", ["summary", "reason", "contract"])
def test_blank_decisions_are_rejected(field: str) -> None:
    raw = assessment().model_dump()
    raw[field] = " \n "
    with pytest.raises(ValidationError):
        Assessment.model_validate(raw)


def test_major_requires_migration_and_unknown_fields_are_rejected() -> None:
    raw = assessment("major").model_dump()
    raw["migration"] = ""
    with pytest.raises(ValidationError):
        Assessment.model_validate(raw)
    with pytest.raises(ValidationError):
        Assessment.model_validate({**assessment().model_dump(), "auto_approve": True})


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "01.2.3", "1.2.3rc1", "1!1.2.3", "1.2.3+local"])
def test_only_canonical_three_part_versions_are_supported(version: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        build_plan("a" * 40, version, {"change": assessment()})


def test_explicit_supersession_preserves_original_but_changes_effective_maximum() -> None:
    plan = build_plan(
        "a" * 40,
        "1.0.0",
        {"old": assessment("major"), "correction": assessment("patch", supersedes=("old",))},
    )
    assert plan["maximum_impact"] == "patch"
    assert plan["causes"] == ["correction"]
    assert len(plan["changes"]) == 2
    assert plan["counts"]["major"] == 0


@pytest.mark.parametrize(
    "relations",
    [
        {"one": ("missing",)},
        {"one": ("one",)},
        {"one": ("two",), "two": ("one",)},
        {"one": (), "two": ("one",), "three": ("one",)},
    ],
)
def test_ambiguous_or_invalid_supersession_is_not_aggregated(relations: dict[str, tuple[str, ...]]) -> None:
    with pytest.raises(ValueError, match="supersed"):
        build_plan("a" * 40, "1.0.0", {key: assessment(supersedes=value) for key, value in relations.items()})


@pytest.mark.parametrize("value", [1, 1.0, "true"])
def test_workflow_opt_in_requires_actual_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        WorkflowConfig.model_validate({"enabled": value, "initial_base": "a" * 40})


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_receipt_schema_version_requires_actual_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        ReleaseReceipt.model_validate(
            {
                "schema_version": value,
                "source_head": "a" * 40,
                "plan_digest": "b" * 64,
                "old_version": "1.0.0",
                "new_version": "1.1.0",
                "consumed": [],
            }
        )
