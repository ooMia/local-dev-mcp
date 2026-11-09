"""Real-Git candidate history reconstruction without changing the controller checkout."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.tools.git._execution import GitExecution, GitResult
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.signing import SigningSetup
from tests.support.worktrees import policy, seed

pytestmark = pytest.mark.integration


async def history(root: Path, factory: WorkspaceFactory, *, sha256: bool = False) -> tuple[str, str, str]:
    base, _ = await seed(root, factory, sha256=sha256)
    await git(root, "switch", "-c", "feat/source")
    (root / "first.txt").write_text("first\n")
    await git(root, "add", ".")
    await git(root, "commit", "-m", "feat: first")
    middle = await git(root, "rev-parse", "HEAD")
    (root / "first.txt").unlink()
    (root / "bytes.bin").write_bytes(bytes(range(256)))
    (root / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "run.sh").chmod(0o755)
    (root / ".changes").mkdir()
    (root / ".changes/first.toml").write_text('summary = "retain exact assessment bytes"\n')
    await git(root, "add", ".")
    await git(root, "update-index", "--chmod=+x", "run.sh")
    await git(root, "commit", "-m", "feat: second")
    return base, middle, await git(root, "rev-parse", "HEAD")


def arguments(base: str, middle: str, head: str) -> dict[str, object]:
    return {
        "new_branch": "consolidated/main",
        "base_head": base,
        "expected_branch": "feat/source",
        "expected_head": head,
        "units": [
            {"source_head": middle, "message": "feat(core): establish development foundation"},
            {"source_head": head, "message": "feat(agent): add automation support"},
        ],
    }


@pytest.mark.parametrize("sha256", [False, True])
def test_exact_snapshot_history_preserves_source_index_and_modes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, sha256: bool
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory, sha256=sha256)
        index = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(policy(tmp_path))) as session:
            declared = {tool.name for tool in await session.list_tools()}
            assert "consolidate_project_history" in declared
            result = await success(session, "consolidate_project_history", arguments(base, middle, head))
            assert result["source_head"] == head
            assert result["tree_verified"] is True and result["checkout_unchanged"] is True
        candidate = await git(tmp_path, "rev-parse", "consolidated/main")
        commits = (await git(tmp_path, "rev-list", "--reverse", f"{base}..{candidate}")).splitlines()
        assert len(commits) == 2
        for new, old, parent in zip(commits, [middle, head], [base, commits[0]], strict=True):
            assert await git(tmp_path, "rev-parse", f"{new}^{{tree}}") == await git(
                tmp_path, "rev-parse", f"{old}^{{tree}}"
            )
            assert await git(tmp_path, "rev-list", "--parents", "-n", "1", new) == f"{new} {parent}"
            assert f"Source-Head: {old}" in await git(tmp_path, "show", "-s", "--format=%B", new)
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "feat/source"
        assert await git(tmp_path, "rev-parse", "main") == base
        assert await git(tmp_path, "ls-files", "--stage") == index
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["reversed", "duplicate", "missing-tip", "base", "stale", "existing", "main"])
def test_rejects_invalid_plan_before_ref_publication(
    tmp_path: Path, workspace_factory: WorkspaceFactory, case: str
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        args = arguments(base, middle, head)
        if case == "reversed":
            args["units"] = [
                {"source_head": head, "message": "feat: later"},
                {"source_head": middle, "message": "feat: earlier"},
            ]
        elif case == "duplicate":
            args["units"] = [
                {"source_head": middle, "message": "feat: first"},
                {"source_head": middle, "message": "feat: duplicate"},
                {"source_head": head, "message": "feat: last"},
            ]
        elif case == "missing-tip":
            args["units"] = [{"source_head": middle, "message": "feat: incomplete"}]
        elif case == "base":
            args["units"] = [
                {"source_head": base, "message": "feat: empty"},
                {"source_head": head, "message": "feat: last"},
            ]
        elif case == "stale":
            args["expected_head"] = middle
        elif case == "existing":
            await git(tmp_path, "branch", "consolidated/main", base)
        elif case == "main":
            args["new_branch"] = "main"
        before = await git(tmp_path, "show-ref", "--heads")
        async with Client(create_server(policy(tmp_path))) as session:
            assert "consolidate_project_history" in {tool.name for tool in await session.list_tools()}
            await failure(session, "consolidate_project_history", args)
        assert await git(tmp_path, "show-ref", "--heads") == before
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["dirty", "untracked", "protected", "trailer", "empty-message"])
def test_rejects_unreviewed_inputs(tmp_path: Path, workspace_factory: WorkspaceFactory, case: str) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        selected = policy(tmp_path)
        args = arguments(base, middle, head)
        if case == "dirty":
            (tmp_path / "base.txt").write_text("uncommitted\n")
        elif case == "untracked":
            (tmp_path / "personal.txt").write_text("retain\n")
        elif case == "protected":
            selected = replace(selected, denied_paths=(tmp_path / "bytes.bin",))
        else:
            message = "feat: candidate\n\nSource-Head: forged" if case == "trailer" else " "
            args["units"] = [{"source_head": head, "message": message}]
        refs = await git(tmp_path, "show-ref", "--heads")
        index = await git(tmp_path, "ls-files", "--stage")
        before = await git(tmp_path, "status", "--porcelain")
        async with Client(create_server(selected)) as session:
            await failure(session, "consolidate_project_history", args)
        assert await git(tmp_path, "show-ref", "--heads") == refs
        assert await git(tmp_path, "ls-files", "--stage") == index
        assert await git(tmp_path, "status", "--porcelain") == before

    asyncio.run(scenario())


def test_all_candidate_commits_use_configured_signature(
    tmp_path: Path, workspace_factory: WorkspaceFactory, signing_setup: SigningSetup
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        with (tmp_path / ".env").open("a") as stream:
            stream.write(signing_setup.dotenv())
        selected = replace(policy(tmp_path), allowed_commands=("git", "gpg"))
        async with Client(create_server(selected)) as session:
            result = await success(session, "consolidate_project_history", arguments(base, middle, head))
            assert result["signing_requested"] is True and result["signature_verified"] is True
        candidate = await git(tmp_path, "rev-parse", "consolidated/main")
        for commit in (await git(tmp_path, "rev-list", f"{base}..{candidate}")).splitlines():
            assert "gpgsig " in await git(tmp_path, "cat-file", "commit", commit)

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["destination-race", "source-race", "lost-response"])
def test_concurrent_changes_and_uncertain_publication_do_not_overwrite_refs(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        original = GitExecution.run
        injected = False

        async def interrupted(self: GitExecution, *args: str, **kwargs: Any) -> GitResult:
            nonlocal injected
            if args[0] == "update-ref" and not injected:
                injected = True
                if case == "destination-race":
                    await git(tmp_path, "branch", "consolidated/main", base)
                result = await original(self, *args, **kwargs)
                if case == "lost-response":
                    raise ToolError("Publication response lost after Git completed.")
                return result
            if args[0] == "commit-tree" and case == "source-race" and not injected:
                injected = True
                result = await original(self, *args, **kwargs)
                await git(tmp_path, "commit", "--allow-empty", "-m", "chore: concurrent source update")
                return result
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(GitExecution, "run", interrupted)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "consolidate_project_history", arguments(base, middle, head))
            if case == "lost-response":
                candidate = await success(session, "get_project_ref", {"ref": "consolidated/main"})
                assert candidate["head"] != head
                await failure(session, "consolidate_project_history", arguments(base, middle, head))
        if case == "destination-race":
            assert await git(tmp_path, "rev-parse", "consolidated/main") == base
        elif case == "source-race":
            assert "consolidated/main" not in await git(tmp_path, "branch", "--list")
        else:
            assert await git(tmp_path, "rev-parse", "consolidated/main^{tree}") == await git(
                tmp_path, "rev-parse", f"{head}^{{tree}}"
            )
        assert await git(tmp_path, "rev-parse", "main") == base
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("late", [False, True])
def test_symbolic_destination_never_changes_another_ref(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, late: bool
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        original = GitExecution.run
        destination = "refs/heads/consolidated/main"
        target = "refs/heads/unrelated"
        if not late:
            await git(tmp_path, "symbolic-ref", destination, target)

        async def inject(self: GitExecution, *args: str, **kwargs: Any) -> GitResult:
            if args[0] == "update-ref" and late:
                await git(tmp_path, "symbolic-ref", destination, target)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(GitExecution, "run", inject)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "consolidate_project_history", arguments(base, middle, head))
        assert await git(tmp_path, "symbolic-ref", destination) == target
        assert await git(tmp_path, "branch", "--list", "unrelated") == ""
        assert await git(tmp_path, "rev-parse", "main") == base
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["tracked", "staged", "untracked"])
def test_explicit_dirty_controller_consolidation_preserves_all_user_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, mode: str
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        if mode == "tracked":
            (tmp_path / "base.txt").write_text("uncommitted\n")
        elif mode == "staged":
            (tmp_path / "base.txt").write_text("staged user content\n")
            await git(tmp_path, "add", "base.txt")
        else:
            (tmp_path / "scratch.txt").write_text("untracked user content\n")
        before = await git(tmp_path, "status", "--porcelain")
        index = await git(tmp_path, "ls-files", "--stage")
        args = {**arguments(base, middle, head), "allow_dirty_controller": True}
        async with Client(create_server(policy(tmp_path))) as session:
            result = await success(session, "consolidate_project_history", args)
            assert result["tree_verified"] is True
            assert result["checkout_unchanged"] is True
        candidate = await git(tmp_path, "rev-parse", "consolidated/main")
        assert await git(tmp_path, "rev-parse", f"{candidate}^{{tree}}") == await git(
            tmp_path, "rev-parse", f"{head}^{{tree}}"
        )
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "ls-files", "--stage") == index
        assert await git(tmp_path, "status", "--porcelain") == before

    asyncio.run(scenario())


def test_opted_in_consolidation_rejects_same_status_dirty_content_drift(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        base, middle, head = await history(tmp_path, workspace_factory)
        original_file = tmp_path / "base.txt"
        original_file.write_text("before\n")
        source = GitExecution.run
        injected = False

        async def mutate(self: GitExecution, *args: str, **kwargs: Any) -> GitResult:
            nonlocal injected
            result = await source(self, *args, **kwargs)
            if args[0] == "commit-tree" and not injected:
                injected = True
                original_file.write_text("after!\n")
            return result

        monkeypatch.setattr(GitExecution, "run", mutate)
        args = {**arguments(base, middle, head), "allow_dirty_controller": True}
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "consolidate_project_history", args)
        assert await git(tmp_path, "branch", "--list", "consolidated/main") == ""
        assert original_file.read_text() == "after!\n"
        assert await git(tmp_path, "rev-parse", "HEAD") == head

    asyncio.run(scenario())
