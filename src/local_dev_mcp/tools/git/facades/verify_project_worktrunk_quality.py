"""Attest completed Worktrunk checks against the exact source tree, not tool invocations."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Annotated, Final

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ....mcp.registry import ToolDefinition
from ....quality import load_quality_manifest
from ....quality.evidence import QualityEvidenceResult, VerifiedQualityEvidence
from .._execution import GitExecution, text_output
from .._prepare import git_readiness
from ..worktrunk import WorktrunkAdapter
from ._refs import BranchName, ExpectedHead, resolve_local_ref

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type ProfileName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
type JobIds = Annotated[list[str], Field(min_length=1, max_length=32)]


@dataclass(frozen=True)
class VerifyProjectWorktrunkQualityTool:
    settings: ServerSettings

    async def execute(
        self,
        branch: BranchName,
        expected_source_head: ExpectedHead,
        expected_main_head: ExpectedHead,
        job_ids: JobIds,
        profile: ProfileName = "pre_merge",
    ) -> QualityEvidenceResult:
        if len(set(job_ids)) != len(job_ids):
            raise ToolError("Quality jobs must be distinct and ordered by the profile.")
        adapter = WorktrunkAdapter(self.settings)
        target = await adapter.require_ready(branch, expected_source_head)
        root = self.settings.policy.root
        try:
            manifest = load_quality_manifest(root)
            source_manifest = load_quality_manifest(target.policy.root)
            selected, recipes = manifest.select(profile, self.settings.policy.jobs)
        except ValueError as error:
            raise ToolError("A shared quality profile is absent or invalid.") from error
        if source_manifest.digest != manifest.digest:
            raise ToolError("Source and controller quality definitions differ.")
        if len(recipes) != len(job_ids):
            raise ToolError("Provide one completed Job ID for each required profile check.")
        if not selected.require_clean:
            raise ToolError("Pre-integration evidence requires a clean-source quality profile.")

        repo = GitExecution(self.settings)
        _, source_head = await resolve_local_ref(repo, branch)
        _, main_head = await resolve_local_ref(repo, "main")
        if source_head != expected_source_head or main_head != expected_main_head:
            raise ToolError("Source or main moved; quality evidence is not valid.")
        tree = text_output(
            (await repo.run("rev-parse", source_head + "^{tree}", operation="Inspect quality source tree")).output
        ).strip()
        expected_path = target.policy.root.relative_to(root).as_posix()

        checks: list[tuple[str, str]] = []
        previous_finished = ""
        controller_origin: tuple[str, str] | None = None
        for recipe, job_id in zip(recipes, job_ids, strict=True):
            job = self.settings.tasks.get(job_id)
            context = job.get("worktree")
            if (
                job["task"] != recipe.name
                or job["state"] != "succeeded"
                or job["exit_code"] != 0
                or job["kind"] != recipe.kind
                or job["level"] != recipe.level
                or job["cwd"] != recipe.cwd
                or job["termination_confirmed"] is not True
                or not job["output_complete"]
                or context is None
                or context["backend"] != "worktrunk"
                or context["path"] != expected_path
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
            if recipe.kind == "test" and (
                job["all_tests"] is not True or job["targets"] != list(recipe.default_targets)
            ):
                raise ToolError("Quality test Jobs must run their complete configured target scope.")
            started, finished = job["started_at"], job["finished_at"]
            if started is None or finished is None or finished < started or started < previous_finished:
                raise ToolError("Quality Jobs must have complete, ordered execution timestamps.")
            previous_finished = finished
            checks.append((recipe.name, job_id))

        # Repeat the source observation after reading all retained Jobs.
        await adapter.require_ready(branch, source_head)
        _, observed_main = await resolve_local_ref(repo, "main")
        if observed_main != main_head or load_quality_manifest(root).digest != manifest.digest:
            raise ToolError("Quality inputs changed while building evidence.")
        recipe_digest = hashlib.sha256(
            json.dumps([asdict(job) for job in recipes], sort_keys=True).encode("utf-8")
        ).hexdigest()
        proof = VerifiedQualityEvidence(
            profile, manifest.digest, recipe_digest, branch, source_head, tree, main_head, expected_path, tuple(checks)
        )
        token = self.settings.tasks.store_quality_evidence(proof)
        return {
            "evidence_id": token,
            "profile": proof.profile,
            "manifest_digest": proof.manifest_digest,
            "recipe_digest": proof.recipe_digest,
            "branch": proof.branch,
            "source_head": proof.source_head,
            "source_tree": proof.source_tree,
            "target_head": proof.target_head,
            "worktree_path": proof.worktree_path,
            "checks": [{"name": name, "job_id": job_id} for name, job_id in proof.checks],
            "status": "VERIFIED",
            "next_action": "Request independent main integration approval; evidence alone does not authorize a merge.",
        }


VERIFY_PROJECT_WORKTRUNK_QUALITY: Final = ToolDefinition(
    name="verify_project_worktrunk_quality",
    description=(
        "Attest an exact clean Worktrunk source using completed, ordered, full-scope quality Jobs "
        "from this server's Task Manager. Verify source/target heads, tree and shared manifest; "
        "issue expiring server-local evidence, never main integration approval or an executable command. "
        "Reject missing/dirty/moved source and unsuitable test selections."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=VerifyProjectWorktrunkQualityTool,
    readiness=git_readiness,
)
