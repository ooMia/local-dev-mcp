"""Validate installed entry points rather than importing source as evidence."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from shellous import sh

from local_dev_mcp.execution import base_environment
from tests.support.git import WorkspaceFactory, git
from tests.support.packaging import InstalledPackage
from tests.support.signing import SigningSetup

pytestmark = [pytest.mark.integration, pytest.mark.packaging]


def test_installed_cli_stdio_signed_commit(
    installed_package: InstalledPackage,
    workspace_factory: WorkspaceFactory,
    signing_setup: SigningSetup,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    launcher = tmp_path_factory.mktemp("installed-launcher")
    # A valid target must work even when the invocation directory is unusable.
    (launcher / "config.toml").write_text("[invalid caller config", encoding="utf-8")
    (launcher / ".env").write_text("FASTMCP_LOG_LEVEL=invalid-caller-canary\n", encoding="utf-8")
    original = {name: (launcher / name).read_bytes() for name in ("config.toml", ".env")}

    async def scenario() -> None:
        root = await workspace_factory(signing=signing_setup)
        (root / "config.toml").write_text(
            "[filesystem]\nallowed_paths="
            + json.dumps([str(root)])
            + '\n[system]\nallowed_commands=["git", "gpg", "uv.release", "python.run.lint"]\n'
            + "[jobs.packaged_lint]\nkind='lint'\npermission='python.run.lint'\nargv="
            + json.dumps([str(installed_package.python), "-I", "-c", "print('installed recipe verified')"])
            + "\n"
        )
        # -I and a fresh launcher keep source-tree imports out of this probe.
        module = await sh(
            installed_package.python,
            "-I",
            "-c",
            "from pathlib import Path; import local_dev_mcp; print(Path(local_dev_mcp.__file__).resolve())",
        ).set(cwd=launcher, inherit_env=False, env=base_environment(), timeout=10)
        assert Path(module.strip()).is_relative_to(installed_package.python.parent.parent.resolve())
        assert installed_package.cli.is_file()
        assert installed_package.wrapper.is_file()
        preflight = await sh.result(
            installed_package.python,
            "-I",
            "-m",
            "local_dev_mcp.tools.tunnel_client._preflight",
            "--workspace",
            str(root),
        ).set(cwd=launcher, inherit_env=False, env={**base_environment(), "FASTMCP_ENV_FILE": os.devnull}, timeout=10)
        assert preflight.exit_code == 0
        assert json.loads(preflight.output) == {"warnings": []}
        (root / "data").write_text("installed signed commit\n")
        async with Client(
            StdioServerParameters(
                command=str(installed_package.cli),
                args=["--workspace", str(root), "run"],
                cwd=launcher,
                env=base_environment(),
            ),
            read_timeout_seconds=30,
            cache=None,
        ) as session:
            tools = await session.list_tools()
            assert "commit_project_changes" in [tool.name for tool in tools.tools]
            assert "read_workspace_file" in [tool.name for tool in tools.tools]
            assert "create_workspace_file" in [tool.name for tool in tools.tools]
            assert "delete_workspace_file" in [tool.name for tool in tools.tools]
            ready = await session.call_tool("get_tool_readiness")
            assert not ready.is_error and ready.structured_content is not None
            assert ready.structured_content["project"] == str(root.resolve())
            catalogue = await session.call_tool("list_project_tasks")
            assert not catalogue.is_error and catalogue.structured_content is not None
            assert catalogue.structured_content["tasks"]
            task = await session.call_tool(
                "start_project_task", {"task": "packaged_lint", "request_id": "installed-recipe-1"}
            )
            assert not task.is_error and task.structured_content is not None
            job_id = task.structured_content["job_id"]
            for _ in range(100):
                if task.structured_content is not None and task.structured_content["state"] != "running":
                    break
                await asyncio.sleep(0.02)
                task = await session.call_tool("get_project_task", {"job_id": job_id})
                assert not task.is_error
            assert task.structured_content is not None
            assert task.structured_content["state"] == "succeeded"
            assert task.structured_content["output_included"] is False
            assert task.structured_content["stdout"] == task.structured_content["stderr"] == ""
            task = await session.call_tool("get_project_task", {"job_id": job_id, "include_output": True})
            assert not task.is_error and task.structured_content is not None
            assert task.structured_content["output_included"] is True
            assert task.structured_content["stdout"] == "installed recipe verified\n"
            created = await session.call_tool(
                "create_workspace_file",
                {"path": "temporary.txt", "content": "installed creation\n", "request_id": "installed-create-1"},
            )
            assert not created.is_error and created.structured_content is not None
            assert created.structured_content["outcome"] == "applied"
            assert (root / "temporary.txt").read_text() == "installed creation\n"
            deletion = {
                "path": "temporary.txt",
                "read_ref": created.structured_content["read_ref"],
                "request_id": "installed-delete-1",
            }
            deleted = await session.call_tool("delete_workspace_file", deletion)
            assert not deleted.is_error and deleted.structured_content is not None
            assert deleted.structured_content["outcome"] == "applied"
            assert not (root / "temporary.txt").exists()
            (root / "temporary.txt").write_text("user recreated it\n")
            replayed = await session.call_tool("delete_workspace_file", deletion)
            assert not replayed.is_error and replayed.structured_content is not None
            assert replayed.structured_content["replayed"] is True
            assert (root / "temporary.txt").read_text() == "user recreated it\n"
            read = await session.call_tool("read_workspace_file", {"path": "data"})
            assert not read.is_error and read.structured_content is not None
            assert read.structured_content["content"] == "installed signed commit\n"
            assert read.structured_content["read_ref"]
            assert "installed signed commit" not in str(read.content)
            edited = await session.call_tool(
                "replace_workspace_file",
                {
                    "path": "data",
                    "read_ref": read.structured_content["read_ref"],
                    "replacement": "installed edited signed commit\n",
                },
            )
            assert not edited.is_error and edited.structured_content is not None
            assert edited.structured_content["outcome"] == "applied"
            assert edited.structured_content["read_ref"]
            assert edited.structured_content["content"] == "installed edited signed commit\n"
            assert (root / "data").read_text() == "installed edited signed commit\n"
            staged = await session.call_tool("stage_project_paths", {"paths": ["data"]})
            assert not staged.is_error, staged.content
            committed = await session.call_tool("commit_project_changes", {"message": "Installed package signing"})
            assert not committed.is_error, committed.content
            result = committed.structured_content
            assert result is not None
            assert result["success"] is True
            assert result["signing_requested"] is True
            assert result["signature_verified"] is True
            head = await session.call_tool("get_project_head")
            assert not head.is_error
            assert head.structured_content is not None
            assert head.structured_content["head"] == await git(root, "rev-parse", "HEAD")
        assert "gpgsig " in await git(root, "cat-file", "commit", "HEAD")
        assert {name: (launcher / name).read_bytes() for name in original} == original
        assert not (launcher / ".git").exists()

    asyncio.run(scenario())
