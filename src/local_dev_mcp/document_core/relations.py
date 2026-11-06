"""Generic, directed relationships between logical documents.

A graph is an immutable view of one supplied document snapshot. Relation
semantics and errors for missing endpoints belong to later policy validation.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .model import Document, DocumentIdentity, DocumentRevision, require_label, require_type


@dataclass(frozen=True, slots=True)
class DocumentReference:
    """Logical document identity, optionally pinned to an exact revision."""

    identity: DocumentIdentity
    revision: DocumentRevision | None = None

    def __post_init__(self) -> None:
        require_type(self.identity, DocumentIdentity, "identity")
        if self.revision is not None:
            require_type(self.revision, DocumentRevision, "revision")


@dataclass(frozen=True, slots=True)
class DocumentRelation:
    """Directed relationship with a caller-defined kind; no policy semantics."""

    source: DocumentIdentity
    target: DocumentReference
    kind: str

    def __post_init__(self) -> None:
        require_type(self.source, DocumentIdentity, "source")
        require_type(self.target, DocumentReference, "target")
        require_label(self.kind, "relation kind")


@dataclass(frozen=True, slots=True, init=False)
class DocumentGraph:
    """An immutable document snapshot and its directed relationship records.

    Duplicate logical identities are ambiguous in a one-version snapshot and
    rejected. Missing relation endpoints, cycles and repeated edges are kept
    for later project-specific validation instead of silently discarded.
    """

    documents: tuple[Document, ...]
    relations: tuple[DocumentRelation, ...]

    def __init__(self, documents: Iterable[Document], relations: Iterable[DocumentRelation]) -> None:
        observed = tuple(documents)
        edges = tuple(relations)
        identities: set[DocumentIdentity] = set()

        for document in observed:
            require_type(document, Document, "document")
            if document.identity in identities:
                raise ValueError("Duplicate document identity in graph snapshot.")
            identities.add(document.identity)
        for relation in edges:
            require_type(relation, DocumentRelation, "relation")

        object.__setattr__(self, "documents", observed)
        object.__setattr__(self, "relations", edges)

    def resolve(self, reference: DocumentReference) -> Document | None:
        """Resolve within this snapshot; an unavailable pin is unresolved."""
        require_type(reference, DocumentReference, "reference")
        for document in self.documents:
            if document.identity == reference.identity:
                return document if reference.revision is None or document.revision == reference.revision else None
        return None

    def outgoing(self, identity: DocumentIdentity) -> tuple[DocumentRelation, ...]:
        """Return only explicit edges originating at the logical identity."""
        require_type(identity, DocumentIdentity, "identity")
        return tuple(edge for edge in self.relations if edge.source == identity)

    def incoming(self, identity: DocumentIdentity) -> tuple[DocumentRelation, ...]:
        """Return only explicit edges targeting the logical identity."""
        require_type(identity, DocumentIdentity, "identity")
        return tuple(edge for edge in self.relations if edge.target.identity == identity)
