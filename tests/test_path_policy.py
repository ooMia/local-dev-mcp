"""Pure path relations for public file access and direct writes."""

from pathlib import Path

import pytest

from local_dev_mcp.settings import Policy, protected_name

pytestmark = pytest.mark.unit

ROOT = Path("/synthetic/workspace")


def test_subdirectory_grants_allow_navigation_without_granting_sibling_access() -> None:
    policy = Policy(ROOT, (ROOT / "src/library", ROOT / "docs/guide.md"), ())

    policy.require_project_access()
    assert policy.allows_path(ROOT, traverse=True)
    assert policy.allows_path(ROOT / "src", traverse=True)
    assert policy.allows_path(ROOT / "docs", traverse=True)
    assert policy.allows_path(ROOT / "src/library/code.py", write=True)
    assert policy.allows_path(ROOT / "docs/guide.md", write=True)
    assert not policy.allows_path(ROOT)
    assert not policy.allows_path(ROOT, write=True, traverse=True)
    assert not policy.allows_path(ROOT / "src/sibling.py", traverse=True)
    assert not policy.allows_path(ROOT / "docs/other.md")
    assert not policy.allows_path(ROOT / "docs/guide.md.bak")


def test_denied_scopes_override_broader_and_more_specific_grants() -> None:
    policy = Policy(ROOT, (ROOT, ROOT / "private/reallowed"), (), denied_paths=(ROOT / "private",))

    assert not policy.allows_path(ROOT / "private", traverse=True)
    assert not policy.allows_path(ROOT / "private/key.json")
    assert not policy.allows_path(ROOT / "private/reallowed/file.py", write=True)
    assert policy.allows_path(ROOT / "private-other/file.py", write=True)


def test_read_only_scopes_preserve_reads_and_override_nested_write_grants() -> None:
    policy = Policy(ROOT, (ROOT, ROOT / "vendor/reallowed"), (), read_only_paths=(ROOT / "vendor",))

    assert policy.allows_path(ROOT / "vendor/library.py")
    assert policy.allows_path(ROOT / "vendor", traverse=True)
    assert not policy.allows_path(ROOT / "vendor/library.py", write=True)
    assert not policy.allows_path(ROOT / "vendor/reallowed/file.py", write=True)
    assert policy.allows_path(ROOT / "vendor-other.py", write=True)


def test_read_only_does_not_grant_outside_allowed_paths() -> None:
    policy = Policy(ROOT, (ROOT / "src",), (), read_only_paths=(ROOT / "docs",))

    assert not policy.allows_path(ROOT / "docs", traverse=True)
    assert not policy.allows_path(ROOT / "docs/guide.md")
    assert policy.allows_path(ROOT / "src/code.py", write=True)


@pytest.mark.parametrize("grants", [(), (ROOT / "private",), (ROOT / ".git",), (ROOT / ".env",)])
def test_startup_requires_some_accessible_workspace_scope(grants: tuple[Path, ...]) -> None:
    policy = Policy(ROOT, grants, (), denied_paths=(ROOT / "private",))

    with pytest.raises(PermissionError, match="Project directory is not allowed"):
        policy.require_project_access()


def test_denied_workspace_blocks_startup_even_with_a_parent_grant() -> None:
    policy = Policy(ROOT, (ROOT.parent,), (), denied_paths=(ROOT,))

    with pytest.raises(PermissionError):
        policy.require_project_access()


@pytest.mark.parametrize("path", [Path("relative"), ROOT / ".." / "outside", ROOT.parent / "outside", ROOT / "\0"])
def test_noncanonical_or_external_paths_are_not_granted(path: Path) -> None:
    policy = Policy(ROOT, (ROOT.parent,), ())

    assert not policy.allows_path(path)
    assert not policy.allows_path(path, write=True, traverse=True)


@pytest.mark.parametrize("name", [".git", ".GIT", ".env", ".ENV", ".env.local", ".env.Example"])
def test_builtin_protection_cannot_be_overridden_by_a_grant(name: str) -> None:
    path = ROOT / "src" / name
    policy = Policy(ROOT, (ROOT, path), ())

    assert protected_name(name)
    assert not policy.allows_path(path, traverse=True)
    assert not policy.allows_path(path / "nested", write=True)


def test_example_environment_file_remains_explicitly_available() -> None:
    policy = Policy(ROOT, (ROOT,), ())

    assert not protected_name(".env.example")
    assert policy.allows_path(ROOT / ".env.example", write=True)


@pytest.mark.parametrize(
    "path", [ROOT / "config.toml", ROOT / "CONFIG.TOML", ROOT / ".gitignore", ROOT / "src/.gitignore"]
)
def test_policy_control_files_remain_readable_but_not_directly_writable(path: Path) -> None:
    policy = Policy(ROOT, (ROOT,), ())

    assert policy.allows_path(path)
    assert not policy.allows_path(path, write=True)
    assert policy.allows_path(ROOT / "src/config.toml", write=True)


def test_restrictions_cover_case_aliases_without_widening_positive_grants() -> None:
    policy = Policy(
        ROOT,
        (ROOT,),
        (),
        read_only_paths=(ROOT / "vendor",),
        denied_paths=(ROOT / "credentials.json",),
    )

    assert not policy.allows_path(ROOT / "VENDOR/library.py", write=True)
    assert not policy.allows_path(ROOT / "CREDENTIALS.JSON")
    narrow = Policy(ROOT, (ROOT / "src",), ())
    assert not narrow.allows_path(ROOT / "SRC/code.py")


def test_permission_diagnostics_do_not_echo_paths_or_caller_values() -> None:
    policy = Policy(ROOT, (), ())

    with pytest.raises(PermissionError) as caught:
        policy.require_path_access(ROOT / "private-marker", write=True)

    assert "private-marker" not in str(caught.value)
    assert "read_only_paths" in str(caught.value)


def test_policy_edit_optout_removes_only_the_builtin_policy_file_write_block() -> None:
    policy = Policy(
        ROOT,
        (ROOT,),
        (),
        read_only_paths=(ROOT / "reference",),
        denied_paths=(ROOT / "private",),
        allow_policy_edits=True,
    )

    for path in (ROOT / "config.toml", ROOT / ".gitignore", ROOT / "src/.gitignore"):
        assert policy.allows_path(path, write=True)
    for path in (
        ROOT / "reference/.gitignore",
        ROOT / "private/.gitignore",
        ROOT / ".env",
        ROOT / ".git/config",
        ROOT.parent / ".gitignore",
    ):
        assert not policy.allows_path(path, write=True)
    scoped = Policy(ROOT, (ROOT / "src",), (), allow_policy_edits=True)
    assert not scoped.allows_path(ROOT / "config.toml", write=True)
    assert scoped.allows_path(ROOT / "src/.gitignore", write=True)
