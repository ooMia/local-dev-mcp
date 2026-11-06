"""Stage 1 acceptance: explicit identity, location independence and revision changes."""

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from local_dev_mcp.document_core import Document, DocumentIdentity, DocumentLocation, DocumentRevision
from local_dev_mcp.document_core.adapters.markdown import from_markdown

pytestmark = pytest.mark.unit


def test_markdown_document_preserves_explicit_identity_and_raw_content() -> None:
    markdown = "---\nid: a-different-id\n---\n# A heading\n"
    identity = DocumentIdentity(namespace="blog", value="article-17")
    location = DocumentLocation(source="repo-docs", locator="articles/start.md")

    document = from_markdown(markdown, identity=identity, location=location)

    assert document.identity == identity
    assert document.location == location
    assert document.content == markdown
    assert document.revision.value == "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert document.identity.value != "a-different-id"


def test_move_changes_location_but_not_logical_id_or_content_revision() -> None:
    original = from_markdown(
        "# Hello\n",
        identity=DocumentIdentity("blog", "hello"),
        location=DocumentLocation("repo-docs", "drafts/hello.md"),
    )
    moved = from_markdown(
        "# Hello\n",
        identity=original.identity,
        location=DocumentLocation("repo-docs", "published/hello.md"),
    )

    assert original.identity == moved.identity
    assert original.revision == moved.revision
    assert original.location != moved.location


def test_content_edit_changes_revision_without_changing_identity() -> None:
    identity = DocumentIdentity("blog", "hello")
    location = DocumentLocation("repo-docs", "hello.md")
    before = from_markdown("# Hello\n", identity=identity, location=location)
    after = from_markdown("# Hello!\n", identity=identity, location=location)

    assert before.identity == after.identity
    assert before.revision != after.revision
    assert before.location == after.location


def test_raw_text_changes_are_not_normalized_and_content_hash_is_deterministic() -> None:
    identity = DocumentIdentity("blog", "hello")
    location = DocumentLocation("repo-docs", "hello.md")
    lf = from_markdown("Hello\n", identity=identity, location=location)
    crlf = from_markdown("Hello\r\n", identity=identity, location=location)

    assert lf.revision != crlf.revision
    assert lf == from_markdown("Hello\n", identity=identity, location=location)


def test_namespace_disambiguates_identical_local_ids() -> None:
    first = DocumentIdentity("blog", "readme")
    second = DocumentIdentity("manual", "readme")
    assert first != second
    assert {first, second} == {first, second}


def test_revision_is_an_opaque_core_value_not_an_assumed_git_sha() -> None:
    observed = Document(
        DocumentIdentity("wiki", "one"),
        DocumentRevision("external:v9"),
        DocumentLocation("system-A", "record/17"),
        "text",
    )
    assert observed.revision.value == "external:v9"


@pytest.mark.parametrize(
    ("factory", "args"),
    [
        (DocumentIdentity, ("", "id")),
        (DocumentIdentity, ("space ", "id")),
        (DocumentIdentity, ("blog", "  ")),
        (DocumentIdentity, ("blog", "contains\nnewline")),
        (DocumentRevision, ("",)),
        (DocumentLocation, ("repo", "")),
        (DocumentLocation, ("", "doc.md")),
    ],
)
def test_identity_revision_and_location_reject_invalid_labels(
    factory: type[DocumentIdentity] | type[DocumentRevision] | type[DocumentLocation], args: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        factory(*args)


def test_document_is_immutable() -> None:
    doc = from_markdown(
        "# Title",
        identity=DocumentIdentity("blog", "post"),
        location=DocumentLocation("repo", "post.md"),
    )
    with pytest.raises(FrozenInstanceError):
        doc.content = "changed"  # type: ignore[misc]


def test_adapter_rejects_non_text_input() -> None:
    with pytest.raises(TypeError):
        from_markdown(
            b"Markdown",  # type: ignore[arg-type]
            identity=DocumentIdentity("blog", "post"),
            location=DocumentLocation("repo", "post.md"),
        )
