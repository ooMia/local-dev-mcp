"""Deterministic release outputs and checks, shared by preparation and integration."""

import copy
import json
import tomllib
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from ..towncrier import render_notes
from ._models import ReleaseReceipt
from ._plan import ReleasePlan

if TYPE_CHECKING:
    from ._snapshots import Snapshot

MARKER = b"<!-- towncrier release notes start -->\n"


def expected_receipt(snapshot: Snapshot) -> ReleaseReceipt:
    plan = snapshot.plan()
    if plan["next_version"] is None:
        raise ValueError("No effective releasable impact remains; a none-only plan cannot prepare a version.")
    return ReleaseReceipt(
        schema_version=1,
        source_head=snapshot.head,
        plan_digest=plan["digest"],
        old_version=plan["current_version"],
        new_version=plan["next_version"],
        consumed=sorted(snapshot.records),
    )


def receipt_bytes(receipt: ReleaseReceipt) -> bytes:
    return (
        "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in receipt.model_dump().items())
        + "\n"
    ).encode()


def changelog_bytes(previous: bytes | None, plan: ReleasePlan) -> bytes:
    version = plan["next_version"]
    if version is None:
        raise ValueError("A none-only plan does not produce a release changelog.")
    existing = previous if previous is not None else b"# Changelog\n\n" + MARKER + b"\n"
    if existing.count(MARKER) != 1 or f"## [{version}]".encode() in existing:
        raise ValueError("Preserve one changelog insertion marker and do not duplicate a released version.")
    sections: dict[str, list[str]] = {name: [] for name in ("major", "minor", "patch", "none")}
    for change in plan["changes"]:
        if not change["effective"]:
            continue
        text = f"**{change['id']}**: {change['summary']}\n\n  {change['reason']}\n\n  Contract: {change['contract']}"
        if change["migration"]:
            text += f"\n\n  Migration: {change['migration']}"
        sections[change["impact"]].append(text)
    notes = render_notes(sections)
    entry = f"\n## [{version}]\n\n{notes}\n".encode()
    return existing.replace(MARKER, MARKER + entry, 1)


def _document(content: bytes | None) -> dict[str, object]:
    if content is None:
        raise ValueError("A release requires a tracked uv.lock and explicit metadata files.")
    try:
        return cast(dict[str, object], tomllib.loads(content.decode()))
    except UnicodeError, tomllib.TOMLDecodeError:
        raise ValueError("Prepared release metadata is not valid UTF-8 TOML.") from None


def _table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Prepared release project metadata must be TOML tables.")
    return cast(dict[str, object], value)


def _lock_at_version(snapshot: Snapshot, expected: str, normalized: str) -> dict[str, object]:
    lock = _document(snapshot.blobs.get("uv.lock"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("A release requires a valid locked project package.")
    name = _table(snapshot.project.get("project")).get("name")
    found = 0
    for raw in cast(list[object], packages):
        package = _table(raw)
        if package.get("name") == name:
            if package.get("version") != expected:
                raise ValueError("The locked root package version does not match the release version.")
            package["version"] = normalized
            found += 1
    if found != 1:
        raise ValueError("The lock must contain exactly one selected root package.")
    return lock


def verify_release(previous: Snapshot, candidate: Snapshot, changed: list[str]) -> list[str]:
    expected = expected_receipt(previous)
    try:
        observed = ReleaseReceipt.model_validate(_document(candidate.blobs.get(".release.toml")))
    except ValidationError:
        raise ValueError("The release receipt has invalid or missing fields.") from None
    if observed != expected or candidate.version != expected.new_version or candidate.records:
        raise ValueError("Release receipt, version or consumed assessments differ from the exact base plan.")
    project = copy.deepcopy(previous.project)
    _table(project.get("project"))["version"] = expected.new_version
    if project != candidate.project:
        raise ValueError("Release preparation may only change the project version, not dependencies or configuration.")
    if _lock_at_version(previous, expected.old_version, expected.old_version) != _lock_at_version(
        candidate, expected.new_version, expected.old_version
    ):
        raise ValueError("Release preparation includes unrelated dependency or lock changes.")
    if candidate.blobs.get("CHANGELOG.md") != changelog_bytes(previous.blobs.get("CHANGELOG.md"), previous.plan()):
        raise ValueError("Prepared changelog must preserve old bytes and add only the exact rendered release entry.")
    allowed = {"pyproject.toml", "uv.lock", "CHANGELOG.md", ".release.toml"}
    allowed.update(f".changes/{name}.toml" for name in expected.consumed)
    if set(changed) - allowed:
        raise ValueError("Release preparation contains unrelated source or metadata changes.")
    return expected.consumed
