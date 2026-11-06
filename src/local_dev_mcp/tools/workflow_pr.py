"""Deterministic PR body collection, rendering and guarded remote synchronization."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlparse

Run = Callable[..., str]
_IMPACTS = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_EXCEPTIONS = {"Activation", "Compatibility", "Migration", "Verification gap", "Review blocker"}
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_BLOCKS = ("purpose", "generated", "exceptions")
_BODY = re.compile(
    r"\A<!-- pr-body:purpose:start -->\n(?P<purpose>.*?)\n<!-- pr-body:purpose:end -->\n\n"
    r"<!-- pr-body:generated:start -->\n(?P<generated>.*?)\n"
    r"<!-- pr-body:generated-sha256:(?P<digest>[0-9a-f]{64}) -->\n"
    r"<!-- pr-body:generated:end -->\n\n"
    r"<!-- pr-body:exceptions:start -->\n(?P<exceptions>.*?)\n"
    r"<!-- pr-body:exceptions:end -->\n?\Z",
    re.DOTALL,
)
_HEAD = re.compile(r"\A<!-- pr-body:head:([0-9a-f]{40}|[0-9a-f]{64}) -->\n")


def _reference(value: str) -> str:
    if not _REF.fullmatch(value) or ".." in value or value.endswith(".lock"):
        raise ValueError("Unsafe Git reference for PR collection.")
    return value


def _sha(root: Path, run: Run, ref: str) -> str:
    value = run(root, "git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", capture=True).strip()
    if _SHA.fullmatch(value) is None:
        raise ValueError("Git did not resolve an exact full commit SHA.")
    return value


def _repo_url(root: Path, run: Run) -> str:
    repo = json.loads(run(root, "gh", "repo", "view", "--json", "url", capture=True))
    url = repo.get("url")
    if not isinstance(url, str):
        raise ValueError("GitHub CLI did not provide a repository URL.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or len(parsed.path.strip("/").split("/")) != 2:
        raise ValueError("Unexpected GitHub repository URL.")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("GitHub repository URL contains unexpected components.")
    return url.rstrip("/")


def _commit_list(root: Path, run: Run, base: str, head: str) -> list[dict[str, str]]:
    data = run(root, "git", "log", "--reverse", "-z", "--format=%H%x00%s", base + ".." + head, capture=True)
    fields = data.rstrip("\x00").split("\x00") if data else []
    if not fields or len(fields) % 2:
        raise ValueError("Expected a nonempty, complete commit history.")
    result: list[dict[str, str]] = []
    for sha, title in zip(fields[::2], fields[1::2], strict=True):
        if _SHA.fullmatch(sha) is None or not title.strip():
            raise ValueError("Invalid commit history record.")
        result.append({"sha": sha, "title": title.strip()})
    return result


def _new_assessments(root: Path, run: Run, base: str, head: str) -> list[dict[str, str]]:
    paths = run(
        root, "git", "diff", "--name-only", "--diff-filter=A", "-z", base, head, "--", ".changes/", capture=True
    )
    records: list[dict[str, str]] = []
    for path in sorted(filter(None, paths.split("\x00"))):
        if not path.startswith(".changes/") or not path.endswith(".toml"):
            continue
        key = path[len(".changes/") : -len(".toml")]
        if _ID.fullmatch(key) is None:
            raise ValueError("Invalid new assessment name.")
        content = run(root, "git", "show", head + ":" + path, capture=True)
        record = tomllib.loads(content)
        fields = ("impact", "summary", "reason", "migration")
        if not all(isinstance(record.get(k), str) for k in fields):
            raise ValueError("Assessment contains missing or non-text fields: " + key)
        if record["impact"] not in _IMPACTS or not record["summary"].strip() or not record["reason"].strip():
            raise ValueError("Invalid release assessment: " + key)
        records.append({"id": key, **{k: cast(str, record[k]) for k in fields}})
    if not records:
        raise ValueError("PR must include a new explicit .changes assessment.")
    return records


def collect(root: Path, run: Run, base: str, head: str) -> dict[str, Any]:
    """Collect only if the supplied base matches the live remote main."""
    base_sha = _sha(root, run, _reference(base))
    head_sha = _sha(root, run, _reference(head))
    common = run(root, "git", "merge-base", base_sha, head_sha, capture=True).strip()
    if _SHA.fullmatch(common) is None:
        raise ValueError("No usable common Git ancestor.")
    if common != base_sha:
        raise ValueError("Feature HEAD does not contain the base; integrate origin/main before creating a PR.")

    repo_url = _repo_url(root, run)
    owner, repo = urlparse(repo_url).path.strip("/").split("/")
    endpoint = f"repos/{owner}/{repo}/git/ref/heads/main"
    remote: dict[str, Any] = json.loads(run(root, "gh", "api", endpoint, capture=True))
    object_info: object = remote.get("object")
    remote_sha: object = cast(dict[str, object], object_info).get("sha") if isinstance(object_info, dict) else None
    if not isinstance(remote_sha, str) or _SHA.fullmatch(remote_sha) is None:
        raise ValueError("GitHub did not return a valid main commit SHA.")
    if remote_sha != base_sha:
        raise ValueError("Base differs from remote main; fetch origin/main and reconcile before creating a PR.")

    commits = _commit_list(root, run, base_sha, head_sha)
    records = _new_assessments(root, run, base_sha, head_sha)
    return {
        "schema": 1,
        "repo_url": repo_url,
        "base": base_sha,
        "merge_base": common,
        "head": head_sha,
        "commits": commits,
        "assessments": records,
    }


def _facts(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Unsupported PR snapshot format.")
    data = cast(dict[str, Any], value)
    if data.get("schema") != 1:
        raise ValueError("Unsupported PR snapshot format.")
    repo = data.get("repo_url")
    head = data.get("head")
    if (
        not isinstance(repo, str)
        or not repo.startswith("https://")
        or not isinstance(head, str)
        or _SHA.fullmatch(head) is None
    ):
        raise ValueError("Invalid PR snapshot identity.")
    for key in ("commits", "assessments"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise ValueError("PR snapshot missing " + key + ".")
    return data


def _review(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Review context must contain only purpose and exceptions.")
    review = cast(dict[str, Any], value)
    if set(review) - {"purpose", "exceptions"}:
        raise ValueError("Review context must contain only purpose and exceptions.")
    if "purpose" in review and (not isinstance(review["purpose"], str) or not review["purpose"].strip()):
        raise ValueError("Purpose must be nonempty text.")
    rows = review.get("exceptions", [])
    if not isinstance(rows, list):
        raise ValueError("Exceptions must be a list.")
    for candidate in cast(list[object], rows):
        if not isinstance(candidate, dict):
            raise ValueError("Each exception requires type, detail and evidence.")
        row = cast(dict[str, Any], candidate)
        if set(row) != {"type", "detail", "evidence"}:
            raise ValueError("Each exception requires type, detail and evidence.")
        if row["type"] not in _EXCEPTIONS or not all(
            isinstance(row[k], str) and row[k].strip() for k in ("detail", "evidence")
        ):
            raise ValueError("Invalid exception type, detail or evidence.")
    return review


def _purpose(facts: dict[str, Any], review: dict[str, Any]) -> str:
    if "purpose" in review:
        result = review["purpose"].strip()
    else:
        assessments = facts["assessments"]
        strongest = max(_IMPACTS[r["impact"]] for r in assessments)
        reasons = list(dict.fromkeys(r["reason"].strip() for r in assessments if _IMPACTS[r["impact"]] == strongest))
        if len(reasons) != 1:
            raise ValueError("Multiple equally important purposes; supply reviewed purpose.")
        result = reasons[0]
    if len(result) > 500 or "\x00" in result:
        raise ValueError("Purpose is too long or invalid.")
    return "## Purpose\n\n" + result


def _exceptions(facts: dict[str, Any], review: dict[str, Any]) -> str:
    rows: list[tuple[str, str]] = []
    for assessment in facts["assessments"]:
        if assessment["migration"].strip():
            rows.append(("Migration", assessment["migration"].strip()))
    for row in review.get("exceptions", []):
        rows.append((row["type"], row["detail"].strip()))
    return "\n\n".join("## " + name + "\n\n" + detail for name, detail in rows)


def _inline(value: str) -> str:
    """Escape untrusted Git subjects and assessment summaries for Markdown lists."""
    result = " ".join(value.splitlines()).strip()
    for token in ("\\", "\u0060", "[", "]", "*", "_"):
        result = result.replace(token, "\\" + token)
    return result.replace("<", "&lt;").replace(">", "&gt;")


def _generated(facts: dict[str, Any], number: int | None) -> str:
    tick = chr(96)
    assessments = facts["assessments"]
    lines = ["<!-- pr-body:head:" + facts["head"] + " -->", "## Changes", ""]
    lines += ["- " + _inline(item["summary"]) for item in assessments]
    lines += ["", "## Release impact", ""]
    lines += ["- " + tick + item["impact"] + tick + " — " + tick + item["id"] + tick for item in assessments]
    lines += ["", "## Commits", ""]
    commits = facts["commits"]
    if len(commits) <= 5:
        lines += [
            "- ["
            + tick
            + commit["sha"][:7]
            + tick
            + "]("
            + facts["repo_url"]
            + "/commit/"
            + commit["sha"]
            + ") — "
            + _inline(commit["title"])
            for commit in commits
        ]
    elif number is None:
        lines.append(str(len(commits)) + " commits · PR Commits link available after creation")
    else:
        lines.append(
            str(len(commits)) + " commits · [Full history](" + facts["repo_url"] + "/pull/" + str(number) + "/commits)"
        )
    return "\n".join(lines)


def _insert(template: str, slot: str, content: str) -> str:
    start = "<!-- pr-body:" + slot + ":start -->\n"
    end = "\n<!-- pr-body:" + slot + ":end -->"
    if template.count(start) != 1 or template.count(end) != 1:
        raise ValueError("PR template is missing a unique slot: " + slot)
    a = template.index(start) + len(start)
    b = template.index(end, a)
    return template[:a] + content + template[b:]


def compose(template: str, purpose: str, generated: str, exceptions: str) -> str:
    digest = hashlib.sha256(generated.encode("utf-8")).hexdigest()
    output = template
    for slot, body in (
        ("purpose", purpose),
        ("generated", generated + "\n<!-- pr-body:generated-sha256:" + digest + " -->"),
        ("exceptions", exceptions),
    ):
        output = _insert(output, slot, body)
    return output.rstrip("\n") + "\n"


def render(root: Path, facts: object, context: object = None, number: int | None = None) -> str:
    data, review = _facts(facts), _review(context)
    template = (root / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    return compose(template, _purpose(data, review), _generated(data, number), _exceptions(data, review))


def parse(body: str) -> dict[str, str]:
    match = _BODY.fullmatch(body)
    if match is None:
        raise ValueError("Existing PR body has missing, duplicated or unsupported region markers.")
    regions = match.groupdict()
    if hashlib.sha256(regions["generated"].encode("utf-8")).hexdigest() != regions["digest"]:
        raise ValueError("Generated PR region was externally changed; refusing to overwrite.")
    head = _HEAD.match(regions["generated"])
    if head is None:
        raise ValueError("Generated PR region lacks its source HEAD.")
    return {"purpose": regions["purpose"], "exceptions": regions["exceptions"], "head": head[1]}


def refresh(root: Path, facts: object, previous: str, number: int, *, accept_new_head: bool = False) -> str:
    old = parse(previous)
    data = _facts(facts)
    if old["head"] != data["head"] and not accept_new_head:
        raise ValueError("PR HEAD changed; review purpose and exceptions before --accept-new-head.")
    template = (root / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    return compose(template, old["purpose"], _generated(data, number), old["exceptions"])


def sync(
    root: Path, run: Run, number: int, base: str, *, base_ref: str = "origin/main", accept_new_head: bool = False
) -> str:
    if number < 1:
        raise ValueError("PR number must be positive.")
    fields = "body,headRefOid,baseRefOid,baseRefName,state,url"
    remote = json.loads(run(root, "gh", "pr", "view", str(number), "--json", fields, capture=True))
    if remote["state"] != "OPEN" or remote["baseRefName"] != base:
        raise ValueError("PR is not open or has a different base branch.")
    data = collect(root, run, base_ref, "HEAD")
    if remote["headRefOid"] != data["head"] or remote["baseRefOid"] != data["base"]:
        raise ValueError("Local refs differ from the remote PR base or head; synchronize first.")
    if remote["url"] != data["repo_url"] + "/pull/" + str(number):
        raise ValueError("PR does not belong to the selected GitHub repository.")
    updated = refresh(root, data, remote["body"], number, accept_new_head=accept_new_head)
    if updated == remote["body"]:
        return updated
    current = json.loads(run(root, "gh", "pr", "view", str(number), "--json", fields, capture=True))
    if current != remote:
        raise ValueError("Remote PR changed during synchronization; no update made.")
    # gh pr edit has no atomic compare-and-swap; a concurrent edit after this
    # final read remains a known limitation, not a transactional guarantee.
    run(root, "gh", "pr", "edit", str(number), "--body-file", "-", input_text=updated)
    return updated
