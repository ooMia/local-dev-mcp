"""Strict v1 Guide registry: explicit IDs only; no relation approval inference."""

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from ...document_core import DocumentIdentity

GUIDE_PATH = "docs/migration-guide.md"
REPOSITORY_ID = "local-dev-mcp"
MANAGED_PATHS = frozenset(
    {
        "AGENTS.md",
        "docs/README.md",
        "docs/adr/0002-document-authority.md",
        "docs/adr/0004-document-core-identity-source-boundary.md",
        "docs/contracts/document-core.md",
    }
)
_JSON_BLOCK = re.compile(r"(?ms)^```json[ \t]*\r?\n(.*?)^```[ \t]*$")
MAX_GUIDE_CHARS = 128 * 1024


@dataclass(frozen=True, slots=True)
class RegisteredDocument:
    identity: DocumentIdentity
    path: str


@dataclass(frozen=True, slots=True)
class GuideRegistry:
    documents: tuple[RegisteredDocument, ...]
    relation_approval: str = "pending"


def parse_guide(source: str) -> GuideRegistry:
    """Interpret one explicit JSON registry without accepting inferred authority."""
    if len(source) > MAX_GUIDE_CHARS:
        raise ValueError("Guide exceeds the registry size limit.")
    blocks = _JSON_BLOCK.findall(source)
    if len(blocks) != 1:
        raise ValueError("Guide requires exactly one fenced JSON registry.")
    try:
        raw: object = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise ValueError("Guide registry is not valid JSON.") from error
    if not isinstance(raw, dict):
        raise ValueError("Guide registry must be a JSON object.")
    registry = cast(dict[str, object], raw)
    if set(registry) != {"schemaVersion", "documents", "relations", "relationApproval"}:
        raise ValueError("Guide registry contains missing or unexpected fields.")
    if type(registry["schemaVersion"]) is not int or registry["schemaVersion"] != 1:
        raise ValueError("Unsupported Guide registry schema version.")
    entries = registry["documents"]
    if not isinstance(entries, list):
        raise ValueError("Guide documents must be a JSON list.")
    entries = cast(list[object], entries)
    if len(entries) != len(MANAGED_PATHS):
        raise ValueError("Guide must register the five managed documents.")
    if registry["relations"] != []:
        raise ValueError("Unapproved relationships cannot be registered.")
    approval = registry["relationApproval"]
    if not isinstance(approval, dict) or approval != {"status": "pending", "record": None}:
        raise ValueError("Initial Guide does not contain an approved relation history.")

    documents: list[RegisteredDocument] = []
    paths: set[str] = set()
    identities: set[DocumentIdentity] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid registered document fields.")
        fields = cast(dict[str, object], entry)
        if set(fields) != {"namespace", "id", "path"}:
            raise ValueError("Invalid registered document fields.")
        namespace, identifier, path = fields["namespace"], fields["id"], fields["path"]
        if not isinstance(namespace, str) or not isinstance(identifier, str) or not isinstance(path, str):
            raise ValueError("Document identity and location must be strings.")
        identity = DocumentIdentity(namespace, identifier)
        normalized = PurePosixPath(path)
        if path not in MANAGED_PATHS or normalized.as_posix() != path:
            raise ValueError("Registry references an unregistered document path.")
        if path in paths or identity in identities:
            raise ValueError("Duplicate document path or identity in registry.")
        paths.add(path)
        identities.add(identity)
        documents.append(RegisteredDocument(identity, path))
    if paths != set(MANAGED_PATHS):
        raise ValueError("Guide registration does not match the five managed documents.")
    return GuideRegistry(tuple(sorted(documents, key=lambda item: item.path)))
