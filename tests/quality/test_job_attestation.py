"""Quality evidence cannot be replayed with a different job scope or origin."""

from copy import deepcopy
from typing import Any, cast

import pytest
from fastmcp.exceptions import ToolError

from local_dev_mcp.quality.job_validation import validate_quality_job_sequence
from local_dev_mcp.settings import JobDefinition
from local_dev_mcp.tools.tasks._response import TaskResult

pytestmark = pytest.mark.unit

RECIPE_LINT = JobDefinition("lint", "lint", "task.lint", ("uv", "run", "ruff", "check"))
RECIPE_TEST = JobDefinition(
    "unit", "test", "task.unit", ("uv", "run", "pytest"), level="unit", default_targets=("tests",)
)


def _job(name: str, *, kind: str = "lint", controller: str = "main") -> TaskResult:
    result: dict[str, Any] = {
        "job_id": name + "-id",
        "request_id": name,
        "task": name,
        "kind": kind,
        "level": "unit" if kind == "test" else None,
        "cwd": ".",
        "state": "succeeded",
        "exit_code": 0,
        "termination_confirmed": True,
        "output_complete": True,
        "started_at": "2026-11-12T14:00:00+00:00" if name == "lint" else "2026-11-12T14:01:00+00:00",
        "finished_at": "2026-11-12T14:00:30+00:00" if name == "lint" else "2026-11-12T14:01:30+00:00",
        "all_tests": kind == "test",
        "targets": ["tests"] if kind == "test" else [],
        "worktree": {
            "backend": "worktrunk",
            "path": ".lab/wt/topic",
            "branch": "feat/topic",
            "head": "a" * 40,
            "original_branch": controller,
            "original_head": "b" * 40,
            "verified": True,
        },
    }
    return cast(TaskResult, result)


def _verify(jobs: dict[str, TaskResult]) -> tuple[tuple[str, str], ...]:
    return validate_quality_job_sequence(
        (RECIPE_LINT, RECIPE_TEST),
        ("lint-id", "unit-id"),
        jobs.__getitem__,
        branch="feat/topic",
        source_head="a" * 40,
        worktree_path=".lab/wt/topic",
    )


def test_complete_quality_jobs_are_accepted() -> None:
    assert _verify({"lint-id": _job("lint"), "unit-id": _job("unit", kind="test")}) == (
        ("lint", "lint-id"),
        ("unit", "unit-id"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "test"),
        ("level", "integration"),
        ("cwd", "other"),
        ("output_complete", False),
        ("state", "failed"),
        ("termination_confirmed", False),
    ],
)
def test_job_metadata_tampering_is_rejected(field: str, value: object) -> None:
    jobs = {"lint-id": _job("lint"), "unit-id": _job("unit", kind="test")}
    jobs["lint-id"][field] = value  # type: ignore[literal-required]
    with pytest.raises(ToolError):
        _verify(jobs)


def test_partial_test_scope_is_rejected() -> None:
    jobs = {"lint-id": _job("lint"), "unit-id": _job("unit", kind="test")}
    jobs["unit-id"]["all_tests"] = False
    with pytest.raises(ToolError, match="complete configured target"):
        _verify(jobs)


def test_cross_controller_job_is_rejected() -> None:
    jobs = {"lint-id": _job("lint"), "unit-id": _job("unit", kind="test", controller="other")}
    with pytest.raises(ToolError, match="different controller"):
        _verify(jobs)


def test_reordered_job_times_are_rejected() -> None:
    jobs = {"lint-id": _job("lint"), "unit-id": _job("unit", kind="test")}
    jobs["unit-id"]["started_at"] = "2026-11-12T13:00:00+00:00"
    with pytest.raises(ToolError, match="ordered"):
        _verify(jobs)


def test_wrong_source_and_duplicate_jobs_are_rejected() -> None:
    jobs = {"lint-id": _job("lint"), "unit-id": _job("unit", kind="test")}
    altered = deepcopy(jobs)
    altered["lint-id"]["worktree"]["head"] = "f" * 40  # type: ignore[index]
    with pytest.raises(ToolError):
        _verify(altered)
    with pytest.raises(ToolError, match="distinct"):
        validate_quality_job_sequence(
            (RECIPE_LINT, RECIPE_TEST),
            ("lint-id", "lint-id"),
            jobs.__getitem__,
            branch="feat/topic",
            source_head="a" * 40,
            worktree_path=".lab/wt/topic",
        )
