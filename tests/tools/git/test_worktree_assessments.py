"""Real Git assessment preparation and exact scoped commit boundaries."""

import asyncio
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from tests.support.git import WorkspaceFactory, git
from tests.support.mcp import failure, success
from tests.support.signing import SigningSetup
from tests.support.worktrees import policy, request, seed

pytestmark = pytest.mark.integration

ASSESSMENT = (
    'impact = "minor"\nsummary = "명시적 기능 평가"\nreason = "선택적 기능을 추가하며 기존 계약을 유지한다."\n'
    'contract = "MCP"\nassessed_by = "agent"\nmigration = ""\nsupersedes = []\n'
)
PREPARE = "prepare_project_worktree_assessment"
COMMIT = "commit_project_worktree_assessment"


def assessment_request(head: str) -> dict[str, object]:
    return {**request(head), "assessment_id": "runtime-foundation", "assessment_toml": ASSESSMENT}


def commit_request(head: str, digest: str) -> dict[str, object]:
    return {
        **request(head),
        "assessment_id": "runtime-foundation",
        "expected_digest": digest,
        "message": "chore: assess runtime foundation",
    }


@pytest.mark.parametrize("sha256", [False, True])
def test_assessment_preparation_and_commit_preserve_original(
    tmp_path: Path, workspace_factory: WorkspaceFactory, sha256: bool
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory, sha256=sha256)
        original_index = await git(tmp_path, "ls-files", "--stage")
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            digest = hashlib.sha256(ASSESSMENT.encode()).hexdigest()
            assert result["digest"] == digest and result["source_head"] == head
            path = target / ".changes/runtime-foundation.toml"
            assert path.read_bytes() == ASSESSMENT.encode()
            assert await git(target, "diff", "--cached", "--name-only") == ""
            assert await git(target, "rev-parse", "HEAD") == head
            await failure(session, PREPARE, assessment_request(head))
            committed = await success(session, COMMIT, commit_request(head, digest))
            after = str(committed["head_after"])
            assert committed["success"] is True and after != head
            assert committed["signature_verified"] is None
            assert await git(target, "diff", "--name-only", head, after) == ".changes/runtime-foundation.toml"
            assert await git(target, "show", after + ":.changes/runtime-foundation.toml") == ASSESSMENT.rstrip("\n")
            assert await git(target, "rev-parse", after + "^") == head
            assert await git(target, "status", "--porcelain") == ""
            await failure(session, COMMIT, commit_request(head, digest))
        assert await git(tmp_path, "status", "--porcelain") == ""
        assert await git(tmp_path, "rev-parse", "HEAD") == head
        assert await git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
        assert await git(tmp_path, "ls-files", "--stage") == original_index
        assert not (tmp_path / ".changes").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad",
    [
        {"assessment_id": "../escape"},
        {"assessment_toml": 'impact = "minor"'},
        {"assessment_toml": ASSESSMENT.replace('impact = "minor"', 'impact = "major"')},
        {"assessment_toml": ASSESSMENT.replace("supersedes = []", 'supersedes = ["other"]')},
        {"assessment_toml": ASSESSMENT + "unknown = true\n"},
        {"assessment_toml": "#" * 16385},
        {"expected_branch_head": "a" * 40},
    ],
)
def test_invalid_assessment_never_writes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, bad: dict[str, str]
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            await failure(session, PREPARE, {**assessment_request(head), **bad})
            assert await git(target, "status", "--porcelain") == ""
            assert not (target / ".changes").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["digest", "extra-untracked", "extra-staged", "modified"])
def test_commit_rejects_unreviewed_target_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory, mode: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            digest = str(result["digest"])
            if mode == "digest":
                digest = "0" * 64
            elif mode == "modified":
                (target / ".changes/runtime-foundation.toml").write_text(ASSESSMENT + "# changed\n")
            else:
                (target / "extra.txt").write_text("preserve\n")
                if mode == "extra-staged":
                    await git(target, "add", "extra.txt")
            await failure(session, COMMIT, commit_request(head, digest))
            assert await git(target, "rev-parse", "HEAD") == head
            assert (target / ".changes/runtime-foundation.toml").exists()
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_assessment_respects_source_denial_and_readonly(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
        target = tmp_path / str(ready["path"])
        for restricted in (
            replace(policy(tmp_path), denied_paths=(tmp_path / ".changes",)),
            replace(policy(tmp_path), read_only_paths=(tmp_path / ".changes",)),
            replace(policy(tmp_path), allowed_commands=("git",)),
        ):
            async with Client(create_server(restricted)) as session:
                await failure(session, PREPARE, assessment_request(head))
            assert not (target / ".changes").exists()

    asyncio.run(scenario())


def test_hook_failure_preserves_assessment_and_index_for_inspection(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            hook = tmp_path / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            await failure(session, COMMIT, commit_request(head, str(result["digest"])))
            assert await git(target, "rev-parse", "HEAD") == head
            assert await git(target, "diff", "--cached", "--name-only") == ".changes/runtime-foundation.toml"
            assert (target / ".changes/runtime-foundation.toml").read_bytes() == ASSESSMENT.encode()
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_assessment_tools_registered_with_strict_schema(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        async with Client(create_server(policy(tmp_path))) as session:
            declared = {entry.name: entry for entry in await session.list_tools()}
            for name in (PREPARE, COMMIT):
                assert name in declared
                assert declared[name].input_schema["additionalProperties"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["symlink", "ignored", "duplicate-id", "duplicate-content"])
def test_preparation_rejects_unsafe_paths_and_existing_judgements(
    tmp_path: Path, workspace_factory: WorkspaceFactory, mode: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            inputs = assessment_request(head)
            outside = tmp_path.parent / (tmp_path.name + "-assessment")
            if mode == "symlink":
                outside.mkdir()
                (target / ".changes").symlink_to(outside, target_is_directory=True)
            elif mode == "ignored":
                with (tmp_path / ".git/info/exclude").open("a") as stream:
                    stream.write("\n.changes/\n")
            else:
                result = await success(session, PREPARE, inputs)
                committed = await success(session, COMMIT, commit_request(head, str(result["digest"])))
                inputs["expected_branch_head"] = str(committed["head_after"])
                if mode == "duplicate-content":
                    inputs["assessment_id"] = "other-id"
            await failure(session, PREPARE, inputs)
            if mode == "symlink":
                assert list(outside.iterdir()) == []
            if mode == "ignored":
                assert not (target / ".changes").exists()
            if mode == "duplicate-content":
                assert not (target / ".changes/other-id.toml").exists()

    asyncio.run(scenario())


def test_clean_filter_cannot_change_reviewed_staged_bytes(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            (tmp_path / ".git/info/attributes").write_text("*.toml filter=assessment\n")
            await git(tmp_path, "config", "filter.assessment.clean", "sed s/minor/patch/g")
            await failure(session, COMMIT, commit_request(head, str(result["digest"])))
            assert await git(target, "rev-parse", "HEAD") == head
            assert 'impact = "patch"' in await git(target, "show", ":.changes/runtime-foundation.toml")
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_commit_detects_hook_changing_assessment_file_mode(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            hook = tmp_path / ".git/hooks/pre-commit"
            hook.write_text(
                "#!/bin/sh\nchmod +x .changes/runtime-foundation.toml\ngit add -- .changes/runtime-foundation.toml\n"
            )
            hook.chmod(0o755)
            await failure(session, COMMIT, commit_request(head, str(result["digest"])))
            assert await git(target, "rev-parse", "HEAD") != head
            assert (await git(target, "ls-tree", "HEAD", ".changes/runtime-foundation.toml")).startswith("100755")
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_assessment_commit_uses_controller_signing_without_copying_credentials(
    tmp_path: Path, workspace_factory: WorkspaceFactory, signing_setup: SigningSetup
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        (tmp_path / ".env").write_text(
            signing_setup.dotenv() + '\nGIT_USER_NAME="Controller"\nGIT_USER_EMAIL="controller@example.invalid"\n'
        )
        selected = replace(policy(tmp_path), allowed_commands=("git", "gpg", "git.worktree.write"))
        async with Client(create_server(selected)) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            committed = await success(session, COMMIT, commit_request(head, str(result["digest"])))
            assert committed["signing_requested"] is True and committed["signature_verified"] is True
            assert await git(target, "log", "-1", "--format=%an") == "Controller"
            assert not (target / ".env").exists()
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["denied", "readonly"])
def test_assessment_respects_physical_worktree_path_policy(
    tmp_path: Path, workspace_factory: WorkspaceFactory, scope: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
        target = tmp_path / str(ready["path"])
        restricted = (
            replace(policy(tmp_path), denied_paths=(target / ".changes",))
            if scope == "denied"
            else replace(policy(tmp_path), read_only_paths=(target / ".changes",))
        )
        async with Client(create_server(restricted)) as session:
            await failure(session, PREPARE, assessment_request(head))
        assert not (target / ".changes").exists()
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["crlf", "mode"])
def test_commit_detects_post_commit_changes_hidden_by_git_status(
    tmp_path: Path, workspace_factory: WorkspaceFactory, mutation: str
) -> None:
    async def scenario() -> None:
        head, _ = await seed(tmp_path, workspace_factory)
        async with Client(create_server(policy(tmp_path))) as session:
            ready = await success(session, "prepare_project_worktree", request(head))
            target = tmp_path / str(ready["path"])
            result = await success(session, PREPARE, assessment_request(head))
            if mutation == "crlf":
                await git(tmp_path, "config", "core.autocrlf", "true")
                script = (
                    f"#!{sys.executable}\nimport subprocess\nfrom pathlib import Path\n"
                    "p=Path('.changes/runtime-foundation.toml')\n"
                    "p.write_bytes(p.read_bytes().replace(b'\\n',b'\\r\\n'))\n"
                    "subprocess.run(['git','add','--',str(p)],check=True)\n"
                )
            else:
                await git(tmp_path, "config", "core.filemode", "false")
                script = "#!/bin/sh\nchmod +x .changes/runtime-foundation.toml\n"
            hook = tmp_path / ".git/hooks/post-commit"
            hook.write_text(script)
            hook.chmod(0o755)
            await failure(session, COMMIT, commit_request(head, str(result["digest"])))
            assert await git(target, "rev-parse", "HEAD") != head
            assert await git(target, "status", "--porcelain") == ""
        assert await git(tmp_path, "status", "--porcelain") == ""

    asyncio.run(scenario())
