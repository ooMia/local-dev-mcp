import asyncio
from pathlib import Path

import pytest

from local_dev_mcp.errors import LocalIOError
from local_dev_mcp.settings import ConfigurationError, Policy
from local_dev_mcp.tools.workspace import WorkspaceFiles
from tests.support.workspace import WorkspaceConfigFactory

pytestmark = pytest.mark.integration


def test_project_path_permission_does_not_require_git(tmp_path: Path, workspace_config: WorkspaceConfigFactory) -> None:
    workspace_config()
    policy = Policy.load(tmp_path)
    policy.require_project_access()
    with pytest.raises(PermissionError, match="git is not allowed"):
        policy.require_command("git")


def test_config_does_not_search_parent(tmp_path: Path, workspace_config: WorkspaceConfigFactory) -> None:
    workspace_config()
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(ConfigurationError):
        Policy.load(child)


def test_scoped_access_paths_resolve_against_the_selected_workspace(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["src", "docs"]\nread_only_paths=["docs"]\n'
        'denied_paths=["src/private"]\n[system]\nallowed_commands=[]'
    )

    policy = Policy.load(tmp_path)

    assert policy.allowed_paths == (tmp_path / "src", tmp_path / "docs")
    assert policy.read_only_paths == (tmp_path / "docs",)
    assert policy.denied_paths == (tmp_path / "src/private",)
    policy.require_project_access()
    policy.require_path_access(tmp_path / "src/code.py", write=True)
    policy.require_path_access(tmp_path / "docs/guide.md")
    with pytest.raises(PermissionError):
        policy.require_path_access(tmp_path / "docs/guide.md", write=True)
    with pytest.raises(PermissionError):
        policy.require_path_access(tmp_path / "src/private/key.json")


@pytest.mark.parametrize("setting", ["allowed_paths", "read_only_paths", "denied_paths"])
def test_invalid_configured_path_reports_its_rule_without_the_value(tmp_path: Path, setting: str) -> None:
    allowed = '["invalid\\u0000private-marker"]' if setting == "allowed_paths" else '["."]'
    optional = "" if setting == "allowed_paths" else f'\n{setting}=["invalid\\u0000private-marker"]'
    (tmp_path / "config.toml").write_text(
        f"[filesystem]\nallowed_paths={allowed}{optional}\n[system]\nallowed_commands=[]"
    )

    with pytest.raises(ConfigurationError) as caught:
        Policy.load(tmp_path)

    assert caught.value.target == f"filesystem.{setting}"
    assert "private-marker" not in caught.value.diagnostic()


def test_optional_path_resolution_failure_names_the_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["."]\ndenied_paths=["unavailable"]\n[system]\nallowed_commands=[]'
    )
    resolve = Path.resolve

    def fail_selected_path(path: Path, strict: bool = False) -> Path:
        if path.name == "unavailable":
            raise PermissionError("private-marker")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_selected_path)

    with pytest.raises(LocalIOError) as caught:
        Policy.load(tmp_path)

    assert caught.value.target == "filesystem.denied_paths"
    assert "private-marker" not in caught.value.diagnostic()


@pytest.mark.parametrize("restriction", ["none", "readonly", "ignored"])
def test_policy_edit_flag_preserves_other_write_restrictions_and_existing_read_refs(
    tmp_path: Path, restriction: str
) -> None:
    readonly = 'read_only_paths=["config.toml"]\n' if restriction == "readonly" else ""
    original = f'[filesystem]\nallowed_paths=["."]\n{readonly}[system]\nallowed_commands=[]\n'
    target = tmp_path / "config.toml"
    target.write_text(original, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("config.toml\n" if restriction == "ignored" else "", encoding="utf-8")
    policy = Policy.load(tmp_path, allow_policy_edits=True)
    files = WorkspaceFiles(policy)

    async def scenario() -> None:
        observed = await files.read_file("config.toml", 1, 200)
        assert observed.read_ref is not None
        changed = await files.replace_file("config.toml", observed.read_ref, original + "# edited\n")
        assert changed.outcome == ("applied" if restriction == "none" else "rejected")
        assert changed.changed is (restriction == "none")
        expected = original + "# edited\n" if restriction == "none" else original
        assert target.read_text(encoding="utf-8") == expected
        if restriction == "none":
            assert changed.read_ref is not None
            target.write_text(expected + "# user edit\n", encoding="utf-8")
            rejected = await files.replace_file("config.toml", changed.read_ref, "must not replace user edit\n")
            assert rejected.code == "stale_version"
            assert target.read_text(encoding="utf-8") == expected + "# user edit\n"

    asyncio.run(scenario())


def test_loading_jobs_and_optout_keeps_them_in_the_selected_workspace_snapshot(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["checks.unit"]\n'
        '[jobs.unit]\nkind="test"\npermission="checks.unit"\nargv=["pytest"]\n'
        'level="unit"\ndefault_targets=["tests"]\n[jobs_environment]\npublic_names=["APP_MODE"]\n',
        encoding="utf-8",
    )

    policy = Policy.load(tmp_path, allow_policy_edits=True)

    assert policy.allow_policy_edits
    assert policy.jobs[0].name == "unit"
    assert policy.public_environment == ("APP_MODE",)
    assert not Policy.load(tmp_path).allow_policy_edits
