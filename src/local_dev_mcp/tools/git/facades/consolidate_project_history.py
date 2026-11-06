"""Create a local candidate history from reviewed source snapshots, without checkout."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, TypedDict

from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ....mcp.registry import ToolDefinition, safe_execution
from .._execution import GitExecution
from .._prepare import PreparedGitCommit, commit_readiness, prepare_git_commit
from ..commands import commit_tree, revisions, show, status, update_ref, var
from . import _repository, _visibility
from ._refs import BranchName, ExpectedHead, require_absent_branch, resolve_local_ref
from ._repository import observed_at
from .commit_project_changes import commit_commands
from .inspect_project_commit_history import InspectProjectCommitHistoryTool

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings


class ConsolidationUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_head: ExpectedHead
    message: Annotated[str, Field(min_length=1, max_length=4096)]


type Units = Annotated[list[ConsolidationUnit], Field(min_length=1, max_length=16)]


class ConsolidatedUnit(TypedDict):
    source_base: str
    source_head: str
    head: str
    tree: str


class ConsolidatedHistory(TypedDict):
    project: str
    branch: str
    base_head: str
    source_head: str
    head: str
    units: list[ConsolidatedUnit]
    tree_verified: bool
    checkout_unchanged: bool
    signing_requested: bool
    signature_verified: bool | None
    observed_at: str
    next_action: str


async def require_checkout(repo: GitExecution, branch: str, head: str, index: str) -> None:
    current_branch, current_head, entries = await status.read_porcelain_v2(repo)
    if current_branch != branch or current_head != head or entries:
        raise ToolError("The controller checkout changed or is dirty; no further writes were attempted.")
    if await revisions.index_fingerprint(repo) != index:
        raise ToolError("The controller index changed; inspect before continuing.")


@dataclass(frozen=True)
class ConsolidateProjectHistoryTool:
    settings: ServerSettings

    @property
    def execute(self) -> Callable[..., Awaitable[ConsolidatedHistory]]:
        @safe_execution
        async def provide() -> PreparedGitCommit:
            return await prepare_git_commit(self.settings)

        async def invoke(
            new_branch: BranchName,
            base_head: ExpectedHead,
            expected_branch: str,
            expected_head: ExpectedHead,
            units: Units,
            prepared: PreparedGitCommit = Depends(provide),
        ) -> ConsolidatedHistory:
            return await self.consolidate(new_branch, base_head, expected_branch, expected_head, units, prepared)

        return invoke

    async def consolidate(
        self,
        new_branch: str,
        base_head: str,
        expected_branch: str,
        expected_head: str,
        units: list[ConsolidationUnit],
        prepared: PreparedGitCommit,
    ) -> ConsolidatedHistory:
        repo = GitExecution(self.settings, prepared)
        await _repository.verify_root(repo)
        await _repository.require_writable_state(repo)
        if not new_branch.startswith("consolidated/"):
            raise ToolError("Use a fresh consolidated/<topic> candidate branch; main cannot be updated.")
        await require_absent_branch(repo, new_branch)
        await update_ref.require_direct(repo, new_branch)
        index = await revisions.index_fingerprint(repo)
        await require_checkout(repo, expected_branch, expected_head, index)
        history = await InspectProjectCommitHistoryTool(self.settings).execute(
            base_head, expected_branch, base_head, expected_head
        )
        previous = base_head
        positions: dict[str, int] = {}
        for position, entry in enumerate(history["commits"]):
            if entry["parents"] != [previous]:
                raise ToolError("Consolidation requires a bounded, contiguous single-parent source history.")
            positions[entry["sha"]] = position
            previous = entry["sha"]
        if not units or len(units) > 16 or units[-1].source_head != expected_head:
            raise ToolError("Select 1 to 16 units ending at the complete observed source HEAD.")

        snapshots: list[tuple[ConsolidationUnit, str]] = []
        previous_position = -1
        previous = base_head
        previous_tree = await revisions.tree(repo, base_head)
        for unit in units:
            position = positions.get(unit.source_head, -1)
            if position <= previous_position:
                raise ToolError("Unit endpoints must be distinct, ordered commits inside the source range.")
            message = unit.message
            if (
                not message.strip()
                or "\0" in message
                or re.match(r"^[a-z]+(?:\([A-Za-z0-9_-]+\))?!?: .+", message) is None
                or re.search(r"(?im)^Source-(?:Base|Head|Tree|PR):", message)
            ):
                raise ToolError("Use a Conventional Commit message without NUL or reserved source trailers.")
            changes = await revisions.changed_paths(repo, previous, unit.source_head)
            if any(
                not _visibility.readable(repo, path)
                for change in changes
                for path in (change.path, change.original_path)
                if path is not None
            ):
                raise ToolError("A selected snapshot changes protected or unreadable paths.")
            tree = await revisions.tree(repo, unit.source_head)
            if tree == previous_tree:
                raise ToolError("Each selected unit must change its preceding snapshot.")
            snapshots.append((unit, tree))
            previous_position, previous, previous_tree = position, unit.source_head, tree

        await var.require_commit_identity(repo)
        parent = base_head
        source_base = base_head
        created: list[ConsolidatedUnit] = []
        for unit, tree in snapshots:
            await require_checkout(repo, expected_branch, expected_head, index)
            message = (
                f"{unit.message.rstrip()}\n\nSource-Base: {source_base}\n"
                f"Source-Head: {unit.source_head}\nSource-Tree: {tree}\n"
            )
            candidate = await commit_tree.create(repo, tree, parent, message, signing=prepared.signing is not None)
            await revisions.resolve_commit(repo, candidate)
            await revisions.require_parent(repo, candidate, parent)
            if await revisions.tree(repo, candidate) != tree:
                raise ToolError("Candidate tree verification failed; no destination ref was created.")
            if prepared.signing is not None and not await show.verify_signature(
                repo, candidate, prepared.signing.fingerprint
            ):
                raise ToolError("Candidate signature verification failed; no destination ref was created.")
            created.append(
                {"source_base": source_base, "source_head": unit.source_head, "head": candidate, "tree": tree}
            )
            parent, source_base = candidate, unit.source_head

        await require_checkout(repo, expected_branch, expected_head, index)
        await require_absent_branch(repo, new_branch)
        # Compare-and-create: even a concurrent creator cannot be overwritten.
        await update_ref.create_absent_branch(repo, new_branch, parent)
        try:
            await update_ref.require_direct(repo, new_branch)
            _, observed = await resolve_local_ref(repo, new_branch)
            await require_checkout(repo, expected_branch, expected_head, index)
            if observed != parent or await revisions.tree(repo, observed) != snapshots[-1][1]:
                raise ToolError("The candidate ref or final tree changed.")
        except ToolError as error:
            raise ToolError(
                "Candidate creation may have succeeded, but postconditions could not be confirmed. "
                "Inspect the candidate ref and controller status before retrying; no reset was attempted."
            ) from error
        return {
            "project": str(repo.root),
            "branch": new_branch,
            "base_head": base_head,
            "source_head": expected_head,
            "head": parent,
            "units": created,
            "tree_verified": True,
            "checkout_unchanged": True,
            "signing_requested": prepared.signing is not None,
            "signature_verified": True if prepared.signing is not None else None,
            "observed_at": observed_at(),
            "next_action": ("Review provenance and validate each snapshot in an isolated local clone before adoption."),
        }


CONSOLIDATE_PROJECT_HISTORY: Final = ToolDefinition(
    name="consolidate_project_history",
    description=(
        "Create an absent consolidated/<topic> local candidate from 1-16 reviewed snapshot endpoints "
        "over at most 64 linear source commits. Preserve every selected Git tree exactly and record original "
        "range/tree provenance. Uses startup identity/signing, verifies each parent/tree/signature, and "
        "atomically creates only the new ref. Leaves original checkout, index, main and remotes untouched. "
        "commit-tree does not run ordinary commit hooks: this is not quality approval or main integration. "
        "After uncertain failure inspect refs/status; never blindly retry or overwrite an existing candidate."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
    required_commands=commit_commands,
    create_runtime=ConsolidateProjectHistoryTool,
    readiness=commit_readiness,
)
