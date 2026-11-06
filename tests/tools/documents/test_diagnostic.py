"""Pure diagnostic projection retains uncertainty instead of inventing success."""

import hashlib

import pytest

from local_dev_mcp.document_core import DocumentIdentity
from local_dev_mcp.tools.documents.diagnostic import diagnose_captured
from local_dev_mcp.tools.documents.registry import MANAGED_PATHS, GuideRegistry, RegisteredDocument

pytestmark = pytest.mark.unit


def sample() -> tuple[GuideRegistry, dict[str, str]]:
    paths = sorted(MANAGED_PATHS)
    registry = GuideRegistry(
        tuple(RegisteredDocument(DocumentIdentity("local-dev-mcp", f"d-{i}"), path) for i, path in enumerate(paths))
    )
    return registry, {path: f"# {path}\n" for path in paths}


def test_pending_approval_blocks_all_relation_validation() -> None:
    registry, sources = sample()
    result = diagnose_captured(registry, sources, repository_id="local-dev-mcp")
    assert result["completion"] == "Blocked"
    assert result["summary"]["capturedDocuments"] == 5
    assert result["summary"]["evaluatedDocuments"] == 0
    assert all(item["outcome"] is None for item in result["documents"])
    assert result["findings"][0]["code"] == "RELATIONS_APPROVAL_PENDING"
    assert result["findings"][0]["evidence"][0]["location"] == "docs/migration-guide.md"
    assert any(item["scope"] == "Relation" and item["status"] == "Blocked" for item in result["coverage"])
    assert "# AGENTS.md" not in str(result)


def test_fingerprint_is_deterministic_and_repository_independent() -> None:
    registry, sources = sample()
    first = diagnose_captured(registry, sources, repository_id="local-dev-mcp")
    second = diagnose_captured(registry, dict(reversed(list(sources.items()))), repository_id="another-repository")
    assert first["snapshot"]["fingerprint"] == second["snapshot"]["fingerprint"]
    assert len(first["snapshot"]["fingerprint"]) == 64
    sources["AGENTS.md"] = "# modified\n"
    third = diagnose_captured(registry, sources, repository_id="local-dev-mcp")
    assert third["snapshot"]["fingerprint"] != first["snapshot"]["fingerprint"]
    assert third["documents"][0]["revision"] == "sha256:" + hashlib.sha256(b"# modified\n").hexdigest()
    assert first["findings"][0]["key"] == third["findings"][0]["key"]
