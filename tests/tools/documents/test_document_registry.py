"""Unit checks for the unapproved initial Guide registry."""

import json

import pytest

from local_dev_mcp.tools.documents.registry import GUIDE_PATH, MANAGED_PATHS, parse_guide

pytestmark = pytest.mark.unit


def guide(registry: dict[str, object]) -> str:
    return "# Guide\n\n```json\n" + json.dumps(registry) + "\n```\n"


def initial() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "documents": [
            {"namespace": "local-dev-mcp", "id": str(index), "path": path}
            for index, path in enumerate(sorted(MANAGED_PATHS))
        ],
        "relations": [],
        "relationApproval": {"status": "pending", "record": None},
    }


def test_explicit_five_document_registration() -> None:
    result = parse_guide(guide(initial()))
    assert GUIDE_PATH == "docs/migration-guide.md"
    assert len(result.documents) == 5
    assert result.relation_approval == "pending"
    assert {entry.path for entry in result.documents} == MANAGED_PATHS


@pytest.mark.parametrize("mutation", ["relation", "approval", "missing", "duplicate", "path", "version"])
def test_rejects_ambiguous_or_unapproved_registry(mutation: str) -> None:
    data = initial()
    entries = data["documents"]
    assert isinstance(entries, list)
    if mutation == "relation":
        data["relations"] = [{"source": "a", "target": "b", "kind": "governs"}]
    elif mutation == "approval":
        data["relationApproval"] = {"status": "approved", "record": "any"}
    elif mutation == "missing":
        entries.pop()
    elif mutation == "duplicate":
        entries[1] = entries[0]
    elif mutation == "path":
        entries[0] = {"namespace": "local-dev-mcp", "id": "id", "path": "../secret"}
    else:
        data["schemaVersion"] = 2
    with pytest.raises(ValueError):
        parse_guide(guide(data))


def test_rejects_missing_or_multiple_json_blocks() -> None:
    with pytest.raises(ValueError):
        parse_guide("# no registry")
    with pytest.raises(ValueError):
        parse_guide(guide(initial()) * 2)
