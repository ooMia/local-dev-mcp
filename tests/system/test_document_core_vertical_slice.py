"""Stage 4: real Markdown inputs through the existing source-independent Core.

The test owns file reads, logical IDs, declared relations and project rules.
No source parser, implicit authority or MCP adapter is introduced in production.
"""

from collections.abc import Iterable
from pathlib import Path

import pytest

from local_dev_mcp.document_core import (
    Document,
    DocumentGraph,
    DocumentIdentity,
    DocumentLocation,
    DocumentReference,
    DocumentRelation,
    ValidationEvidence,
    ValidationOutcome,
    ValidationResult,
    validate,
)
from local_dev_mcp.document_core.adapters.markdown import from_markdown

pytestmark = pytest.mark.integration


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ingest(root: Path, relative: str, identity: DocumentIdentity) -> Document:
    # Filesystem access and stable-ID assignment belong to this source caller.
    return from_markdown(
        (root / relative).read_text(encoding="utf-8"),
        identity=identity,
        location=DocumentLocation(source="test-repository", locator=relative),
    )


def _authority_policy(graph: DocumentGraph) -> Iterable[ValidationResult]:
    """Example project policy: an ADR may govern an existing Contract revision."""
    for edge in graph.relations:
        if edge.kind != "governs":
            continue
        source = graph.resolve(DocumentReference(edge.source))
        actual = graph.resolve(DocumentReference(edge.target.identity))
        pinned = graph.resolve(edge.target)
        is_valid = (
            source is not None
            and source.identity.namespace == "adr"
            and edge.target.identity.namespace == "contract"
            and pinned is not None
        )
        evidence = (
            ValidationEvidence(
                kind="snapshot",
                detail=f"source={edge.source.namespace}/{edge.source.value}, "
                f"target={edge.target.identity.namespace}/{edge.target.identity.value}, "
                f"observed={actual.revision.value if actual else 'missing'}",
            ),
        )
        yield ValidationResult(
            subject=edge.target,
            rule="sample.adr-governs-contract",
            code="valid-governance" if is_valid else "governance-unresolved",
            outcome=ValidationOutcome.VERIFIED if is_valid else ValidationOutcome.INCONSISTENT,
            evidence=evidence,
        )


def _reference_policy(
    graph: DocumentGraph, *, missing: ValidationOutcome = ValidationOutcome.UNVERIFIED
) -> Iterable[ValidationResult]:
    """Example article policy: unknown references are not asserted broken."""
    for edge in graph.relations:
        if edge.kind != "cites":
            continue
        resolved = graph.resolve(edge.target)
        yield ValidationResult(
            subject=edge.target,
            rule="sample.article-references",
            code="resolved" if resolved is not None else "reference-unresolved",
            outcome=ValidationOutcome.VERIFIED if resolved is not None else missing,
            evidence=(ValidationEvidence("snapshot", "Target looked up in supplied document snapshot"),),
        )


def test_adr_contract_and_article_markdown_complete_validation_pipeline(tmp_path: Path) -> None:
    # Markdown frontmatter IDs do not override caller-owned stable IDs.
    _write(tmp_path, "adr/decision.md", "---\nid: unrelated\n---\n# Decision\n")
    _write(tmp_path, "contracts/interface.md", "# Interface\n")
    _write(tmp_path, "articles/overview.md", "# Overview\n")
    _write(tmp_path, "references/book.md", "# Reference\n")

    adr = _ingest(tmp_path, "adr/decision.md", DocumentIdentity("adr", "adr-004"))
    contract = _ingest(tmp_path, "contracts/interface.md", DocumentIdentity("contract", "interface-v1"))
    article = _ingest(tmp_path, "articles/overview.md", DocumentIdentity("article", "overview"))
    reference = _ingest(tmp_path, "references/book.md", DocumentIdentity("reference", "book"))
    governing = DocumentRelation(adr.identity, DocumentReference(contract.identity, contract.revision), "governs")
    cited = DocumentRelation(article.identity, DocumentReference(reference.identity), "cites")

    graph = DocumentGraph((adr, contract, article, reference), (governing, cited))
    results = validate(graph, (_authority_policy, _reference_policy))

    assert adr.identity == DocumentIdentity("adr", "adr-004")
    assert adr.identity.value != "unrelated"
    assert [result.rule for result in results] == ["sample.adr-governs-contract", "sample.article-references"]
    assert [result.code for result in results] == ["valid-governance", "resolved"]
    assert [result.outcome for result in results] == [ValidationOutcome.VERIFIED, ValidationOutcome.VERIFIED]
    assert [result.subject for result in results] == [governing.target, cited.target]
    assert all(result.evidence for result in results)
    assert contract.revision.value in results[0].evidence[0].detail


def test_edited_contract_invalidates_pinned_governance_reference(tmp_path: Path) -> None:
    _write(tmp_path, "adr/a.md", "# Design choice\n")
    _write(tmp_path, "contracts/c.md", "# Contract version one\n")
    adr = _ingest(tmp_path, "adr/a.md", DocumentIdentity("adr", "decision"))
    original = _ingest(tmp_path, "contracts/c.md", DocumentIdentity("contract", "contract"))
    governing = DocumentRelation(adr.identity, DocumentReference(original.identity, original.revision), "governs")
    before_results = validate(DocumentGraph((adr, original), (governing,)), (_authority_policy,))
    assert before_results[0].outcome is ValidationOutcome.VERIFIED

    _write(tmp_path, "contracts/c.md", "# Contract version two\n")
    revised = _ingest(tmp_path, "contracts/c.md", original.identity)
    graph = DocumentGraph((adr, revised), (governing,))
    (result,) = validate(graph, (_authority_policy,))

    assert revised.identity == original.identity
    assert revised.revision != original.revision
    assert result.outcome is ValidationOutcome.INCONSISTENT
    assert result.subject.revision == original.revision
    assert revised.revision.value in result.evidence[0].detail


def test_same_missing_article_reference_supports_different_project_policies(tmp_path: Path) -> None:
    _write(tmp_path, "articles/story.md", "# Story\n")
    article = _ingest(tmp_path, "articles/story.md", DocumentIdentity("article", "story"))
    link = DocumentRelation(
        article.identity, DocumentReference(DocumentIdentity("reference", "external-source")), "cites"
    )
    graph = DocumentGraph((article,), (link,))

    open_policy = validate(graph, (_reference_policy,))

    def closed_policy(snapshot: DocumentGraph) -> Iterable[ValidationResult]:
        return _reference_policy(snapshot, missing=ValidationOutcome.INCONSISTENT)

    closed_results = validate(graph, (closed_policy,))
    assert len(open_policy) == len(closed_results) == 1
    assert open_policy[0].outcome is ValidationOutcome.UNVERIFIED
    assert closed_results[0].outcome is ValidationOutcome.INCONSISTENT
    assert open_policy[0].subject == closed_results[0].subject == link.target
    assert graph.relations == (link,)
