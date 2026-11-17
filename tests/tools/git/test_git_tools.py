"""Real Git repositories and MCP calls for the local snapshot workflow."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from shellous import sh

from local_dev_mcp.app.runtime import ServerSettings
from local_dev_mcp.errors import ConfigurationError, PolicyError
from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.git import _execution, command_environment
from local_dev_mcp.tools.git.facades import _repository, get_project_head
from tests.support.git import WorkspaceFactory, client, configure_identity, git
from tests.support.mcp import failure, success
from tests.support.signing import SigningSetup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("flag", [None, "false"])
def test_default_unsigned_commit_ignores_repository_signing_and_unused_gpg_settings(
    tmp_path: Path, flag: str | None, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        with (tmp_path / ".env").open("a") as stream:
            if flag is not None:
                stream.write(f"GIT_COMMIT_SIGN={flag}\n")
            stream.write("GIT_GPG_HOME=/missing\nGIT_GPG_PROGRAM=/missing\nGIT_SIGNING_KEY=invalid\n")
        await git(tmp_path, "config", "commit.gpgSign", "true")
        await git(tmp_path, "config", "gpg.program", "missing-signer")
        (tmp_path / "data").write_text("unsigned by explicit intent")
        async with client(tmp_path) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            result = await success(session, "commit_project_changes", {"message": "unsigned"})
            assert result["success"] is True
            assert result["signing_requested"] is False
            assert result["signature_verified"] is None
            assert result["signature_error"] is None
        assert "gpgsig " not in await git(tmp_path, "cat-file", "commit", "HEAD")

    asyncio.run(scenario())


def test_unsigned_settings_are_fixed_until_server_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        file = tmp_path / "data"
        file.write_text("first")
        server_client = client(tmp_path)
        (tmp_path / ".env").write_text("GIT_USER_NAME=Next Writer\nGIT_USER_EMAIL=next@example.invalid\n")
        async with server_client as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            first = await success(session, "commit_project_changes", {"message": "fixed"})
            assert first["signing_requested"] is False
            assert await git(tmp_path, "show", "-s", "--format=%an") == "Test Writer"
        file.write_text("second")
        async with client(tmp_path) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            await success(session, "commit_project_changes", {"message": "after restart"})
        assert await git(tmp_path, "show", "-s", "--format=%an") == "Next Writer"

    asyncio.run(scenario())


@pytest.mark.parametrize("selection", ["explicit", "absent", "empty"])
def test_commit_returns_verified_openpgp_signature(
    tmp_path: Path, signing_setup: SigningSetup, selection: str, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        if selection != "explicit":
            env_file = tmp_path / ".env"
            env_file.write_text(
                "\n".join(line for line in env_file.read_text().splitlines() if not line.startswith("GIT_GPG_PROGRAM="))
                + "\n"
            )
            if selection == "empty":
                with env_file.open("a") as stream:
                    stream.write("GIT_GPG_PROGRAM=\n")
        await git(tmp_path, "config", "commit.gpgSign", "false")
        await git(tmp_path, "config", "gpg.format", "ssh")
        await git(tmp_path, "config", "gpg.program", "missing-repository-program")
        (tmp_path / "selected.txt").write_text("signed payload\n")
        policy = Policy(tmp_path, (tmp_path, signing_setup.home), ("git", "gpg"))
        async with Client(create_server(policy)) as session:
            await success(session, "stage_project_paths", {"paths": ["selected.txt"]})
            result = await success(session, "commit_project_changes", {"message": "signed"})
            assert result["signature_verified"] is True
            assert result["signature_error"] is None
            assert (await success(session, "get_project_head"))["head"] == result["head_after"]
        assert "gpgsig -----BEGIN PGP SIGNATURE-----" in await git(tmp_path, "cat-file", "commit", "HEAD")

    asyncio.run(scenario())


def test_enabled_signing_static_failure_is_scoped_to_commit(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / ".env").write_text(
            "GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\nGIT_COMMIT_SIGN=true\n"
        )
        (tmp_path / "selected.txt").write_text("must stay staged\n")
        await git(tmp_path, "add", "selected.txt")
        before = await git(tmp_path, "write-tree")
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert "GIT_SIGNING_KEY" in error
            await success(session, "get_project_status")
        assert await git(tmp_path, "write-tree") == before

    asyncio.run(scenario())


def test_explicit_invalid_gpg_program_is_not_replaced(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        with (tmp_path / ".env").open("a") as stream:
            stream.write("GIT_GPG_PROGRAM=/missing-explicit-gpg\n")
        (tmp_path / "data").write_text("keep staged")
        await git(tmp_path, "add", "data")
        before = await git(tmp_path, "write-tree")
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert "command -v gpg" in error
        assert await git(tmp_path, "write-tree") == before

    asyncio.run(scenario())


def test_full_workflow_initial_commit_updates_deletions_and_unstage(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        file = tmp_path / "file.txt"
        file.write_text("first\n")
        async with client(tmp_path, signing=signing_setup) as session:
            status = await success(session, "get_project_status")
            assert status["head"] is None
            assert status["branch"]
            assert status["entries"] == [
                {
                    "kind": "untracked",
                    "path": "file.txt",
                    "original_path": None,
                    "index_status": "?",
                    "worktree_status": "?",
                    "submodule": None,
                }
            ]
            assert (await success(session, "get_project_diff"))["patch"] == ""
            await success(session, "stage_project_paths", {"paths": ["."]})
            staged = await success(session, "get_project_diff", {"staged": True})
            assert "+first" in str(staged["patch"])
            await success(session, "unstage_project_paths", {"paths": ["."]})
            assert file.read_text() == "first\n"
            assert await git(tmp_path, "ls-files") == ""
            await success(session, "stage_project_paths", {"paths": ["file.txt"]})
            committed = await success(session, "commit_project_changes", {"message": "first\n\nDetails"})
            assert committed["success"] is True
            assert committed["head_before"] is None
            assert committed["head_after"] == await git(tmp_path, "rev-parse", "HEAD")
            assert committed["head_observed"] is True
            assert committed["observation_error"] is None
            assert await git(tmp_path, "show", "HEAD:file.txt") == "first"
            file.write_text("second\n")
            assert "+second" in str((await success(session, "get_project_diff"))["patch"])
            await success(session, "stage_project_paths", {"paths": ["file.txt"]})
            await success(session, "unstage_project_paths", {"paths": ["file.txt"]})
            assert file.read_text() == "second\n"
            assert await git(tmp_path, "diff", "--cached") == ""
            file.unlink()
            await success(session, "stage_project_paths", {"paths": ["."]})
            deleted = await success(session, "commit_project_changes", {"message": "delete"})
            assert deleted["head_before"] == committed["head_after"]
            assert await git(tmp_path, "ls-files") == ""
            assert "failed" in await failure(session, "commit_project_changes", {"message": "empty"})

    asyncio.run(scenario())


def test_literal_special_paths_ignored_files_and_nonforced_unborn_unstage(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        names = ["-option.txt", "star*.txt", "line\nbreak.txt", "공백 name.txt", ":(glob)*"]
        for name in names:
            (tmp_path / name).write_text("original\n")
        (tmp_path / "star-other.txt").write_text("not selected\n")
        (tmp_path / ".gitignore").write_text("ignored.txt\n")
        (tmp_path / "ignored.txt").write_text("hidden\n")
        async with client(tmp_path) as session:
            await success(session, "stage_project_paths", {"paths": names})
            listed = await git(tmp_path, "ls-files", "-z")
            assert set(listed.split("\0")[:-1]) == set(names)
            before = await git(tmp_path, "write-tree")
            assert "failed" in await failure(session, "stage_project_paths", {"paths": ["ignored.txt"]})
            assert before == await git(tmp_path, "write-tree")
            (tmp_path / names[0]).write_text("changed after stage\n")
            assert "failed" in await failure(session, "unstage_project_paths", {"paths": [names[0]]})
            assert await git(tmp_path, "show", ":" + names[0]) == "original"
            assert (tmp_path / names[0]).read_text() == "changed after stage\n"
            for invalid in ([], ["../outside"], [str(tmp_path / "file")], ["bad\0name"]):
                await failure(session, "stage_project_paths", {"paths": invalid})
            await success(session, "stage_project_paths", {"paths": ["star*.txt"]})
            assert "star-other.txt" not in (await git(tmp_path, "ls-files", "-z")).split("\0")

    asyncio.run(scenario())


def test_status_rename_and_detached_head_and_partial_stage_replacement(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        file = tmp_path / "old name.txt"
        file.write_text("one\ntwo\n")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "first")
        file.rename(tmp_path / "new\nname.txt")
        await git(tmp_path, "add", "-A")
        async with client(tmp_path) as session:
            status = await success(session, "get_project_status")
            assert status["entries"] == [
                {
                    "kind": "renamed",
                    "path": "new\nname.txt",
                    "original_path": "old name.txt",
                    "index_status": "R",
                    "worktree_status": ".",
                    "submodule": "N...",
                }
            ]
            changed = tmp_path / "new\nname.txt"
            changed.write_text("one modified\ntwo\n")
            assert await git(tmp_path, "show", ":new\nname.txt") == "one\ntwo"
            await success(session, "stage_project_paths", {"paths": ["new\nname.txt"]})
            assert await git(tmp_path, "show", ":new\nname.txt") == "one modified\ntwo"
            await git(tmp_path, "checkout", "--detach", "--quiet")
            status = await success(session, "get_project_status")
            assert status["branch"] is None
            assert status["head"] == await git(tmp_path, "rev-parse", "HEAD")

    asyncio.run(scenario())


def test_commit_uses_prepared_dotenv_identity_without_local_configuration(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup, identity=False)
        configuration = await git(tmp_path, "config", "--local", "--list")
        configure_identity(tmp_path, "Prepared Writer", "prepared@example.invalid")
        (tmp_path / "selected.txt").write_text("prepared commit\n")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["selected.txt"]})
            committed = await success(session, "commit_project_changes", {"message": "prepared"})
            assert committed["success"] is True
            assert (await success(session, "get_project_head"))["head"] == committed["head_after"]
        assert await git(tmp_path, "show", "-s", "--format=%an <%ae>|%cn <%ce>") == (
            "Prepared Writer <prepared@example.invalid>|Prepared Writer <prepared@example.invalid>"
        )
        assert await git(tmp_path, "config", "--local", "--list") == configuration

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GIT_USER_NAME", " leading"),
        ("GIT_USER_NAME", "trailing "),
        ("GIT_USER_NAME", "   "),
        ("GIT_USER_NAME", "line\nbreak"),
        ("GIT_USER_NAME", "bad\0name"),
        ("GIT_USER_NAME", "name<suffix>"),
        ("GIT_USER_EMAIL", " leading@example.invalid"),
        ("GIT_USER_EMAIL", "line\rbreak@example.invalid"),
        ("GIT_USER_EMAIL", "<email@example.invalid>"),
    ],
)
def test_invalid_prepared_identity_is_redacted_and_preserves_index(
    tmp_path: Path, key: str, value: str, caplog: pytest.LogCaptureFixture, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        name = value if key == "GIT_USER_NAME" else "Prepared Writer"
        email = value if key == "GIT_USER_EMAIL" else "prepared@example.invalid"
        (tmp_path / ".env").write_text(f'GIT_USER_NAME="{name}"\nGIT_USER_EMAIL="{email}"\n')
        (tmp_path / "selected.txt").write_text("still staged\n")
        await git(tmp_path, "add", "selected.txt")
        before = await git(tmp_path, "write-tree")
        async with client(tmp_path) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert key in error
            assert value not in error
        assert all(value not in record.getMessage() for record in caplog.records)
        assert await git(tmp_path, "write-tree") == before

    asyncio.run(scenario())


@pytest.mark.parametrize("prepared", [False, True])
def test_commit_requires_dotenv_identity_and_preserves_preexisting_staged_content(
    signing_setup: SigningSetup, tmp_path: Path, prepared: bool, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        if not prepared:
            (tmp_path / ".env").unlink()
        (tmp_path / "preexisting.txt").write_text("user stage\n")
        await git(tmp_path, "add", ".")
        if not prepared:
            with pytest.raises(ConfigurationError, match=r"\.env"):
                client(tmp_path, signing=signing_setup)
            assert await git(tmp_path, "ls-files") == "preexisting.txt"
            return
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "commit_project_changes", {"message": "first"})
            assert await git(tmp_path, "show", "HEAD:preexisting.txt") == "user stage"
            assert await git(tmp_path, "show", "-s", "--format=%an <%ae>") == "Test Writer <writer@example.invalid>"
            for message in ("", "  \n", "bad\0message"):
                await failure(session, "commit_project_changes", {"message": message})

    asyncio.run(scenario())


def test_prepared_identity_overrides_local_and_ambient_values_and_stays_frozen(
    signing_setup: SigningSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        for role in ("author", "committer"):
            await git(tmp_path, "config", f"{role}.name", "Repository Identity")
            await git(tmp_path, "config", f"{role}.email", "repository@example.invalid")
        for role in ("AUTHOR", "COMMITTER", "USER"):
            monkeypatch.setenv(f"GIT_{role}_NAME", "Ambient Identity")
            monkeypatch.setenv(f"GIT_{role}_EMAIL", "ambient@example.invalid")
        configuration = await git(tmp_path, "config", "--local", "--list")
        configure_identity(tmp_path, "미아 개발자", "local-identity")
        file = tmp_path / "selected.txt"
        file.write_text("first\n")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["selected.txt"]})
            await success(session, "commit_project_changes", {"message": "first"})
            assert await git(tmp_path, "show", "-s", "--format=%an <%ae>|%cn <%ce>") == (
                "미아 개발자 <local-identity>|미아 개발자 <local-identity>"
            )
            configure_identity(tmp_path, "Next Writer", "next@example.invalid")
            file.write_text("second\n")
            await success(session, "stage_project_paths", {"paths": ["selected.txt"]})
            await success(session, "commit_project_changes", {"message": "second"})
            assert await git(tmp_path, "show", "-s", "--format=%an <%ae>|%cn <%ce>") == (
                "미아 개발자 <local-identity>|미아 개발자 <local-identity>"
            )
            (tmp_path / ".env").unlink()
            with pytest.raises(ConfigurationError, match=r"\.env"):
                client(tmp_path, signing=signing_setup)
            assert await git(tmp_path, "show", "-s", "--format=%s") == "second"
        assert await git(tmp_path, "config", "--local", "--list") == configuration
        assert os.environ["GIT_AUTHOR_NAME"] == "Ambient Identity"

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable Git hook fixture")
def test_commit_keeps_one_identity_snapshot_and_does_not_pass_unrelated_dotenv_values(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        configure_identity(tmp_path, "Before Hook", "before@example.invalid")
        with (tmp_path / ".env").open("a") as env_file:
            env_file.write("CONTROL_PLANE_API_KEY=not-a-real-key\nOTHER_SECRET=not-a-real-secret\n")
        updated = 'GIT_USER_NAME="After Hook"\nGIT_USER_EMAIL=after@example.invalid\n'
        hook = tmp_path / ".git/hooks/pre-commit"
        hook.write_text(
            f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
            "assert os.environ['GIT_AUTHOR_NAME'] == 'Before Hook'\n"
            "assert os.environ['GIT_COMMITTER_NAME'] == 'Before Hook'\n"
            "assert 'CONTROL_PLANE_API_KEY' not in os.environ\n"
            "assert 'OTHER_SECRET' not in os.environ\n"
            "assert 'GIT_USER_NAME' not in os.environ\n"
            f"Path({str(tmp_path / '.env')!r}).write_text({updated!r})\n"
        )
        hook.chmod(0o700)
        (tmp_path / "selected.txt").write_text("snapshot\n")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["selected.txt"]})
            await success(session, "commit_project_changes", {"message": "snapshot"})
        assert await git(tmp_path, "show", "-s", "--format=%an <%ae>|%cn <%ce>") == (
            "Before Hook <before@example.invalid>|Before Hook <before@example.invalid>"
        )
        assert (tmp_path / ".env").read_text() == updated

    asyncio.run(scenario())


def test_missing_environment_prevents_server_start_and_preserves_staged_changes(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / ".env").unlink()
        (tmp_path / "selected.txt").write_text("keep staged\n")
        await git(tmp_path, "add", "selected.txt")
        before = await git(tmp_path, "write-tree")
        with pytest.raises(ConfigurationError) as error:
            client(tmp_path)
        assert ".env" in str(error.value)
        assert await git(tmp_path, "write-tree") == before

    asyncio.run(scenario())


@pytest.mark.parametrize("deny_path", [False, True])
def test_startup_policy_denial_preserves_index(
    tmp_path: Path, deny_path: bool, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        (tmp_path / "data").write_text("content")
        await git(tmp_path, "add", ".")
        before = await git(tmp_path, "write-tree")
        if deny_path:
            with pytest.raises(PolicyError, match="not allowed"):
                client(tmp_path, allowed=False)
        else:
            async with client(tmp_path, commands=()) as session:
                assert "not allowed" in await failure(session, "get_project_status")
        assert before == await git(tmp_path, "write-tree")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"]
)
def test_in_progress_operation_blocks_writes_but_not_reads(
    signing_setup: SigningSetup, tmp_path: Path, state: str, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / "data").write_text("content")
        marker = tmp_path / ".git" / state
        if state.endswith("HEAD"):
            marker.write_text("0" * 40 + "\n")
        else:
            marker.mkdir()
        async with client(tmp_path, signing=signing_setup) as session:
            # Diff can be queried even when no commit exists or an operation is active.
            await success(session, "get_project_diff")
            assert "not supported" in await failure(session, "stage_project_paths", {"paths": ["."]})
            assert "not supported" in await failure(session, "unstage_project_paths", {"paths": ["."]})
            assert "not supported" in await failure(session, "commit_project_changes", {"message": "blocked"})
        assert await git(tmp_path, "ls-files") == ""

    asyncio.run(scenario())


def test_query_output_cap_and_write_readback_failure(
    signing_setup: SigningSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / "large").write_text("x" * 4096 + "\n")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["large"]})
            with monkeypatch.context() as patch:
                patch.setattr(_execution, "OUTPUT_LIMIT", 128)
                assert "exceeded 1 MiB" in await failure(session, "get_project_diff", {"staged": True})
                assert "exceeded 1 MiB" in await failure(session, "get_project_status")
            await success(session, "commit_project_changes", {"message": "first"})
            (tmp_path / "large").write_text("new\n")
            await success(session, "stage_project_paths", {"paths": ["large"]})
            original = _repository.read_head
            observations = 0

            async def head(self: _execution.GitExecution) -> str | None:
                nonlocal observations
                observations += 1
                if observations > 1:
                    from fastmcp.exceptions import ToolError

                    raise ToolError("test-only observation failure")
                return await original(self)

            with monkeypatch.context() as patch:
                patch.setattr(_repository, "read_head", head)
                result = await success(session, "commit_project_changes", {"message": "second"})
            assert result["success"] is True
            assert result["head_observed"] is False
            assert result["observation_error"]
            assert await git(tmp_path, "show", "-s", "--format=%s") == "second"

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="executable hook fixture")
def test_hook_failure_and_signature_failure_are_not_bypassed(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / "data").write_text("content")
        await git(tmp_path, "add", ".")
        hook = tmp_path / ".git/hooks/pre-commit"
        hook.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
            'Path("hook-ran").write_text("yes")\nprint("secret-hook-output")\nsys.exit(1)\n'
        )
        hook.chmod(0o755)
        async with client(tmp_path, signing=signing_setup) as session:
            error = await failure(session, "commit_project_changes", {"message": "blocked"})
            assert "secret-hook-output" not in error
            assert (tmp_path / "hook-ran").exists()
            assert "data" in await git(tmp_path, "ls-files")
            hook.unlink()
            await git(tmp_path, "config", "commit.gpgSign", "true")
            await git(tmp_path, "config", "gpg.program", "nonexistent-test-signer")
            signed = await success(session, "commit_project_changes", {"message": "signed"})
            assert signed["signature_verified"] is True
            (tmp_path / "data").write_text("next content")
            await success(session, "stage_project_paths", {"paths": ["data"]})
            await git(tmp_path, "config", "commit.gpgSign", "false")
            await success(session, "commit_project_changes", {"message": "allowed"})

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="executable filter fixture")
def test_required_filter_failure_does_not_stage_or_leak_output(
    tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        script = tmp_path / ".git/failing-filter"
        script.write_text(
            f'#!{sys.executable}\nimport sys\nprint("secret-filter-output",file=sys.stderr)\nsys.exit(1)\n'
        )
        script.chmod(0o755)
        await git(tmp_path, "config", "filter.test.clean", str(script))
        await git(tmp_path, "config", "filter.test.required", "true")
        (tmp_path / ".gitattributes").write_text("*.dat filter=test\n")
        (tmp_path / "data.dat").write_text("raw")
        async with client(tmp_path) as session:
            error = await failure(session, "stage_project_paths", {"paths": ["data.dat"]})
            assert "secret-filter-output" not in error
            assert await git(tmp_path, "ls-files") == ""

    asyncio.run(scenario())


def test_root_not_parent_and_symlink_traversal(tmp_path: Path, workspace_factory: WorkspaceFactory) -> None:
    async def scenario() -> None:
        await workspace_factory()
        child = tmp_path / "child"
        child.mkdir()
        configure_identity(child)
        async with client(child) as session:
            assert "root" in await failure(session, "stage_project_paths", {"paths": ["."]})
        outside = tmp_path.parent / (tmp_path.name + "-external")
        outside.mkdir()
        try:
            (outside / "secret").write_text("outside")
            (tmp_path / "link").symlink_to(outside, target_is_directory=True)
            async with client(tmp_path) as session:
                assert "symlink" in await failure(session, "stage_project_paths", {"paths": ["link/secret"]})
                await success(session, "stage_project_paths", {"paths": ["link"]})
                assert await git(tmp_path, "show", ":link") == str(outside)
        finally:
            (outside / "secret").unlink()
            outside.rmdir()

    asyncio.run(scenario())


def test_sha256_head_and_status_are_full_length(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup, sha256=True)
        (tmp_path / "data").write_text("content")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["."]})
            result = await success(session, "commit_project_changes", {"message": "sha256"})
            assert len(str(result["head_after"])) == 64
            assert (await success(session, "get_project_head"))["head"] == result["head_after"]
            assert (await success(session, "get_project_status"))["head"] == result["head_after"]

    asyncio.run(scenario())


def test_unmerged_index_blocks_writes_without_operation_marker(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        file = tmp_path / "data"
        file.write_text("base\n")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "base")
        base = await git(tmp_path, "rev-parse", "HEAD")
        await git(tmp_path, "checkout", "-b", "side")
        file.write_text("side\n")
        await git(tmp_path, "commit", "-am", "side")
        await git(tmp_path, "checkout", "--detach", base)
        file.write_text("other\n")
        await git(tmp_path, "commit", "-am", "other")
        merge = await sh.result("git", "-C", str(tmp_path), "merge", "side").set(
            inherit_env=False, env=command_environment(), timeout=10
        )
        assert merge.exit_code != 0
        (tmp_path / ".git/MERGE_HEAD").unlink()  # Isolate the unmerged-index guard in this fixture.
        async with client(tmp_path, signing=signing_setup) as session:
            status = await success(session, "get_project_status")
            assert "unmerged" in str(status["entries"])
            await success(session, "get_project_diff")
            for tool in ("stage_project_paths", "unstage_project_paths", "commit_project_changes"):
                arguments: dict[str, object] = {"message": "blocked"} if tool.startswith("commit") else {"paths": ["."]}
                assert "unresolved" in await failure(session, tool, arguments)

    asyncio.run(scenario())


def test_linked_worktree_root_and_commit_message_option_literal(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / "base").write_text("first\n")
        await git(tmp_path, "add", ".")
        await git(tmp_path, "commit", "-m", "base")
        old = await git(tmp_path, "rev-parse", "HEAD")
        linked = tmp_path / "linked"
        await git(tmp_path, "worktree", "add", "--detach", str(linked))
        (linked / ".env").write_text(signing_setup.dotenv())
        configure_identity(linked)
        (linked / "base").write_text("next\n")
        async with client(linked, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["base"]})
            result = await success(session, "commit_project_changes", {"message": "--amend"})
            assert result["head_before"] == old
            assert await git(linked, "rev-parse", "HEAD^") == old
            assert await git(linked, "show", "-s", "--format=%s") == "--amend"
            assert (await success(session, "get_project_status"))["branch"] is None
        assert await git(tmp_path, "rev-parse", "HEAD") == old

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable fixture and independent PID observation")
def test_capture_limit_drains_then_reports_error_and_timeout_reaps_direct_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp.exceptions import ToolError

    binary = tmp_path / "git"
    binary.write_text(
        f"#!{sys.executable}\nimport os,sys,time\nfrom pathlib import Path\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "if sys.argv[-1] == 'burst':\n"
        "    sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n"
        "    sys.stdout.flush()\n"
        "    Path('drained').write_text('yes')\n"
        "else:\n"
        "    time.sleep(10)\n"
    )
    binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / ".env").write_text("GIT_USER_NAME=Test\nGIT_USER_EMAIL=test@example.invalid\n")
    repo = _execution.GitExecution(ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git",))))

    async def scenario() -> None:
        with pytest.raises(ToolError, match="exceeded 1 MiB"):
            await repo.run("burst", operation="Capture fixture")
        assert (tmp_path / "drained").read_text() == "yes"
        monkeypatch.setattr(_execution, "READ_TIMEOUT", 0.1)
        with pytest.raises(ToolError, match="timed out"):
            await repo.run("sleep", operation="Timeout fixture")
        pid = int((tmp_path / "child.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="executable hook fixture")
def test_write_options_disable_only_automatic_maintenance(
    signing_setup: SigningSetup, tmp_path: Path, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        (tmp_path / "data").write_text("content")
        hook = tmp_path / ".git/hooks/pre-commit"
        hook.write_text(
            f"#!{sys.executable}\nimport subprocess\nfrom pathlib import Path\n"
            "values = [subprocess.check_output(['git','config','--get',key],text=True).strip() "
            "for key in ['maintenance.auto','gc.auto']]\n"
            "Path('.git/observed-options').write_text(','.join(values))\n"
        )
        hook.chmod(0o755)
        await git(tmp_path, "config", "maintenance.auto", "true")
        await git(tmp_path, "config", "gc.auto", "999")
        async with client(tmp_path, signing=signing_setup) as session:
            await success(session, "stage_project_paths", {"paths": ["data"]})
            await success(session, "commit_project_changes", {"message": "options"})
        assert (tmp_path / ".git/observed-options").read_text() == "false,0"
        assert await git(tmp_path, "config", "--local", "maintenance.auto") == "true"
        assert await git(tmp_path, "config", "--local", "gc.auto") == "999"

    asyncio.run(scenario())


@pytest.mark.parametrize("denial", ["path", "git", "gpg"])
def test_direct_git_execution_requires_policy_before_process_start(tmp_path: Path, denial: str) -> None:
    from local_dev_mcp.tools.git import PreparedGitCommit
    from local_dev_mcp.tools.git._prepare import prepare_git_commit
    from local_dev_mcp.tools.gpg.configuration import GpgSigning

    home = tmp_path / "keys"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    signing = GpgSigning("A" * 40, tmp_path / "missing-gpg", home, tmp_path / "missing-wrapper")
    preparation = PreparedGitCommit("Test", "test@example.invalid", signing)
    policy = Policy(
        workspace,
        () if denial == "path" else (workspace,),
        ("gpg",) if denial == "git" else ("git",) if denial == "gpg" else ("git", "gpg"),
    )
    settings = ServerSettings(policy, tmp_path / "missing-git", preparation, ())

    async def scenario() -> None:
        with pytest.raises(ToolError, match="not allowed"):
            await _execution.GitExecution(settings, preparation).run("status", operation="Denied direct execution")
        with pytest.raises(ToolError, match="not allowed"):
            await prepare_git_commit(settings)

    asyncio.run(scenario())


@pytest.mark.parametrize("detached", [False, True])
def test_head_does_not_treat_broken_reference_as_unborn(
    tmp_path: Path, detached: bool, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory()
        missing = "f" * 40
        if detached:
            (tmp_path / ".git/HEAD").write_text(missing + "\n")
        else:
            (tmp_path / ".git/HEAD").write_text("ref: refs/heads/broken\n")
            (tmp_path / ".git/refs/heads/broken").write_text(missing + "\n")
        async with client(tmp_path) as session:
            message = await failure(session, "get_project_head")
            assert "Cannot resolve HEAD" in message
            assert "at least one commit" not in message

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable fixture and independent PID observation")
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
def test_head_total_budget_and_cancellation_reap_current_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from types import SimpleNamespace

    binary = tmp_path / "git"
    binary.write_text(
        f"#!{sys.executable}\nimport os,signal,sys\nfrom pathlib import Path\n"
        "phase = 'root' if '--show-toplevel' in sys.argv else 'head'\n"
        "Path(phase + '.pid').write_text(str(os.getpid()))\n"
        "if phase == 'head': signal.pause()\n"
        "Path(phase + '.finished').touch()\n"
        "print(str(Path.cwd()) if phase == 'root' else 'a' * 40)\n"
    )
    binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    configure_identity(tmp_path)
    lookup_timeout: asyncio.Timeout | None = None

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        nonlocal lookup_timeout
        assert lookup_timeout is None
        assert not (tmp_path / "root.pid").exists()
        lookup_timeout = asyncio.timeout(delay)
        return lookup_timeout

    # Keep the real operation-wide timer; interrupt it after the second child enters.
    # Rebind only this module's lookup, leaving library and watchdog timers untouched.
    monkeypatch.setattr(get_project_head, "asyncio", SimpleNamespace(timeout=capture_timeout))

    async def scenario() -> None:
        async def lookup() -> str | None:
            if interruption == "deadline":
                async with client(tmp_path) as session:
                    return await failure(session, "get_project_head")
            settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git",)))
            await get_project_head.GetProjectHeadTool(settings).execute()
            return None

        task = asyncio.create_task(lookup())
        try:
            async with asyncio.timeout(5):
                while not (tmp_path / "head.pid").exists():
                    if task.done():
                        await task
                        pytest.fail("HEAD lookup ended before the second child entered.")
                    await asyncio.sleep(0.01)
            assert lookup_timeout is not None
            assert (tmp_path / "root.finished").exists()
            if interruption == "deadline":
                lookup_timeout.reschedule(asyncio.get_running_loop().time())
                message = await task
                assert message is not None and "Git HEAD lookup timed out" in message
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert (tmp_path / "root.finished").exists()
        assert not (tmp_path / "head.finished").exists()
        for phase in ("root", "head"):
            pid = int((tmp_path / f"{phase}.pid").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    asyncio.run(scenario())


def test_direct_signed_git_execution_accepts_external_home_with_gpg_permission(
    tmp_path: Path, signing_setup: SigningSetup, workspace_factory: WorkspaceFactory
) -> None:
    async def scenario() -> None:
        await workspace_factory(signing=signing_setup)
        assert not signing_setup.home.is_relative_to(tmp_path)
        settings = ServerSettings.load(Policy(tmp_path, (tmp_path,), ("git", "gpg")))
        result = await _execution.GitExecution(settings, settings.require_commit()).run(
            "status", "--porcelain", operation="Direct Git execution"
        )
        assert result.code == 0

    asyncio.run(scenario())
