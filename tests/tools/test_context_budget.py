"""Offline evaluation does not execute either MCP provider or infer model billing."""

import copy
from typing import Any, cast

import pytest

from local_dev_mcp.tools.context_budget import BenchmarkError, parse_runs, summarize

pytestmark = pytest.mark.unit

SHA = "a" * 40


def sample() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runs": [
            {
                "case_id": "find-pr-url",
                "repo_sha": SHA,
                "strategy": "baseline",
                "index_state": "not_applicable",
                "correct": True,
                "fallback": False,
                "verified_reference": "src/local_dev_mcp/tools/git/facades/remote_pr.py:567",
                "tool_calls": [
                    {"name": "search_workspace_text", "output_bytes": 400, "output_tokens": 100, "outcome": "ok"},
                    {"name": "read_workspace_file", "output_bytes": 800, "output_tokens": 200, "outcome": "ok"},
                ],
                "tokenizer": "fixed-test-tokenizer",
                "elapsed_ms": 40,
            },
            {
                "case_id": "find-pr-url",
                "repo_sha": SHA,
                "strategy": "trail",
                "index_state": "fresh",
                "correct": True,
                "fallback": False,
                "verified_reference": "src/local_dev_mcp/tools/git/facades/remote_pr.py:567",
                "tool_calls": [
                    {"name": "trail_find_all", "output_bytes": 600, "output_tokens": 150, "outcome": "ok"},
                ],
                "tokenizer": "fixed-test-tokenizer",
                "elapsed_ms": 80,
            },
        ],
    }


def test_compares_complete_pairs_without_fabricating_model_billing() -> None:
    report = summarize(parse_runs(sample()))
    assert report["measurement_scope"] == "tool_responses_only"
    assert report["model_input_tokens_measured"] is False
    assert report["case_count"] == 1
    assert report["median_output_byte_saving_pct"] == 50
    assert report["median_output_token_saving_pct"] == 50
    assert report["token_comparable_cases"] == 1
    case = cast(list[dict[str, object]], report["cases"])[0]
    assert case["baseline_calls"] == 2
    assert case["trail_calls"] == 1
    assert case["baseline_output_tokens"] == 300
    assert case["trail_output_tokens"] == 150
    assert case["baseline_elapsed_ms"] == 40


def test_absent_or_mismatched_tokenizer_does_not_claim_token_savings() -> None:
    data = sample()
    runs = data["runs"]
    runs[1]["tokenizer"] = "other-tokenizer"
    report = summarize(parse_runs(data))
    assert report["token_comparable_cases"] == 0
    assert report["median_output_token_saving_pct"] is None
    assert report["median_output_byte_saving_pct"] == 50

    data = sample()
    runs = data["runs"]
    runs[1]["tool_calls"][0]["output_tokens"] = None
    report = summarize(parse_runs(data))
    assert report["token_comparable_cases"] == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("sha", "repo_sha"),
        ("reference", "verified_reference"),
        ("duplicate", "Duplicate"),
        ("incomplete", "2-100"),
        ("index", "index_state"),
        ("boolean", "booleans"),
        ("bytes", "output_bytes"),
        ("outcome", "outcome"),
        ("tokenizer", "explicit tokenizer"),
        ("schema", "schema version"),
        ("extra", "unexpected fields"),
    ],
)
def test_rejects_uncomparable_or_invalid_evidence(mutation: str, expected: str) -> None:
    data = copy.deepcopy(sample())
    run = data["runs"][1]
    if mutation == "sha":
        run["repo_sha"] = "b" * 40
    elif mutation == "reference":
        run["verified_reference"] = "foreign.py:1"
    elif mutation == "duplicate":
        run.update(strategy="baseline", index_state="not_applicable")
    elif mutation == "incomplete":
        data["runs"].pop()
    elif mutation == "index":
        run["index_state"] = "not_applicable"
    elif mutation == "boolean":
        run["correct"] = 1
    elif mutation == "bytes":
        run["tool_calls"][0]["output_bytes"] = True
    elif mutation == "outcome":
        run["tool_calls"][0]["outcome"] = "ignored"
    elif mutation == "tokenizer":
        run["tokenizer"] = None
    elif mutation == "schema":
        data["schema_version"] = True
    elif mutation == "extra":
        run["extra"] = "unknown"
    with pytest.raises(BenchmarkError, match=expected):
        parse_runs(data)


def test_includes_failed_calls_and_fallback_cost_in_trail_totals() -> None:
    data = sample()
    run = data["runs"][1]
    run["index_state"] = "stale"
    run["correct"] = False
    run["fallback"] = True
    run["tool_calls"].append(
        {"name": "read_workspace_file", "output_bytes": 1000, "output_tokens": 250, "outcome": "error"}
    )
    report = summarize(parse_runs(data))
    case = cast(list[dict[str, object]], report["cases"])[0]
    assert case["trail_errors"] == 1
    assert case["trail_fallback"] is True
    assert case["trail_correct"] is False
    assert case["output_byte_saving_pct"] == -33.33
    assert report["trail_fresh_cases"] == 0
