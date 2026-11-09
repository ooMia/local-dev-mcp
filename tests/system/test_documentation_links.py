"""Check repository documentation links and accepted research file placement."""

import html
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
RESEARCH = DOCS / "research"


def test_research_documents_follow_accepted_location() -> None:
    assert RESEARCH.is_dir()
    assert tuple(RESEARCH.glob("*-research.md"))
    assert not tuple(DOCS.glob("*-research.md"))


def _non_fenced_lines(source: str) -> list[str]:
    """Exclude Markdown fenced code examples from link and heading checks."""
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in source.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            delimiter = match.group(1)
            if fence is None:
                fence = (delimiter[0], len(delimiter))
            elif delimiter[0] == fence[0] and len(delimiter) >= fence[1]:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return lines


def _anchors(source: str) -> set[str]:
    """Generate local heading anchors, including GitHub-style duplicate IDs."""
    found: set[str] = set()
    duplicates: dict[str, int] = {}
    for line in _non_fenced_lines(source):
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*#*\s*$", line)
        if heading:
            title = html.unescape(re.sub(r"<[^>]*>", "", heading.group(1)))
            title = re.sub(r"[`*~]", "", title)
            slug = re.sub(r"[^\w\- ]", "", title.casefold().strip()).replace(" ", "-")
            if slug:
                index = duplicates.get(slug, 0)
                duplicates[slug] = index + 1
                found.add(slug if index == 0 else f"{slug}-{index}")
        for match in re.finditer(r"<a\s+[^>]*?(?:id|name)=['\"]([^'\"]+)['\"]", line):
            found.add(match.group(1))
    return found


def _markdown_link_problems(document: Path, root: Path) -> list[str]:
    """Report broken local Markdown links without following external targets."""
    problems: list[str] = []
    source = document.read_text(encoding="utf-8")
    root_real = root.resolve()
    targets: list[str] = []
    for line in _non_fenced_lines(source):
        targets.extend(re.findall(r"!?\[[^\]\n]*\]\(([^)]+)\)", line))
        for match in re.finditer(r"^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", line):
            targets.append(match.group(1) or match.group(2))
    for raw in targets:
        reference = raw.strip().split(' "', 1)[0].strip("<>")
        if not reference or reference.startswith("//"):
            continue
        link = urlsplit(reference)
        if link.scheme or link.netloc or reference.startswith("/"):
            continue
        target = (document.parent / unquote(link.path)).resolve() if link.path else document.resolve()
        if not target.is_relative_to(root_real):
            problems.append(f"{raw}: outside documentation root")
        elif not target.exists():
            problems.append(f"{raw}: missing target")
        elif link.fragment:
            markdown = target / "README.md" if target.is_dir() else target
            if markdown.suffix.casefold() != ".md":
                problems.append(f"{raw}: anchor target is not Markdown")
            elif not markdown.is_file() or unquote(link.fragment).casefold() not in _anchors(
                markdown.read_text(encoding="utf-8")
            ):
                problems.append(f"{raw}: missing anchor")
    return problems


def test_link_validator_detects_missing_document_and_heading(tmp_path: Path) -> None:
    page = tmp_path / "index.md"
    target = tmp_path / "target.md"
    target.write_text("# Existing Heading\n", encoding="utf-8")
    page.write_text(
        "[valid](target.md#existing-heading) [missing](missing.md) [broken fragment](target.md#not-present)\n",
        encoding="utf-8",
    )
    problems = _markdown_link_problems(page, tmp_path)
    assert len(problems) == 2
    assert any("missing.md" in problem for problem in problems)
    assert any("#not-present" in problem for problem in problems)


def test_link_validator_supports_duplicate_and_unicode_anchors(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    page = tmp_path / "index.md"
    target.write_text("# 중복 제목\n# 중복 제목\n# API `get_task`\n", encoding="utf-8")
    page.write_text(
        "[one](target.md#중복-제목) [two](target.md#중복-제목-1) [three](target.md#api-get_task)\n",
        encoding="utf-8",
    )
    assert _markdown_link_problems(page, tmp_path) == []


def test_link_validator_ignores_examples_in_fenced_blocks(tmp_path: Path) -> None:
    page = tmp_path / "index.md"
    page.write_text(
        "```markdown\n[example](not-real.md)\n```\n[external](https://example.com/a#b)\n",
        encoding="utf-8",
    )
    assert _markdown_link_problems(page, tmp_path) == []


def test_documentation_links_cover_all_current_docs() -> None:
    pages = sorted(
        {*DOCS.rglob("*.md"), *ROOT.glob("*.md"), *ROOT.glob("infra/**/*.md"), ROOT / ".changes" / "README.md"}
    )
    assert (DOCS / "adr" / "0012-managed-worktree-finalization.md") in pages
    assert (DOCS / "tools" / "git" / "remote-pr.md") in pages
    failures = [
        f"{page.relative_to(ROOT)}: {problem}" for page in pages for problem in _markdown_link_problems(page, ROOT)
    ]
    assert not failures, "\n".join(failures)


def _adr_index_problems(index: Path, adr_directory: Path) -> list[str]:
    """Ensure each numbered ADR remains discoverable from the decision index."""
    references = re.findall(
        r"\]\((adr/[0-9]{4}-[^)\s]+\.md)\)",
        index.read_text(encoding="utf-8"),
    )
    present = {
        path.relative_to(index.parent).as_posix()
        for path in adr_directory.glob("[0-9][0-9][0-9][0-9]-*.md")
        if path.is_file()
    }
    linked = set(references)
    problems = [f"{path}: missing from index" for path in sorted(present - linked)]
    problems.extend(f"{path}: not present in adr/" for path in sorted(linked - present))
    problems.extend(f"{path}: duplicate in index" for path in sorted(linked) if references.count(path) > 1)
    return problems


def test_adr_validator_detects_unindexed_decision(tmp_path: Path) -> None:
    adr = tmp_path / "adr"
    adr.mkdir()
    (adr / "0011-example.md").write_text("# Decision\n", encoding="utf-8")
    index = tmp_path / "README.md"
    index.write_text("# Index\n", encoding="utf-8")
    assert _adr_index_problems(index, adr) == ["adr/0011-example.md: missing from index"]


def test_document_map_indexes_all_numbered_adrs() -> None:
    assert _adr_index_problems(DOCS / "README.md", DOCS / "adr") == []


def test_readme_tool_table_matches_explicit_registry() -> None:
    from local_dev_mcp.mcp.server import TOOLS

    section = (ROOT / "README.md").read_text(encoding="utf-8").split("## 도구 사용\n", 1)[1].split("\n## ", 1)[0]
    documented = re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", section, flags=re.MULTILINE)
    registered = [definition.name for definition in TOOLS]
    assert len(documented) == len(set(documented)), "README tool table has duplicate names"
    assert set(documented) == set(registered)
