"""Experimental, source-independent document value objects."""

from .model import Document, DocumentIdentity, DocumentLocation, DocumentRevision
from .relations import DocumentGraph, DocumentReference, DocumentRelation
from .validation import ValidationEvidence, ValidationOutcome, ValidationResult, ValidationRule, validate

__all__ = [
    "Document",
    "DocumentGraph",
    "DocumentIdentity",
    "DocumentLocation",
    "DocumentReference",
    "DocumentRelation",
    "DocumentRevision",
    "ValidationEvidence",
    "ValidationOutcome",
    "ValidationResult",
    "ValidationRule",
    "validate",
]
