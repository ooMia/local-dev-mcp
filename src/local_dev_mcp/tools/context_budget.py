"""Compare paired, explicitly metered code-navigation traces without model token claims.

The input contains aggregate tool response sizes, never raw tool responses or credentials.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class BenchmarkError(ValueError):
    """Invalid or incomparable benchmark evidence."""


@dataclass(frozen=True)
class Call:
    name: str
    output_bytes: int
    output_tokens: int | None
    outcome: str


@dataclass(frozen=True)
class Run:
    case_id: str
    repo_sha: str
    strategy: str
    index_state: str
    correct: bool
    fallback: bool
    verified_reference: str
    tool_calls: tuple[Call, ...]
    tokenizer: str | None
    elapsed_ms: int | None


def _fields(value: object, required: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{name} must be an object.")
    data = cast(dict[str, object], value)
    if set(data) != required:
        raise BenchmarkError(f"{name} has missing or unexpected fields.")
    return data


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BenchmarkError(f"{name} must be a non-negative integer.")
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BenchmarkError(f"{name} must be a non-empty string (max 256).")
    return value


def _call(value: object) -> Call:
    row = _fields(value, {"name", "output_bytes", "output_tokens", "outcome"}, "tool call")
    tokens = row["output_tokens"]
    outcome = row["outcome"]
    if outcome not in ("ok", "error"):
        raise BenchmarkError("tool call outcome must be ok or error.")
    return Call(
        name=_nonempty(row["name"], "tool name"),
        output_bytes=_integer(row["output_bytes"], "output_bytes"),
        output_tokens=None if tokens is None else _integer(tokens, "output_tokens"),
        outcome=cast(str, outcome),
    )


def _run(value: object) -> Run:
    required = {
        "case_id",
        "repo_sha",
        "strategy",
        "index_state",
        "correct",
        "fallback",
        "verified_reference",
        "tool_calls",
        "tokenizer",
        "elapsed_ms",
    }
    row = _fields(value, required, "run")
    case_id = _nonempty(row["case_id"], "case_id")
    repo_sha = row["repo_sha"]
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", case_id) is None:
        raise BenchmarkError("case_id must use lowercase letters, digits and hyphens.")
    if not isinstance(repo_sha, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", repo_sha) is None:
        raise BenchmarkError("repo_sha must be an exact full Git SHA.")
    strategy = row["strategy"]
    state = row["index_state"]
    if strategy not in ("baseline", "trail"):
        raise BenchmarkError("strategy must be baseline or trail.")
    if state not in ("not_applicable", "fresh", "stale", "unavailable", "unknown"):
        raise BenchmarkError("Invalid index_state.")
    if (strategy == "baseline" and state != "not_applicable") or (strategy == "trail" and state == "not_applicable"):
        raise BenchmarkError("index_state does not match strategy.")
    if type(row["correct"]) is not bool or type(row["fallback"]) is not bool:
        raise BenchmarkError("correct and fallback must be booleans.")
    raw_calls = row["tool_calls"]
    if not isinstance(raw_calls, list):
        raise BenchmarkError("tool_calls must contain 1-128 entries.")
    call_items = cast(list[object], raw_calls)
    if not 1 <= len(call_items) <= 128:
        raise BenchmarkError("tool_calls must contain 1-128 entries.")
    calls = tuple(_call(item) for item in call_items)
    tokenizer = row["tokenizer"]
    if tokenizer is not None:
        tokenizer = _nonempty(tokenizer, "tokenizer")
    if any(call.output_tokens is not None for call in calls) and tokenizer is None:
        raise BenchmarkError("Metered tokens require an explicit tokenizer.")
    elapsed = row["elapsed_ms"]
    return Run(
        case_id=case_id,
        repo_sha=repo_sha,
        strategy=cast(str, strategy),
        index_state=cast(str, state),
        correct=row["correct"],
        fallback=row["fallback"],
        verified_reference=_nonempty(row["verified_reference"], "verified_reference"),
        tool_calls=calls,
        tokenizer=tokenizer,
        elapsed_ms=None if elapsed is None else _integer(elapsed, "elapsed_ms"),
    )


def parse_runs(value: object) -> list[Run]:
    """Read a bounded, complete baseline/trail pair for each case."""
    doc = _fields(value, {"schema_version", "runs"}, "benchmark")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise BenchmarkError("Unsupported benchmark schema version.")
    raw_runs = doc["runs"]
    if not isinstance(raw_runs, list):
        raise BenchmarkError("runs must contain 2-100 entries.")
    run_items = cast(list[object], raw_runs)
    if not 2 <= len(run_items) <= 100:
        raise BenchmarkError("runs must contain 2-100 entries.")
    runs = [_run(item) for item in run_items]
    pairs: dict[str, dict[str, Run]] = {}
    for run in runs:
        group = pairs.setdefault(run.case_id, {})
        if run.strategy in group:
            raise BenchmarkError("Duplicate case and strategy.")
        group[run.strategy] = run
    for group in pairs.values():
        if set(group) != {"baseline", "trail"}:
            raise BenchmarkError("Every case requires baseline and trail.")
        a, b = group["baseline"], group["trail"]
        if a.repo_sha != b.repo_sha or a.verified_reference != b.verified_reference:
            raise BenchmarkError("Paired runs must have identical repo_sha and verified_reference.")
    return runs


def _saving_pct(baseline: int, trail: int) -> float | None:
    return round(100 * (baseline - trail) / baseline, 2) if baseline else None


def _measured_tokens(run: Run) -> int | None:
    if run.tokenizer is None or any(call.output_tokens is None for call in run.tool_calls):
        return None
    return sum(call.output_tokens for call in run.tool_calls if call.output_tokens is not None)


def summarize(runs: list[Run]) -> dict[str, object]:
    """Compare exact tool outputs; never infer billed model input or token savings."""
    pairs: dict[str, dict[str, Run]] = {}
    for run in runs:
        pairs.setdefault(run.case_id, {})[run.strategy] = run
    cases: list[dict[str, object]] = []
    byte_pct: list[float] = []
    token_pct: list[float] = []
    for case_id, group in sorted(pairs.items()):
        base, trail = group["baseline"], group["trail"]
        base_bytes = sum(call.output_bytes for call in base.tool_calls)
        trail_bytes = sum(call.output_bytes for call in trail.tool_calls)
        base_tokens, trail_tokens = _measured_tokens(base), _measured_tokens(trail)
        comparable_tokens = (
            base_tokens is not None
            and trail_tokens is not None
            and base.tokenizer is not None
            and base.tokenizer == trail.tokenizer
        )
        byte_saving = _saving_pct(base_bytes, trail_bytes)
        if byte_saving is not None:
            byte_pct.append(byte_saving)
        token_saving = (
            _saving_pct(base_tokens, trail_tokens)
            if comparable_tokens and base_tokens is not None and trail_tokens is not None
            else None
        )
        if token_saving is not None:
            token_pct.append(token_saving)
        cases.append(
            {
                "case_id": case_id,
                "repo_sha": base.repo_sha,
                "reference": base.verified_reference,
                "baseline_correct": base.correct,
                "trail_correct": trail.correct,
                "trail_index_state": trail.index_state,
                "trail_fallback": trail.fallback,
                "baseline_calls": len(base.tool_calls),
                "trail_calls": len(trail.tool_calls),
                "baseline_errors": sum(call.outcome == "error" for call in base.tool_calls),
                "trail_errors": sum(call.outcome == "error" for call in trail.tool_calls),
                "baseline_output_bytes": base_bytes,
                "trail_output_bytes": trail_bytes,
                "output_byte_saving_pct": byte_saving,
                "tokenizer": base.tokenizer if comparable_tokens else None,
                "baseline_output_tokens": base_tokens if comparable_tokens else None,
                "trail_output_tokens": trail_tokens if comparable_tokens else None,
                "output_token_saving_pct": token_saving,
                "baseline_elapsed_ms": base.elapsed_ms,
                "trail_elapsed_ms": trail.elapsed_ms,
            }
        )
    return {
        "schema_version": 1,
        "measurement_scope": "tool_responses_only",
        "model_input_tokens_measured": False,
        "case_count": len(cases),
        "trail_correct_cases": sum(c["trail_correct"] is True for c in cases),
        "baseline_correct_cases": sum(c["baseline_correct"] is True for c in cases),
        "trail_fresh_cases": sum(c["trail_index_state"] == "fresh" for c in cases),
        "median_output_byte_saving_pct": round(statistics.median(byte_pct), 2) if byte_pct else None,
        "median_output_token_saving_pct": round(statistics.median(token_pct), 2) if token_pct else None,
        "token_comparable_cases": len(token_pct),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare sanitized, paired code-navigation tool traces.")
    parser.add_argument("input", type=Path, help="JSON file with schema_version=1 and paired runs")
    args = parser.parse_args(argv)
    try:
        raw: object = json.loads(args.input.read_text(encoding="utf-8"))
        report = summarize(parse_runs(raw))
    except (BenchmarkError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Invalid benchmark input: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
