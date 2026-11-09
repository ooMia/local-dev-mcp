"""Read-only policy preflight for main integration, without approval semantics."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ....mcp.registry import ToolDefinition
from ...release import check_integration
from .._execution import GitExecution
from .._prepare import git_readiness
from ..commands import revisions
from . import _repository, _visibility
from ._refs import ExpectedHead, resolve_local_ref
from ._repository import observed_at

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type CheckState = Literal["pass", "blocked", "pending"]
type PreflightState = Literal["blocked", "external_review_required"]

_TOPIC = re.compile(r"(?:feat|fix|refactor|docs|test|build|ci|chore|perf)/[A-Za-z0-9][A-Za-z0-9/-]*")
_RELEASE = re.compile(r"release/(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")

_POLICY_REFERENCES: Final = [
    "AGENTS.md",
    "docs/adr/0005-assessed-squash-and-batched-releases.md",
    "docs/adr/0006-native-pr-workflow-mvp.md",
    "docs/adr/0008-initial-consolidated-baseline.md",
    "docs/tools/workflow/README.md",
    "docs/tools/git/history-consolidation.md",
    "docs/tools/git/remote-pr.md",
    "docs/tools/release/README.md",
]


class PolicyCheck(TypedDict):
    code: str
    state: CheckState
    detail: str


class ProjectPolicyPreflight(TypedDict):
    project: str
    operation: Literal["integrate_main"]
    scope: Literal["local_preflight_only"]
    source_ref: str
    source_head: str
    main_head: str
    state: PreflightState
    checks: list[PolicyCheck]
    policy_references: list[str]
    next_actions: list[str]
    observed_at: str


@dataclass(frozen=True)
class InspectProjectPolicyTool:
    settings: ServerSettings

    async def execute(
        self,
        operation: Literal["integrate_main"],
        source_ref: str,
        expected_main_head: ExpectedHead,
        expected_source_head: ExpectedHead,
    ) -> ProjectPolicyPreflight:
        repo = GitExecution(self.settings)
        await _repository.verify_root(repo)
        main = await revisions.resolve(repo, "main")
        source_kind, head = await resolve_local_ref(repo, source_ref)
        if main != expected_main_head or head != expected_source_head:
            raise ToolError("Observed source or main HEAD changed. Re-read both refs before preflight.")

        checks: list[PolicyCheck] = []
        next_actions: list[str] = []

        branch_ok = source_kind == "branch" and (
            _TOPIC.fullmatch(source_ref) is not None or _RELEASE.fullmatch(source_ref) is not None
        )
        checks.append(
            {
                "code": "topic_branch",
                "state": "pass" if branch_ok else "blocked",
                "detail": "A named feature or release topic branch is required for normal integration.",
            }
        )
        if not branch_ok:
            next_actions.append("Create a type/topic branch from the reviewed candidate; do not merge consolidated/*.")

        common, _, _ = await revisions.ancestry(repo, main, head)
        ancestry_ok = common == main and main != head
        checks.append(
            {
                "code": "main_ancestry",
                "state": "pass" if ancestry_ok else "blocked",
                "detail": "Source must contain the current local main and introduce a change.",
            }
        )
        if not ancestry_ok:
            next_actions.append("Re-evaluate the source against the current main; never force or reset to compensate.")

        assessment_ok = False
        assessment_detail = "An explicit new assessment is required, even for none impact."
        if ancestry_ok:

            def authorize(path: str) -> None:
                if not _visibility.readable(repo, path):
                    raise ValueError("Restricted release input")

            async def read(*args: str) -> bytes:
                return (await repo.run(*args, operation="Inspect integration assessment")).output

            try:
                integration = await check_integration(read, main, head, authorize=authorize)
                kind = integration["kind"]
                assessment_ok = (
                    kind == "legacy"
                    or (kind == "change" and _TOPIC.fullmatch(source_ref) is not None)
                    or (
                        kind == "release"
                        and _RELEASE.fullmatch(source_ref) is not None
                        and integration["plan"] is not None
                        and source_ref == f"release/{integration['plan']['next_version']}"
                    )
                )
                if assessment_ok:
                    assessment_detail = (
                        "Committed release integration snapshot is structurally valid; "
                        "compatibility judgement remains a reviewed decision."
                    )
            except ValueError:
                pass
        checks.append(
            {
                "code": "release_assessment",
                "state": "pass" if assessment_ok else "blocked",
                "detail": assessment_detail,
            }
        )
        if not assessment_ok:
            next_actions.append(
                "Review compatibility, add a new .changes assessment and validate the committed source."
            )

        checks.append(
            {
                "code": "pr_quality_and_provenance",
                "state": "pending",
                "detail": "Remote PR, exact-head Project quality, Ready state and Source-PR are not inspected here.",
            }
        )
        next_actions.append(
            "Inspect the remote main and PR; verify exact-head CI, then use reviewed Ready and squash merge."
        )
        if await revisions.resolve(repo, "main") != main or await revisions.resolve(repo, source_ref) != head:
            raise ToolError("Source or main moved during policy preflight. Re-observe and retry.")
        return {
            "project": str(repo.root),
            "operation": operation,
            "scope": "local_preflight_only",
            "source_ref": source_ref,
            "source_head": head,
            "main_head": main,
            "state": "blocked" if any(check["state"] == "blocked" for check in checks) else "external_review_required",
            "checks": checks,
            "policy_references": list(_POLICY_REFERENCES),
            "next_actions": next_actions,
            "observed_at": observed_at(),
        }


INSPECT_PROJECT_POLICY: Final = ToolDefinition(
    name="inspect_project_policy",
    description=(
        "Read-only local main integration preflight from observed source/main SHAs. "
        "Checks topic branch, ancestry and the existing committed release-assessment contract; "
        "returns applicable policy document paths and required remote PR/CI follow-up. "
        "This is not policy approval, remote PR inspection or an authorization to merge. "
        "No fetch, checkout, index update or write."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=("git",),
    create_runtime=InspectProjectPolicyTool,
    readiness=git_readiness,
)
