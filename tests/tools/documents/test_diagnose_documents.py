"""MCP document diagnosis: scoped access, blocked semantics and stale detection."""

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client

from local_dev_mcp.mcp.server import create_server
from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.documents.registry import GUIDE_PATH, MANAGED_PATHS
from local_dev_mcp.tools.workspace import SnapshotChanged, SnapshotObservation, SnapshotReader
from tests.support.mcp import failure, success
from tests.support.workspace import configure

pytestmark = pytest.mark.integration


def prepare(tmp_path: Path) -> tuple[Path, Policy]:
    root = tmp_path / "local-dev-mcp"
    root.mkdir()
    configure(root)
    entries: list[dict[str, str]] = []
    for number, path in enumerate(sorted(MANAGED_PATHS)):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# Managed document {number}\nPRIVATE_CONTENT_SHOULD_NOT_BE_EXPOSED\n")
        entries.append({"namespace": "local-dev-mcp", "id": f"doc-{number}", "path": path})
    guide = root / GUIDE_PATH
    guide.write_text(
        "# Guide\n```json\n"
        + json.dumps(
            {
                "schemaVersion": 1,
                "documents": entries,
                "relations": [],
                "relationApproval": {"status": "pending", "record": None},
            }
        )
        + "\n```\n"
    )
    return root, Policy(root, (root,), ())


def test_mcp_returns_blocked_fingerprint_without_modifying_files(tmp_path: Path) -> None:
    root, policy = prepare(tmp_path)
    before = {path: (root / path).read_bytes() for path in [GUIDE_PATH, *MANAGED_PATHS]}
    server = create_server(policy)

    async def scenario() -> None:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            tool = tools["diagnose_documents"]
            assert tool.annotations is not None and tool.annotations.read_only_hint is True
            assert tool.input_schema["required"] == ["repository_id"]
            assert tool.input_schema["additionalProperties"] is False
            first = await success(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            second = await success(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            assert first == second
            assert first["completion"] == "Blocked"
            summary = first["summary"]
            documents = first["documents"]
            assert isinstance(summary, dict)
            assert isinstance(documents, list)
            assert cast(dict[str, object], summary).get("evaluatedDocuments") == 0
            assert len(cast(list[object], documents)) == 5
            assert "PRIVATE_CONTENT_SHOULD_NOT_BE_EXPOSED" not in str(first)
            assert "RELATIONS_APPROVAL_PENDING" in str(first["findings"])
            assert await failure(client, "diagnose_documents", {"repository_id": "other"})
            assert await failure(client, "diagnose_documents", {"repository_id": "local-dev-mcp", "path": "."})

    asyncio.run(scenario())
    assert before == {path: (root / path).read_bytes() for path in before}


def test_repository_scope_and_document_denial_fail_closed(tmp_path: Path) -> None:
    root, _ = prepare(tmp_path)
    server = create_server(Policy(root, (root,), (), denied_paths=(root / "docs/contracts",)))

    async def scenario() -> None:
        async with Client(server) as client:
            message = await failure(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            assert "DOCUMENT_READ_FAILED" in message
            assert "PRIVATE_CONTENT" not in message

    asyncio.run(scenario())
    (tmp_path / ".env").write_text("")
    other = create_server(Policy(tmp_path, (tmp_path,), ()))

    async def other_scenario() -> None:
        async with Client(other) as client:
            assert "REPOSITORY_NOT_REGISTERED" in await failure(
                client, "diagnose_documents", {"repository_id": "local-dev-mcp"}
            )

    asyncio.run(other_scenario())


def test_invalid_guide_reports_execution_error_not_verified(tmp_path: Path) -> None:
    root, policy = prepare(tmp_path)
    (root / GUIDE_PATH).write_text("# Guide without a registry\n")

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            error = await failure(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            assert "REGISTRY_INVALID" in error

    asyncio.run(scenario())


def test_modified_input_during_diagnosis_has_no_normal_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, policy = prepare(tmp_path)
    original = SnapshotReader.verify

    def changed(reader: SnapshotReader, observation: SnapshotObservation) -> None:
        if observation.path == GUIDE_PATH:
            (root / "AGENTS.md").write_text("# changed during diagnosis\n")
        return original(reader, observation)

    monkeypatch.setattr(SnapshotReader, "verify", changed)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            message = await failure(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            assert "SNAPSHOT_CHANGED" in message

    asyncio.run(scenario())


def test_change_during_guide_capture_is_snapshot_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, policy = prepare(tmp_path)
    original = SnapshotReader.read

    def changed(self: SnapshotReader, relative: str) -> SnapshotObservation:
        if relative == GUIDE_PATH:
            raise SnapshotChanged()
        return original(self, relative)

    monkeypatch.setattr(SnapshotReader, "read", changed)

    async def scenario() -> None:
        async with Client(create_server(policy)) as client:
            error = await failure(client, "diagnose_documents", {"repository_id": "local-dev-mcp"})
            assert "SNAPSHOT_CHANGED" in error
            assert "DOCUMENT_READ_FAILED" not in error

    asyncio.run(scenario())
