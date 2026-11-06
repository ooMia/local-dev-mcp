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
        DOCS / "README.md",
        DOCS / "testing.md",
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
