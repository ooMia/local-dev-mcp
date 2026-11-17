"""Deterministic aggregation of recorded decisions; never infer compatibility."""

import hashlib
import json
from collections.abc import Mapping
from typing import TypedDict

from ._models import COMMIT_ID, IDENTIFIER, IMPACTS, Assessment, Impact, canonical_version


class PlannedChange(TypedDict):
    id: str
    impact: Impact
    summary: str
    reason: str
    contract: str
    assessed_by: str
    migration: str
    supersedes: list[str]
    effective: bool


class ReleasePlan(TypedDict):
    head: str
    digest: str
    current_version: str
    next_version: str | None
    maximum_impact: Impact
    counts: dict[Impact, int]
    causes: list[str]
    changes: list[PlannedChange]


def _superseded(records: Mapping[str, Assessment]) -> set[str]:
    replaced: set[str] = set()
    for identifier, record in records.items():
        for target in record.supersedes:
            if target not in records or target == identifier or target in replaced:
                raise ValueError("Invalid, missing or multiply superseded assessment.")
            replaced.add(target)
    completed: set[str] = set()

    def visit(identifier: str, chain: frozenset[str]) -> None:
        if identifier in chain:
            raise ValueError("Cyclic superseded assessments are not a release decision.")
        if identifier in completed:
            return
        for target in records[identifier].supersedes:
            visit(target, chain | {identifier})
        completed.add(identifier)

    for identifier in records:
        visit(identifier, frozenset())
    return replaced


def build_plan(head: str, current_version: str, records: Mapping[str, Assessment]) -> ReleasePlan:
    if COMMIT_ID.fullmatch(head) is None:
        raise ValueError("A complete observed commit SHA is required.")
    if len(records) > 256 or any(IDENTIFIER.fullmatch(name) is None for name in records):
        raise ValueError("Use at most 256 uniquely named assessments.")
    version = canonical_version(current_version)
    replaced = _superseded(records)
    changes: list[PlannedChange] = []
    counts = dict.fromkeys(IMPACTS, 0)
    for name, record in sorted(records.items()):
        effective = name not in replaced
        if effective:
            counts[record.impact] += 1
        changes.append(
            {
                "id": name,
                "impact": record.impact,
                "summary": record.summary,
                "reason": record.reason,
                "contract": record.contract,
                "assessed_by": record.assessed_by,
                "migration": record.migration,
                "supersedes": list(record.supersedes),
                "effective": effective,
            }
        )
    maximum: Impact = "none"
    for impact in reversed(IMPACTS):
        if counts[impact]:
            maximum = impact
            break
    causes = [item["id"] for item in changes if item["effective"] and item["impact"] == maximum]
    next_version: str | None = None
    if maximum != "none":
        major, minor, patch = version.release
        if maximum == "major" and major > 0:
            major, minor, patch = major + 1, 0, 0
        elif maximum in ("major", "minor"):
            minor, patch = minor + 1, 0
        else:
            patch += 1
        next_version = f"{major}.{minor}.{patch}"
    payload = {"head": head, "current_version": current_version, "changes": changes}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {
        "head": head,
        "digest": digest,
        "current_version": current_version,
        "next_version": next_version,
        "maximum_impact": maximum,
        "counts": counts,
        "causes": causes,
        "changes": changes,
    }
