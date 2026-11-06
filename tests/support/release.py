"""Small real-project setup shared by release integration scenarios."""

from pathlib import Path

from tests.support.git import WorkspaceFactory, git


def record_text(*, impact: str = "minor", supersedes: tuple[str, ...] = ()) -> str:
    targets = ", ".join(f'"{item}"' for item in supersedes)
    return (
        f'impact = "{impact}"\n'
        'summary = "Add a public capability"\n'
        'reason = "Existing public calls remain valid"\n'
        'contract = "MCP"\n'
        'assessed_by = "agent"\n'
        'migration = "Update the old required argument"\n'
        f"supersedes = [{targets}]\n"
    )


async def release_workspace(root: Path, factory: WorkspaceFactory, *, pending: bool = False) -> str:
    await factory()
    await git(root, "branch", "-M", "main")
    project = '[project]\nname = "release-probe"\nversion = "1.2.3"\nrequires-python = ">=3.15"\n'
    (root / "pyproject.toml").write_text(project)
    await git(root, "add", "pyproject.toml")
    await git(root, "commit", "-m", "initial project")
    initial = await git(root, "rev-parse", "HEAD")
    (root / "pyproject.toml").write_text(
        project + f'\n[tool.local-dev-mcp.release]\nenabled = true\ninitial_base = "{initial}"\n'
    )
    (root / ".changes").mkdir()
    if pending:
        (root / ".changes/previous.toml").write_text(record_text())
    else:
        (root / ".changes/README.md").write_text("Pending assessments\n")
    await git(root, "add", ".")
    await git(root, "commit", "-m", "configure workflow")
    return await git(root, "rev-parse", "HEAD")


async def feature(root: Path, *, assessment: bool = True) -> str:
    await git(root, "switch", "-c", "feat/example")
    (root / "feature.txt").write_text("a public feature\n")
    if assessment:
        (root / ".changes/example.toml").write_text(record_text())
    await git(root, "add", ".")
    await git(root, "commit", "-m", "feat: add example")
    return await git(root, "rev-parse", "HEAD")
