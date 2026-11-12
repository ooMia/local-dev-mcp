"""One strict quality Job contract shared by issuance and merge preflight.

Job IDs are retained server-local evidence, not user approval.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ..settings import JobDefinition

if TYPE_CHECKING:
    from ..tools.tasks._response import TaskResult


def validate_quality_job_sequence(
    recipes: tuple[JobDefinition, ...],
    job_ids: tuple[str, ...] | list[str],
    get_job: Callable[[str], TaskResult],
    *,
    branch: str,
    source_head: str,
    worktree_path: str,
) -> tuple[tuple[str, str], ...]:
    """Reject incomplete or mixed Worktrunk quality evidence before use."""
    if len(recipes) != len(job_ids):
        raise ToolError("Provide one completed Job ID for each required profile check.")
    if len(set(job_ids)) != len(job_ids):
        raise ToolError("Quality Job IDs must be distinct.")

    checks: list[tuple[str, str]] = []
    controller_origin: tuple[str, str] | None = None
    previous_finished = ""
    for recipe, job_id in zip(recipes, job_ids, strict=True):
        job = get_job(job_id)
        context = job.get("worktree")
        if (
            job["task"] != recipe.name
            or job["kind"] != recipe.kind
            or job["level"] != recipe.level
            or job["cwd"] != recipe.cwd
            or job["state"] != "succeeded"
            or job["exit_code"] != 0
            or job["termination_confirmed"] is not True
            or job["output_complete"] is not True
            or context is None
            or context["backend"] != "worktrunk"
            or context["path"] != worktree_path
            or context["branch"] != branch
            or context["head"] != source_head
            or context["verified"] is not True
        ):
            raise ToolError("A required quality Job did not pass for this exact Worktrunk source.")
        origin = (context["original_branch"], context["original_head"])
        if controller_origin is None:
            controller_origin = origin
        elif controller_origin != origin:
            raise ToolError("Quality Jobs came from different controller checkouts.")
        if recipe.kind == "test" and (job["all_tests"] is not True or job["targets"] != list(recipe.default_targets)):
            raise ToolError("Quality test Jobs must run their complete configured target scope.")
        started, finished = job["started_at"], job["finished_at"]
        if started is None or finished is None or finished < started or started < previous_finished:
            raise ToolError("Quality Jobs must have complete, ordered execution timestamps.")
        previous_finished = finished
        checks.append((recipe.name, job_id))
    return tuple(checks)
