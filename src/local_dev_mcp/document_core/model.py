"""Source-independent document identity, revision, location and content contracts."""

from dataclasses import dataclass


def require_label(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} must be nonempty and have no boundary whitespace or control characters.")


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    """Stable logical identity; location and content are not part of its key."""

    namespace: str
    value: str

    def __post_init__(self) -> None:
        require_label(self.namespace, "namespace")
        require_label(self.value, "document ID")


@dataclass(frozen=True, slots=True)
class DocumentRevision:
    """Opaque identifier for one version, supplied by a source adapter."""

    value: str

    def __post_init__(self) -> None:
        require_label(self.value, "revision")


@dataclass(frozen=True, slots=True)
class DocumentLocation:
    """Current source and locator, not an identity or access permission."""

    source: str
    locator: str

    def __post_init__(self) -> None:
        require_label(self.source, "source")
        require_label(self.locator, "locator")


def require_type(value: object, expected: type[object], field: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{field} must be a {expected.__name__}.")


@dataclass(frozen=True, slots=True)
class Document:
    """An observed version of a logical document at one source location."""

    identity: DocumentIdentity
    revision: DocumentRevision
    location: DocumentLocation
    content: str

    def __post_init__(self) -> None:
        require_type(self.identity, DocumentIdentity, "identity")
        require_type(self.revision, DocumentRevision, "revision")
        require_type(self.location, DocumentLocation, "location")
        require_type(self.content, str, "content")
