"""Check repository documentation links and accepted research file placement."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
RESEARCH = DOCS / "research"


def test_research_documents_follow_accepted_location() -> None:
    assert RESEARCH.is_dir()
    assert tuple(RESEARCH.glob("*-research.md"))
    assert not tuple(DOCS.glob("*-research.md"))


@pytest.mark.parametrize(
    "document",
    [
        ROOT / "README.md",
        ROOT / "infra" / "postgres" / "README.md",
        DOCS / "README.md",
        DOCS / "testing.md",
        DOCS / "conventions.md",
        DOCS / "tools" / "release" / "README.md",
        DOCS / "tools" / "git" / "README.md",
        DOCS / "adr" / "0005-assessed-squash-and-batched-releases.md",
        DOCS / "contracts" / "event-core.md",
        DOCS / "contracts" / "github-durable-delivery.md",
        DOCS / "contracts" / "github-webhook-verification.md",
        DOCS / "contracts" / "github-operation-plan.md",
        RESEARCH / "b3s-delivery-compatibility-2026-11-02.md",
        RESEARCH / "testing-policy-research.md",
        RESEARCH / "cli-task-policy-research.md",
    ],
)
def test_changed_document_links_point_to_existing_paths(document: Path) -> None:
    broken: list[str] = []
    for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        if "://" in target or target.startswith("#"):
            continue
        relative_path = target.split("#", 1)[0]
        if relative_path and not (document.parent / relative_path).exists():
            broken.append(target)
    assert not broken, f"{document.relative_to(ROOT)}: missing local links {broken}"


def test_readme_tool_table_matches_explicit_registry() -> None:
    from local_dev_mcp.mcp.server import TOOLS

    section = (ROOT / "README.md").read_text(encoding="utf-8").split("## 도구 사용\n", 1)[1].split("\n## ", 1)[0]
    documented = re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", section, flags=re.MULTILINE)
    registered = [definition.name for definition in TOOLS]
    assert len(documented) == len(set(documented)), "README tool table has duplicate names"
    assert set(documented) == set(registered)
