"""Stage 3: project-owned validation rules and structured evidence."""

from collections.abc import Iterable

import pytest

from local_dev_mcp.document_core import (
    Document,
    DocumentGraph,
    DocumentIdentity,
    DocumentLocation,
    DocumentReference,
    DocumentRelation,
    DocumentRevision,
    ValidationEvidence,
    ValidationOutcome,
    ValidationResult,
    validate,
)
from local_dev_mcp.document_core.adapters.markdown import from_markdown

pytestmark = pytest.mark.unit


def _document(name: str, text: str = "# Text\n") -> Document:
    return from_markdown(
        text,
        identity=DocumentIdentity("docs", name),
        location=DocumentLocation("repo", f"{name}.md"),
    )


def _result(
    reference: DocumentReference,
    outcome: ValidationOutcome,
    *,
    rule: str = "examples.resolve-links",
    code: str = "link-target",
) -> ValidationResult:
    return ValidationResult(
        subject=reference,
        rule=rule,
        code=code,
        outcome=outcome,
        evidence=(ValidationEvidence("observed", "Graph snapshot reference resolution"),),
    )


def _closed_world(graph: DocumentGraph) -> Iterable[ValidationResult]:
    for relation in graph.relations:
        resolved = graph.resolve(relation.target)
        yield _result(
            relation.target,
            ValidationOutcome.VERIFIED if resolved is not None else ValidationOutcome.INCONSISTENT,
        )


def _open_world(graph: DocumentGraph) -> Iterable[ValidationResult]:
    for relation in graph.relations:
        resolved = graph.resolve(relation.target)
        yield _result(
            relation.target,
            ValidationOutcome.VERIFIED if resolved is not None else ValidationOutcome.UNVERIFIED,
            rule="examples.external-links",
        )


def test_project_rules_can_disagree_on_the_same_missing_reference() -> None:
    document = _document("one")
    target = DocumentReference(DocumentIdentity("docs", "unavailable"))
    graph = DocumentGraph((document,), (DocumentRelation(document.identity, target, "points-to"),))

    closed = validate(graph, (_closed_world,))
    open_results = validate(graph, (_open_world,))

    assert len(closed) == len(open_results) == 1
    assert closed[0].outcome is ValidationOutcome.INCONSISTENT
    assert open_results[0].outcome is ValidationOutcome.UNVERIFIED
    assert closed[0].subject == open_results[0].subject == target
    assert closed[0].evidence[0].kind == "observed"


def test_rule_verifies_resolved_reference_and_preserves_revision_pin() -> None:
    source = _document("source")
    target = _document("target")
    reference = DocumentReference(target.identity, target.revision)
    graph = DocumentGraph((source, target), (DocumentRelation(source.identity, reference, "links-to"),))

    (result,) = validate(graph, (_closed_world,))
    assert result.outcome is ValidationOutcome.VERIFIED
    assert result.subject.revision == target.revision
    assert result.rule == "examples.resolve-links"
    assert result.code == "link-target"


def test_stale_revision_remains_explicit_in_evidence_subject() -> None:
    source = _document("source")
    previous_target = _document("target")
    revised_target = _document("target", "# Revised\n")
    reference = DocumentReference(revised_target.identity, previous_target.revision)
    graph = DocumentGraph((source, revised_target), (DocumentRelation(source.identity, reference, "links-to"),))

    (result,) = validate(graph, (_closed_world,))
    assert result.outcome is ValidationOutcome.INCONSISTENT
    assert result.subject.revision == previous_target.revision


def test_opaque_relation_kinds_are_not_validated_by_core() -> None:
    document = _document("one")
    relation = DocumentRelation(document.identity, DocumentReference(document.identity), "governs")
    graph = DocumentGraph((document,), (relation,))

    assert validate(graph, ()) == ()
    assert validate(graph, (_closed_world,))[0].outcome is ValidationOutcome.VERIFIED


def test_rule_order_and_result_order_are_preserved() -> None:
    document = _document("one")
    graph = DocumentGraph((document,), ())
    reference = DocumentReference(document.identity)
    events: list[str] = []

    def first(_: DocumentGraph) -> Iterable[ValidationResult]:
        events.append("first")
        yield _result(reference, ValidationOutcome.VERIFIED, rule="one", code="first")
        yield _result(reference, ValidationOutcome.UNVERIFIED, rule="one", code="second")

    def second(_: DocumentGraph) -> Iterable[ValidationResult]:
        events.append("second")
        return (_result(reference, ValidationOutcome.INCONSISTENT, rule="two", code="third"),)

    results = validate(graph, (first, second))
    assert events == ["first", "second"]
    assert [r.code for r in results] == ["first", "second", "third"]
    assert isinstance(results, tuple)


def test_failures_in_rule_execution_are_not_reported_as_verified() -> None:
    document = _document("one")
    graph = DocumentGraph((document,), ())

    def failing(_: DocumentGraph) -> Iterable[ValidationResult]:
        raise RuntimeError("Rule unavailable")

    with pytest.raises(RuntimeError, match="Rule unavailable"):
        validate(graph, (failing,))


def test_non_result_rule_output_is_rejected() -> None:
    document = _document("one")
    graph = DocumentGraph((document,), ())

    def invalid(_: DocumentGraph) -> Iterable[ValidationResult]:
        yield "incorrect"  # type: ignore[misc]

    with pytest.raises(TypeError, match="ValidationResult"):
        validate(graph, (invalid,))


def test_outcome_is_distinct_from_severity_or_blocking_policy() -> None:
    reference = DocumentReference(DocumentIdentity("docs", "unknown"))
    result = _result(reference, ValidationOutcome.UNVERIFIED)
    assert result.outcome.value == "Unverified"
    assert not hasattr(result, "severity")
    assert not hasattr(result, "blocking")


@pytest.mark.parametrize("bad_value", ["", " ", "broken\nvalue"])
def test_result_rejects_invalid_rule_or_code(bad_value: str) -> None:
    reference = DocumentReference(DocumentIdentity("docs", "one"))
    with pytest.raises(ValueError):
        ValidationResult(
            subject=reference,
            rule=bad_value,
            code="reference",
            outcome=ValidationOutcome.INCONSISTENT,
        )
    with pytest.raises(ValueError):
        ValidationResult(
            subject=reference,
            rule="docs.links",
            code=bad_value,
            outcome=ValidationOutcome.INCONSISTENT,
        )


def test_result_rejects_invalid_outcome_and_evidence() -> None:
    reference = DocumentReference(DocumentIdentity("docs", "one"))
    with pytest.raises(TypeError):
        ValidationResult(
            subject=reference,
            rule="docs.links",
            code="reference",
            outcome="Verified",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ValidationResult(
            subject=reference,
            rule="docs.links",
            code="reference",
            outcome=ValidationOutcome.VERIFIED,
            evidence=("plain evidence",),  # type: ignore[arg-type]
        )


def test_evidence_and_results_are_immutable_and_reusable() -> None:
    reference = DocumentReference(DocumentIdentity("docs", "one"), DocumentRevision("v1"))
    evidence = ValidationEvidence("snapshot", "Observed reference to v1")
    result = ValidationResult(
        subject=reference,
        rule="docs.links",
        code="pinned-link",
        outcome=ValidationOutcome.VERIFIED,
        evidence=(evidence,),
    )
    assert result.evidence == (evidence,)
    assert hash(result)
    with pytest.raises(AttributeError):
        result.evidence += (evidence,)  # type: ignore[misc]
