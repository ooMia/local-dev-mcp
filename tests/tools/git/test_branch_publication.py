"""Branch publication uses the managed checkout and the existing remote gates."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.tools.git import _remote
from local_dev_mcp.tools.git._execution import GitExecution
from local_dev_mcp.tools.git.commands import worktree
from local_dev_mcp.tools.git.facades import remote_pr
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.worktrees import policy, request, seed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("backend", ["native", "worktrunk"])
def test_branch_publication_reuses_remote_gates_in_target_checkout(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        # Controller template exists only in the current checkout, not the historical branch.
        template = Path(__file__).resolve().parents[3] / ".github" / "PULL_REQUEST_TEMPLATE.md"
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(template.read_text())
        with (tmp_path / ".git" / "info" / "exclude").open("a") as stream:
            stream.write("\n.github/\n")
        origin = tmp_path.parent / (tmp_path.name + "-origin.git")
        await git(tmp_path, "init", "--bare", str(origin))
        await git(tmp_path, "remote", "add", "origin", str(origin))
        await git(tmp_path, "push", "origin", "main")
        await git(tmp_path, "update-ref", "refs/remotes/origin/main", base)
        permissions = replace(
            policy(tmp_path),
            allowed_commands=(
                "git",
                "wt",
                "gh",
                "git.worktree.write",
                "git.remote.read",
                "git.remote.write",
                "gh.pr.create",
            ),
        )
        roots: list[Path] = []
        bodies: list[str] = []
        created: list[dict[str, object]] = []
        pushes: list[tuple[str, ...]] = []

        class LocalRemote(_remote.Remote):
            async def repo(self) -> tuple[str, str]:
                roots.append(self.root)
                return "example/project", "https://github.com/example/project"

            async def main_sha(self, name: str) -> str:
                return base

            async def branch(self, name: str) -> str | None:
                text = await git(self.root, "ls-remote", "--heads", "origin", "refs/heads/" + name)
                return text.split("\t")[0] if text else None

            async def git(self, *args: str, write: bool = False, allow_failure: bool = False) -> _remote.Result:
                if args[0] == "push":
                    pushes.append(args)
                    await git(self.root, *args)
                    return _remote.Result(0, "")
                return await super().git(*args, write=write, allow_failure=allow_failure)

            async def gh(self, *args: str, write: bool = False, input_text: str | None = None) -> _remote.Result:
                if args[:2] == ("pr", "list"):
                    return _remote.Result(0, json.dumps(created))
                if args[:2] == ("pr", "create"):
                    assert write and input_text is not None
                    bodies.append(input_text)
                    created.append(
                        {
                            "number": 8,
                            "url": "https://github.com/example/project/pull/8",
                            "headRefOid": await self.local_sha("HEAD"),
                            "baseRefName": "main",
                            "isDraft": True,
                            "state": "OPEN",
                        }
                    )
                    return _remote.Result(0, "https://github.com/example/project/pull/8\n")
                if args[:2] == ("pr", "view"):
                    return _remote.Result(
                        0,
                        json.dumps(
                            {"body": bodies[-1], "headRefOid": await self.local_sha("HEAD"), "baseRefOid": base}
                        ),
                    )
                raise AssertionError(args)

        monkeypatch.setattr(remote_pr, "Remote", LocalRemote)
        async with Client(create_server(permissions)) as session:
            initial = request(base)
            await failure(session, "push_project_branch", {**initial, "expected_remote_main": base})
            if backend == "native":
                ready = await success(session, "prepare_project_worktree", initial)
                target = tmp_path / str(ready["path"])
            else:
                target = tmp_path / ".lab" / "manually-registered"
                await git(tmp_path, "worktree", "add", str(target), "feat/publish")
            assert not (target / ".github").exists()
            (target / ".changes").mkdir()
            (target / ".changes" / "example.toml").write_text(
                'impact = "minor"\nsummary = "Isolated publication"\n'
                'reason = "Publish selected historical branches."\nmigration = ""\n'
            )
            await git(target, "add", ".changes")
            await git(target, "commit", "-m", "feat: assess isolated publication")
            head = await git(target, "rev-parse", "HEAD")
            arguments = {**initial, "expected_branch_head": head, "expected_remote_main": base, "backend": backend}
            inspected = await success(
                session,
                "inspect_project_branch_remote",
                {"branch": "feat/publish", "expected_branch_head": head, "backend": backend},
            )
            assert inspected["branch"] == "feat/publish" and inspected["head"] == head
            (target / "base.txt").write_text("dirty")
            await failure(session, "push_project_branch", arguments)
            (target / "base.txt").write_text("base\n")
            await failure(session, "push_project_branch", {**arguments, "expected_branch_head": base})
            if backend == "worktrunk":
                (tmp_path / "base.txt").write_text("dirty controller only\n")
                await failure(session, "push_project_branch", arguments)
                await failure(
                    session,
                    "push_project_branch",
                    {**arguments, "backend": "native", "allow_dirty_controller": True},
                )
                publication_args = {**arguments, "allow_dirty_controller": True}
            else:
                await failure(
                    session,
                    "push_project_branch",
                    {**arguments, "allow_dirty_controller": True},
                )
                publication_args = arguments
            result = await success(session, "push_project_branch", publication_args)
            assert result["remote_after"] == head
            assert roots and set(roots) == {target}
            assert len(pushes) == 1 and pushes[0][-1] == head + ":refs/heads/feat/publish"
            assert not any("--force" in item or "--no-verify" in item for item in pushes[0])
            restricted = replace(permissions, denied_paths=(tmp_path / ".github",))
            async with Client(create_server(restricted)) as denied:
                await failure(
                    denied,
                    "create_project_branch_draft_pr",
                    {**publication_args, "title_text": "feat(git): publish isolated branch"},
                )
            assert not created
            template_path = tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md"
            saved = template_path.with_suffix(".saved")
            template_path.rename(saved)
            template_path.symlink_to(saved)
            await failure(
                session,
                "create_project_branch_draft_pr",
                {**publication_args, "title_text": "feat(git): publish isolated branch"},
            )
            assert not created
            template_path.unlink()
            saved.rename(template_path)
            draft = await success(
                session,
                "create_project_branch_draft_pr",
                {**publication_args, "title_text": "feat(git): publish isolated branch"},
            )
            assert draft["created"] is True and draft["draft"] is True
            duplicate = await success(
                session,
                "create_project_branch_draft_pr",
                {**publication_args, "title_text": "feat(git): publish isolated branch"},
            )
            assert duplicate["created"] is False and len(created) == 1 and len(bodies) == 1
            assert head in bodies[0]
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert await git(tmp_path, "rev-parse", "HEAD") == base
        if backend == "worktrunk":
            assert (tmp_path / "base.txt").read_text() == "dirty controller only\n"
            assert await git(tmp_path, "status", "--porcelain") == " M base.txt"
        else:
            assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_partial_add_failure_is_inspectable_and_never_retried(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = worktree.add_existing_branch

    async def fail_after_checkout(execution: GitExecution, path: Path, branch: str) -> None:
        await original(execution, path, branch)
        raise ToolError("Simulated post-checkout failure.")

    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        monkeypatch.setattr(worktree, "add_existing_branch", fail_after_checkout)
        async with Client(create_server(policy(tmp_path))) as session:
            await failure(session, "prepare_project_worktree", request(base))
            state = await success(session, "inspect_project_worktree", {"branch": "feat/publish"})
            assert state["state"] == "partial" and state["registered"] is True
            assert (tmp_path / str(state["path"]) / "base.txt").read_text() == "base\n"
            await failure(session, "prepare_project_worktree", request(base))
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "rev-parse", "HEAD") == base

    asyncio.run(scenario())


def test_worktrunk_remote_inspection_uses_registered_checkout_without_native_receipt(
    tmp_path: Path, workspace_factory: WorkspaceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        base, _ = await seed(tmp_path, workspace_factory)
        origin = tmp_path.parent / (tmp_path.name + "-remote-inspection.git")
        await git(tmp_path, "init", "--bare", str(origin))
        await git(tmp_path, "remote", "add", "origin", str(origin))
        await git(tmp_path, "push", "origin", "main")
        await git(tmp_path, "update-ref", "refs/remotes/origin/main", base)

        checkout = tmp_path / ".lab" / "external-branch"
        await git(tmp_path, "worktree", "add", str(checkout), "feat/publish")
        roots: list[Path] = []

        class LocalRemote(_remote.Remote):
            async def repo(self) -> tuple[str, str]:
                roots.append(self.root)
                return "example/project", "https://github.com/example/project"

            async def main_sha(self, name: str) -> str:
                return base

            async def branch(self, name: str) -> str | None:
                return None

        monkeypatch.setattr(remote_pr, "Remote", LocalRemote)
        permissions = replace(policy(tmp_path), allowed_commands=("git", "gh", "git.remote.read"))
        async with Client(create_server(permissions)) as session:
            await failure(
                session,
                "inspect_project_branch_remote",
                {"branch": "feat/publish", "expected_branch_head": base},
            )
            inspected = await success(
                session,
                "inspect_project_branch_remote",
                {"branch": "feat/publish", "expected_branch_head": base, "backend": "worktrunk"},
            )
            assert inspected["branch"] == "feat/publish"
            assert inspected["head"] == inspected["remote_main"] == base
            assert inspected["tracking_is_fresh"] is True
            assert roots == [checkout]

            (checkout / "dirty.txt").write_text("uncommitted user data")
            await failure(
                session,
                "inspect_project_branch_remote",
                {"branch": "feat/publish", "expected_branch_head": base, "backend": "worktrunk"},
            )
            assert (checkout / "dirty.txt").read_text() == "uncommitted user data"

    asyncio.run(scenario())
