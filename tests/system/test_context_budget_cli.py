"""CLI validates offline, sanitized observations without external MCP calls."""

import json
from pathlib import Path

import pytest

from local_dev_mcp.tools.context_budget import main

pytestmark = pytest.mark.integration


def test_cli_emits_comparison_without_fabricating_model_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {
        "case_id": "example-location",
        "repo_sha": "a" * 40,
        "correct": True,
        "fallback": False,
        "verified_reference": "src/local_dev_mcp/tools/git/facades/remote_pr.py:567",
        "tokenizer": None,
        "elapsed_ms": None,
    }
    payload = {
        "schema_version": 1,
        "runs": [
            {
                **common,
                "strategy": "baseline",
                "index_state": "not_applicable",
                "tool_calls": [
                    {"name": "search_workspace_text", "outcome": "ok", "output_bytes": 250, "output_tokens": None}
                ],
            },
            {
                **common,
                "strategy": "trail",
                "index_state": "stale",
                "tool_calls": [{"name": "trail_find_all", "outcome": "ok", "output_bytes": 300, "output_tokens": None}],
            },
        ],
    }
    path = tmp_path / "sanitized.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 1
    assert report["median_output_byte_saving_pct"] == -20
    assert report["median_output_token_saving_pct"] is None
    assert report["model_input_tokens_measured"] is False


def test_cli_invalid_input_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version":1,"runs":[]}', encoding="utf-8")
    assert main([str(path)]) == 2
    assert "Invalid benchmark input" in capsys.readouterr().err
