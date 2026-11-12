"""Read-only reviewed Worktrunk integration plan; never grants approval."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ....quality import load_quality_manifest
from .. import _worktrees
from .._execution import GitExecution, text_output
from .._prepare import git_readiness
from ..commands import status, worktree
from ..worktrunk import WorktrunkAdapter
from ._refs import BranchName, ExpectedHead, resolve_local_ref

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class WorktrunkMergePlan(TypedDict):
    branch: str
    source_head: str
    source_tree: str
    target_branch: Literal["main"]
    target_head: str
    evidence_id: str
    profile: str
    manifest_digest: str
    plan_digest: str
    status: Literal["approval_required"]
    write_requested: bool
    next_action: str


@dataclass(frozen=True)
class GetProjectWorktrunkMergePlanTool:
    settings: ServerSettings

    async def execute(
        self,
        evidence_id: str,
        branch: BranchName,
        expected_source_head: ExpectedHead,
        expected_main_head: ExpectedHead,
    ) -> WorktrunkMergePlan:
        proof = self.settings.tasks.get_quality_evidence(evidence_id)
        if (
            proof.branch != branch
            or proof.source_head != expected_source_head
            or proof.target_head != expected_main_head
            or proof.profile != "pre_merge"
        ):
            raise ToolError("The evidence does not match the reviewed source and target.")
        adapter = WorktrunkAdapter(self.settings)
        target = await adapter.require_ready(branch, expected_source_head)
        if target.policy.root.relative_to(self.settings.policy.root).as_posix() != proof.worktree_path:
            raise ToolError("Evidence Worktrunk path changed.")
        manifest = load_quality_manifest(self.settings.policy.root)
        if (
            manifest.digest != proof.manifest_digest
            or load_quality_manifest(target.policy.root).digest != proof.manifest_digest
        ):
            raise ToolError("The shared quality manifest changed after verification.")
        profile, recipes = manifest.select(proof.profile, self.settings.policy.jobs)
        if not profile.require_clean or tuple(name for name, _ in proof.checks) != profile.checks:
            raise ToolError("Quality profile no longer matches the verified checks.")
        digest = hashlib.sha256(
            json.dumps([asdict(job) for job in recipes], sort_keys=True).encode("utf-8")
        ).hexdigest()
        if digest != proof.recipe_digest:
            raise ToolError("Registered quality recipes changed after verification.")
        for (name, job_id), recipe in zip(proof.checks, recipes, strict=True):
            job = self.settings.tasks.get(job_id)
            context = job.get("worktree")
            if (
                name != recipe.name
                or job["state"] != "succeeded"
                or job["exit_code"] != 0
                or not job["termination_confirmed"]
                or context is None
                or context["backend"] != "worktrunk"
                or context["verified"] is not True
                or context["head"] != proof.source_head
                or context["path"] != proof.worktree_path
            ):
                raise ToolError("One or more verified quality Jobs became unavailable.")

        repo = GitExecution(self.settings)
        _, source_head = await resolve_local_ref(repo, branch)
        _, main_head = await resolve_local_ref(repo, "main")
        if source_head != proof.source_head or main_head != proof.target_head:
            raise ToolError("Source or main moved after quality verification.")
        if source_head == main_head:
            raise ToolError("Source adds no commits beyond main; no integration is needed.")
        ancestry = await repo.run(
            "merge-base",
            "--is-ancestor",
            main_head,
            source_head,
            operation="Check Worktrunk merge-plan ancestry",
            allow_failure=True,
        )
        if ancestry.code != 0:
            raise ToolError("Main is no longer an ancestor of the reviewed source.")
        source_tree = text_output(
            (await repo.run("rev-parse", source_head + "^{tree}", operation="Inspect merge-plan tree")).output
        ).strip()
        if source_tree != proof.source_tree:
            raise ToolError("Reviewed source Git Tree changed.")
        records = await worktree.list_registered(repo)
        registered_main = [row for row in records if row.branch == "refs/heads/main"]
        if len(registered_main) != 1 or registered_main[0].unavailable:
            raise ToolError("A registered main Worktree is required.")
        main_path = registered_main[0].path
        try:
            self.settings.policy.require_path_access(main_path, write=True)
        except PermissionError as error:
            raise ToolError("The main Worktree is outside the authorized workspace.") from error
        main_repo = GitExecution(_worktrees.scoped_settings(self.settings, main_path))
        branch_now, head_now, entries = await status.read_porcelain_v2(main_repo)
        if branch_now != "main" or head_now != main_head or entries:
            raise ToolError("Main Worktree changed or is dirty; no integration is allowed.")
        await adapter.require_ready(branch, source_head)
        _, latest_main = await resolve_local_ref(repo, "main")
        if latest_main != main_head:
            raise ToolError("Main moved while preparing the plan.")
        plan_digest = hashlib.sha256(
            json.dumps(
                {
                    "branch": branch,
                    "source_head": source_head,
                    "source_tree": source_tree,
                    "main_head": main_head,
                    "evidence_id": evidence_id,
                    "mode": "ff",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return {
            "branch": branch,
            "source_head": source_head,
            "source_tree": source_tree,
            "target_branch": "main",
            "target_head": main_head,
            "evidence_id": evidence_id,
            "profile": proof.profile,
            "manifest_digest": proof.manifest_digest,
            "plan_digest": plan_digest,
            "status": "approval_required",
            "write_requested": False,
            "next_action": (
                "Review this exact plan; MCP merge remains blocked until trustworthy user approval is implemented."
            ),
        }


GET_PROJECT_WORKTRUNK_MERGE_PLAN: Final = ToolDefinition(
    name="get_project_worktrunk_merge_plan",
    description=(
        "Read-only preflight for exact Worktrunk source, retained quality evidence and clean main. "
        "Verify ancestry, source Git Tree, manifest/recipes and Job IDs; return a review digest. "
        "Does not grant approval, execute a merge, clean Worktrees or modify Git refs."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git", "wt", "git.worktree.write"),
    create_runtime=GetProjectWorktrunkMergePlanTool,
    readiness=git_readiness,
)
