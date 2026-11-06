"""Source-independent validation result contracts and injected project rules.

Only rule invocation and structured result collection live here. Meaning of
individual rules, failure severity and release blocking remain project-owned.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .model import require_label, require_type
from .relations import DocumentGraph, DocumentReference


class ValidationOutcome(StrEnum):
    VERIFIED = "Verified"
    INCONSISTENT = "Inconsistent"
    UNVERIFIED = "Unverified"


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    """Project-supplied evidence; kind identifies the supporting observation."""

    kind: str
    detail: str

    def __post_init__(self) -> None:
        require_label(self.kind, "evidence kind")
        require_type(self.detail, str, "evidence detail")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """One rule's assessment of one logical document/version reference."""

    subject: DocumentReference
    rule: str
    code: str
    outcome: ValidationOutcome
    evidence: tuple[ValidationEvidence, ...] = ()

    def __post_init__(self) -> None:
        require_type(self.subject, DocumentReference, "subject")
        require_label(self.rule, "rule")
        require_label(self.code, "result code")
        require_type(self.outcome, ValidationOutcome, "outcome")
        require_type(self.evidence, tuple, "evidence")
        for entry in self.evidence:
            require_type(entry, ValidationEvidence, "evidence entry")


type ValidationRule = Callable[[DocumentGraph], Iterable[ValidationResult]]


def validate(graph: DocumentGraph, rules: Iterable[ValidationRule]) -> tuple[ValidationResult, ...]:
    """Run supplied rules in order and return their results in emitted order.

    The caller owns rule selection, implementation and interpretation. Rule
    failures propagate rather than being misreported as successful validation.
    """
    require_type(graph, DocumentGraph, "graph")
    results: list[ValidationResult] = []
    for rule in rules:
        for result in rule(graph):
            require_type(result, ValidationResult, "result")
            results.append(result)
    return tuple(results)
