"""Guard audit completeness without implying that catalog checks execute every tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_dev_mcp.mcp.server import TOOLS
from local_dev_mcp.quality import platform_audit as audit

pytestmark = pytest.mark.unit


def test_explicit_tool_inventory_matches_all_public_mcp_definitions() -> None:
    issues = audit.inventory_issues()
    assert issues == {"missing": [], "unexpected": [], "duplicate": []}
    names = audit.catalog_names()
    assert len(names) == 71
    assert len(set(names)) == len(names)
    assert len(audit.TOOL_FAMILIES) == 11
    assert set(audit.READ_PROBES).issubset(names)


def test_read_probes_are_actually_read_only() -> None:
    by_name = {definition.name: definition for definition in TOOLS}
    assert all(by_name[name].annotations.read_only_hint is True for name in audit.READ_PROBES)


def test_wsl_identification_uses_host_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.sys, "platform", "linux")
    assert audit.is_wsl(kernel="6.6.87.1-microsoft-standard-WSL2", distro="")
    assert audit.is_wsl(kernel="6.12.0-generic", distro="Ubuntu")
    assert not audit.is_wsl(kernel="6.12.0-generic", distro="")
    monkeypatch.setattr(audit.sys, "platform", "darwin")
    assert not audit.is_wsl(kernel="6.6.87-microsoft", distro="Ubuntu")


def test_catalog_is_never_reported_as_functional_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "binary_inventory", lambda: {})
    report = audit.build_report(Path("/tmp/tool-audit-test"))
    assert report["catalog"]["state"] == "NOT_RUN"
    assert report["readiness"]["state"] == "NOT_RUN"
    assert len(report["tools"]) == 71
    assert all(item["functional"] == "NOT_RUN" for item in report["tools"])
    assert all(state == "NOT_RUN" for state in report["read_probes"].values())
    assert "mutating and external tools are NOT_RUN" in report["evidence_boundary"]


def test_binary_inventory_reports_windows_interop_without_running_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = {"git": "/usr/bin/git", "wt": "/mnt/c/Windows/wt.exe"}
    monkeypatch.setattr(audit.shutil, "which", lambda name: found.get(name))
    report = audit.binary_inventory()
    assert report["git"]["available"] is True
    assert report["git"]["windows_interop_candidate"] is False
    assert report["wt"]["windows_interop_candidate"] is True
    assert report["trail"]["available"] is False
    assert report["trail"]["path"] is None


def test_catalog_only_wsl_gate_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(audit.sys, "argv", ["platform_audit", "--catalog-only", "--require-wsl"])
    monkeypatch.setattr(audit, "is_wsl", lambda: False)
    monkeypatch.setattr(audit, "binary_inventory", lambda: {})
    assert audit.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["host"]["gate"] == "BLOCKED_NOT_WSL"
    assert report["outcome"] == "FAIL"
    assert report["catalog"]["state"] == "NOT_RUN"
