"""Minimal Markdown-to-Document adapter; document IDs come from the caller."""

import hashlib

from ..model import Document, DocumentIdentity, DocumentLocation, DocumentRevision


def _require_markdown(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("markdown must be text.")
    return value


def from_markdown(markdown: str, *, identity: DocumentIdentity, location: DocumentLocation) -> Document:
    """Preserve raw text and derive a deterministic version independent of its path.

    This adapter receives already-read UTF-8 text. It neither reads files nor
    invents an identity from a filename, frontmatter or Markdown heading.
    """
    content = _require_markdown(markdown)
    revision = DocumentRevision("sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest())
    return Document(identity=identity, revision=revision, location=location, content=content)
