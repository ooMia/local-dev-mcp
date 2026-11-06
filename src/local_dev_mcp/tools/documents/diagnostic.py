"""Read-only diagnostic projection for a single captured Guide registry."""

import hashlib
import json
from typing import Literal, TypedDict

from ...document_core import DocumentGraph, DocumentLocation
from ...document_core.adapters.markdown import from_markdown
from .registry import GUIDE_PATH, GuideRegistry

type Completion = Literal["Completed", "Blocked"]
type CoverageState = Literal["Evaluated", "Blocked", "NotEvaluated"]


class DocumentStatus(TypedDict):
    namespace: str
    id: str
    location: str
    revision: str
    outcome: str | None


class Coverage(TypedDict):
    scope: str
    target: str | None
    check: str
    status: CoverageState
    blockingFindingKey: str | None


class Evidence(TypedDict):
    kind: str
    location: str
    pointer: str


class Finding(TypedDict):
    key: str
    code: str
    severity: str
    scope: str
    category: str
    targets: list[str]
    evidence: list[Evidence]


class Snapshot(TypedDict):
    fingerprint: str
    formatVersion: int


class RuleSet(TypedDict):
    id: str
    version: str


class Summary(TypedDict):
    totalDocuments: int
    capturedDocuments: int
    evaluatedDocuments: int
    statusCounts: dict[str, int]
    approvedRelations: int


class DiagnosticResult(TypedDict):
    schemaVersion: int
    repositoryId: str
    snapshot: Snapshot
    ruleSet: RuleSet
    completion: Completion
    summary: Summary
    documents: list[DocumentStatus]
    coverage: list[Coverage]
    findings: list[Finding]


def diagnose_captured(registry: GuideRegistry, sources: dict[str, str], *, repository_id: str) -> DiagnosticResult:
    """Build a deterministic, explicitly blocked report until relations are approved.

    Missing approval is *not* the same as an approved empty relationship set.
    ValidationOutcome cannot be assigned before the corresponding rule ran.
    """
    documents = tuple(
        from_markdown(
            sources[entry.path],
            identity=entry.identity,
            location=DocumentLocation("repository", entry.path),
        )
        for entry in registry.documents
    )
    # The generic Core is supplied explicit IDs and edges; it never interprets Guide authority.
    graph = DocumentGraph(documents, ())
    canonical = {
        "formatVersion": 1,
        "documents": [
            {
                "namespace": item.identity.namespace,
                "id": item.identity.value,
                "location": item.location.locator,
                "revision": item.revision.value,
            }
            for item in sorted(graph.documents, key=lambda doc: doc.location.locator)
        ],
        "relations": [],
        "relationApproval": registry.relation_approval,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    key = "relation-approval-pending"
    entries: list[DocumentStatus] = [
        {
            "namespace": doc.identity.namespace,
            "id": doc.identity.value,
            "location": doc.location.locator,
            "revision": doc.revision.value,
            "outcome": None,
        }
        for doc in sorted(graph.documents, key=lambda document: document.location.locator)
    ]
    coverage: list[Coverage] = [
        {"scope": "Snapshot", "target": None, "check": "capture", "status": "Evaluated", "blockingFindingKey": None}
    ]
    for item in entries:
        target = f"{item['namespace']}:{item['id']}"
        coverage.append(
            {
                "scope": "Document",
                "target": target,
                "check": "capture",
                "status": "Evaluated",
                "blockingFindingKey": None,
            }
        )
        coverage.append(
            {
                "scope": "Document",
                "target": target,
                "check": "validation",
                "status": "Blocked",
                "blockingFindingKey": key,
            }
        )
    coverage.append(
        {"scope": "Relation", "target": None, "check": "approval", "status": "Blocked", "blockingFindingKey": key}
    )
    coverage.append(
        {"scope": "Snapshot", "target": None, "check": "validation", "status": "Blocked", "blockingFindingKey": key}
    )
    return {
        "schemaVersion": 1,
        "repositoryId": repository_id,
        "snapshot": {"formatVersion": 1, "fingerprint": fingerprint},
        "ruleSet": {"id": "local-dev-mcp-initial", "version": "1"},
        "completion": "Blocked",
        "summary": {
            "totalDocuments": len(entries),
            "capturedDocuments": len(entries),
            "evaluatedDocuments": 0,
            "statusCounts": {"Verified": 0, "Inconsistent": 0, "Unverified": 0},
            "approvedRelations": 0,
        },
        "documents": entries,
        "coverage": coverage,
        "findings": [
            {
                "key": key,
                "code": "RELATIONS_APPROVAL_PENDING",
                "severity": "warning",
                "scope": "Snapshot",
                "category": "blocker",
                "targets": [],
                "evidence": [{"kind": "registry", "location": GUIDE_PATH, "pointer": "relationApproval.status"}],
            }
        ],
    }
