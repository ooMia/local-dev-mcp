"""Stage 2 acceptance: document relations stay source-independent and directional."""

from dataclasses import FrozenInstanceError

import pytest

from local_dev_mcp.document_core import (
    Document,
    DocumentGraph,
    DocumentIdentity,
    DocumentLocation,
    DocumentReference,
    DocumentRelation,
)
from local_dev_mcp.document_core.adapters.markdown import from_markdown

pytestmark = pytest.mark.unit


def _document(namespace: str, name: str, content: str = "# Hi\n") -> Document:
    return from_markdown(
        content,
        identity=DocumentIdentity(namespace, name),
        location=DocumentLocation("repo", f"{namespace}/{name}.md"),
    )


def test_logical_reference_resolves_against_the_graph_snapshot() -> None:
    first = _document("wiki", "entry")
    newer = _document("wiki", "entry", "# Revised\n")
    identity = first.identity

    assert DocumentGraph((first,), ()).resolve(DocumentReference(identity)) == first
    assert DocumentGraph((newer,), ()).resolve(DocumentReference(identity)) == newer


def test_pinned_reference_requires_the_same_revision() -> None:
    before = _document("wiki", "entry")
    after = _document("wiki", "entry", "# Revised\n")
    reference = DocumentReference(before.identity, before.revision)

    assert DocumentGraph((before,), ()).resolve(reference) == before
    assert DocumentGraph((after,), ()).resolve(reference) is None
    assert DocumentGraph((after,), ()).resolve(DocumentReference(after.identity)) == after


def test_directed_relation_is_not_implicitly_reciprocal_and_preserves_opaque_kind() -> None:
    first = _document("wiki", "one")
    second = _document("wiki", "two")
    relation = DocumentRelation(first.identity, DocumentReference(second.identity), "links-to")
    graph = DocumentGraph((first, second), (relation,))

    assert graph.outgoing(first.identity) == (relation,)
    assert graph.incoming(second.identity) == (relation,)
    assert graph.outgoing(second.identity) == ()
    assert graph.incoming(first.identity) == ()
    assert graph.relations == (relation,)
    assert graph.documents == (first, second)


def test_cross_namespace_target_lookup_does_not_confuse_same_local_id() -> None:
    first = _document("manual", "same")
    second = _document("wiki", "same")
    reference = DocumentReference(second.identity)
    graph = DocumentGraph((first, second), (DocumentRelation(first.identity, reference, "references"),))

    assert graph.resolve(reference) == second
    assert graph.resolve(DocumentReference(first.identity)) == first


def test_unresolved_relations_are_retained_without_project_policy() -> None:
    original = _document("wiki", "known")
    missing = DocumentIdentity("wiki", "unknown")
    edge = DocumentRelation(original.identity, DocumentReference(missing), "references")
    graph = DocumentGraph((original,), (edge,))

    assert graph.outgoing(original.identity) == (edge,)
    assert graph.incoming(missing) == (edge,)
    assert graph.resolve(edge.target) is None


def test_cycles_and_duplicate_edges_are_not_core_policy_violations() -> None:
    a = _document("wiki", "a")
    b = _document("wiki", "b")
    ab = DocumentRelation(a.identity, DocumentReference(b.identity), "custom")
    ba = DocumentRelation(b.identity, DocumentReference(a.identity), "custom")
    graph = DocumentGraph((a, b), (ab, ba, ab))

    assert graph.outgoing(a.identity) == (ab, ab)
    assert graph.incoming(a.identity) == (ba,)
    assert len(graph.relations) == 3


def test_duplicate_logical_identity_is_rejected_even_across_revisions() -> None:
    first = _document("wiki", "entry")
    revised = _document("wiki", "entry", "# Revised\n")

    with pytest.raises(ValueError, match="Duplicate document identity"):
        DocumentGraph((first, revised), ())


def test_relation_and_graph_values_are_immutable() -> None:
    first = _document("wiki", "a")
    relation = DocumentRelation(first.identity, DocumentReference(first.identity), "self")
    graph = DocumentGraph((first,), (relation,))

    with pytest.raises(FrozenInstanceError):
        relation.kind = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        graph.relations = ()  # type: ignore[misc]


def test_relation_types_do_not_encode_adr_or_policy_semantics() -> None:
    a = _document("wiki", "a")
    b = _document("wiki", "b")
    edges = (
        DocumentRelation(a.identity, DocumentReference(b.identity), "supersedes"),
        DocumentRelation(a.identity, DocumentReference(b.identity), "governs"),
    )
    graph = DocumentGraph((a, b), edges)
    assert graph.outgoing(a.identity) == edges


@pytest.mark.parametrize("kind", ["", " needs-trim ", "line\nbreak"])
def test_relation_kind_requires_a_valid_label(kind: str) -> None:
    with pytest.raises(ValueError):
        DocumentRelation(
            DocumentIdentity("wiki", "one"),
            DocumentReference(DocumentIdentity("wiki", "two")),
            kind,
        )


def test_reference_rejects_non_revision_and_relation_rejects_invalid_target() -> None:
    with pytest.raises(TypeError):
        DocumentReference(DocumentIdentity("wiki", "one"), "wrong")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DocumentRelation(
            DocumentIdentity("wiki", "one"),
            DocumentIdentity("wiki", "two"),  # type: ignore[arg-type]
            "references",
        )
