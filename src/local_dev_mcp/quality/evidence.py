"""Server-local, exact-state quality evidence; never an integration approval."""

from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True)
class VerifiedQualityEvidence:
    profile: str
    manifest_digest: str
    recipe_digest: str
    branch: str
    source_head: str
    source_tree: str
    target_head: str
    worktree_path: str
    checks: tuple[tuple[str, str], ...]

    @property
    def job_ids(self) -> tuple[str, ...]:
        return tuple(job_id for _, job_id in self.checks)


class QualityEvidenceResult(TypedDict):
    evidence_id: str
    profile: str
    manifest_digest: str
    recipe_digest: str
    branch: str
    source_head: str
    source_tree: str
    target_head: str
    worktree_path: str
    checks: list[dict[str, str]]
    status: str
    next_action: str
