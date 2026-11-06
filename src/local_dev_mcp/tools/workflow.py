"""Project workflow commands using the existing registered checks, Git and GitHub CLI.

This developer CLI does not start MCP or read dotenv. Explicit CLI authentication is inherited.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import cast

from ..execution import base_environment
from ._workflow_hooks import inspect_hooks, install_hooks

_CHECKS = ("format_check", "lint", "typecheck", "unit", "integration")
_SOURCE = re.compile(r"^Source-PR: https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$", re.MULTILINE)


def developer_environment() -> dict[str, str]:
    # Preserve explicit CLI authentication without forwarding Git's hook-local repository variables.
    names = (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "GH_CONFIG_DIR",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_HOST",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
    )
    return {**base_environment(), **{name: os.environ[name] for name in names if name in os.environ}}


def run(root: Path, *argv: str, capture: bool = False, input_text: str | None = None) -> str:
    environment = developer_environment()
    if argv and argv[0] == "git" and "GIT_INDEX_FILE" in os.environ:
        environment["GIT_INDEX_FILE"] = os.environ["GIT_INDEX_FILE"]
    result = subprocess.run(
        argv,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL if input_text is None else None,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        timeout=1800,
        check=True,
    )
    return result.stdout if capture else ""


def strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Registered commands and targets must be string arrays.")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item:
            raise ValueError("Registered commands and targets require nonempty strings.")
        result.append(item)
    return result


def quality(root: Path) -> None:
    with (root / "config.toml").open("rb") as stream:
        jobs = tomllib.load(stream)["jobs"]
    for name in _CHECKS:
        recipe = jobs[name]
        argv = strings(recipe["argv"])
        targets = strings(recipe.get("default_targets", []))
        if not argv:
            raise ValueError(f"Empty registered command for {name}.")
        print(f"Workflow check: {name}", flush=True)
        run(root, *argv, *targets)


def require_source(message: str) -> None:
    if _SOURCE.search(message) is None:
        raise ValueError("Main commits require a Source-PR: https://github.com/OWNER/REPO/pull/NUMBER line.")


def require_clean(root: Path, *, staged: bool) -> None:
    # Reject index states that let Git omit differences from the checked working copy.
    entries = run(root, "git", "ls-files", "-v", "-z", capture=True).split("\0")
    if any(entry and (entry[0].islower() or entry.startswith("S ")) for entry in entries):
        raise ValueError("Clear assume-unchanged/skip-worktree flags before main validation.")
    # A hook must check the exact content being committed, not a different working copy.
    run(root, "git", "diff", "--quiet", "--no-ext-diff")
    if not staged:
        run(root, "git", "diff", "--cached", "--quiet", "--no-ext-diff")
    if run(root, "git", "ls-files", "--others", "--exclude-standard", capture=True).strip():
        raise ValueError("Stage or remove untracked files before main validation.")


def common_directory(root: Path) -> Path:
    return Path(run(root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir", capture=True).strip())


def approve(root: Path, base: str, tree: str, head: str = "") -> None:
    target = common_directory(root) / "workflow-approval.json"
    with NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
        json.dump({"base": base, "tree": tree, "head": head}, stream)
        temporary = Path(stream.name)
    temporary.replace(target)


def approve_main(root: Path, *, squash: bool = False) -> None:
    require_clean(root, staged=False)
    base = run(root, "git", "rev-parse", "refs/heads/main", capture=True).strip()
    head = run(root, "git", "rev-parse", "HEAD", capture=True).strip()
    run(root, "git", "merge-base", "--is-ancestor", base, head)
    if not squash:
        validate_commits(root, base, head)
    quality(root)
    require_clean(root, staged=False)
    if run(root, "git", "rev-parse", "HEAD", capture=True).strip() != head:
        raise ValueError("The candidate HEAD changed during validation.")
    if run(root, "git", "rev-parse", "refs/heads/main", capture=True).strip() != base:
        raise ValueError("Main changed during validation.")
    approve(root, base, run(root, "git", "rev-parse", f"{head}^{{tree}}", capture=True).strip(), "" if squash else head)


def validate_commits(root: Path, base: str, head: str) -> None:
    if run(root, "git", "rev-list", "--min-parents=2", f"{base}..{head}", capture=True).strip():
        raise ValueError("Main requires one-parent feature result commits.")
    for message in run(root, "git", "log", "--format=%B%x00", f"{base}..{head}", capture=True).split("\0"):
        if message.strip():
            require_source(message.strip())


def reference_transaction(root: Path, arguments: list[str]) -> None:
    if arguments != ["prepared"]:
        return
    for row in (line.split() for line in sys.stdin.read().splitlines()):
        if len(row) != 3 or row[2] != "refs/heads/main":
            continue
        old, new, _ = row
        if not old.strip("0") or not new.strip("0"):
            raise ValueError("Main creation or deletion needs explicit bootstrap; it is not an integration.")
        receipt = json.loads((common_directory(root) / "workflow-approval.json").read_text())
        tree = run(root, "git", "rev-parse", f"{new}^{{tree}}", capture=True).strip()
        if receipt["base"] != old or receipt["tree"] != tree:
            raise ValueError("Main update lacks matching quality approval.")
        if receipt["head"]:
            if receipt["head"] != new:
                raise ValueError("Approved main candidate differs from the proposed commit.")
        elif run(root, "git", "show", "-s", "--format=%P", new, capture=True).strip() != old:
            raise ValueError("Main commit parent differs from the tested baseline.")
        run(root, "git", "merge-base", "--is-ancestor", old, new)
        validate_commits(root, old, new)


def hook(root: Path, name: str, arguments: list[str]) -> None:
    if name == "reference-transaction":
        reference_transaction(root, arguments)
        return
    branch = run(root, "git", "branch", "--show-current", capture=True).strip()
    if name == "pre-push":
        updates = [line.split() for line in sys.stdin.read().splitlines()]
        main = [row for row in updates if len(row) == 4 and row[2] == "refs/heads/main"]
        if not main:
            return
        head = run(root, "git", "rev-parse", "HEAD", capture=True).strip()
        if branch != "main" or any(row[1] != head for row in main):
            raise ValueError("Push main only from its checked-out, validated HEAD; deletion is refused.")
        require_clean(root, staged=False)
        require_source(run(root, "git", "show", "-s", "--format=%B", "HEAD", capture=True))
        quality(root)
    elif branch == "main":
        if name == "commit-msg":
            if len(arguments) != 1:
                raise ValueError("commit-msg requires its message file.")
            require_source(Path(arguments[0]).read_text())
        else:
            require_clean(root, staged=True)
            before = run(root, "git", "write-tree", capture=True)
            base = run(root, "git", "rev-parse", "HEAD", capture=True).strip()
            receipt_path = common_directory(root) / "workflow-approval.json"
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
                if receipt == {"base": base, "tree": before.strip(), "head": ""}:
                    return
            quality(root)
            require_clean(root, staged=True)
            if run(root, "git", "write-tree", capture=True) != before:
                raise ValueError("The staged snapshot changed during validation.")
            if run(root, "git", "rev-parse", "HEAD", capture=True).strip() != base:
                raise ValueError("Main changed during validation.")
            approve(root, base, before.strip())


def verify_distributions(directory: Path) -> None:
    archives = [*directory.glob("*.whl"), *directory.glob("*.tar.gz")]
    if not any(path.suffix == ".whl" for path in archives) or not any(
        path.name.endswith(".tar.gz") for path in archives
    ):
        raise ValueError("Both a wheel and a source distribution are required.")
    for path in archives:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as wheel:
                names = wheel.namelist()
        else:
            with tarfile.open(path) as source:
                names = source.getnames()
        if any(set(PurePosixPath(name).parts) & {".changes", ".release.toml"} for name in names):
            raise ValueError(f"Release working metadata leaked into {path.name}.")


def merge_pr(root: Path, number: int) -> None:
    data = json.loads(
        run(
            root,
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "url,title,headRefOid,baseRefName,state,isDraft",
            capture=True,
        )
    )
    if data["baseRefName"] != "main" or data["state"] != "OPEN" or data["isDraft"]:
        raise ValueError("Select an open, ready PR targeting main.")
    body = f"Source-PR: {data['url']}\nSource-Head: {data['headRefOid']}\n"
    require_source(body)
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", data["headRefOid"]) is None:
        raise ValueError("GitHub did not return a full source SHA.")
    # Required/up-to-date CI and permitted merge methods are enforced by GitHub rules.
    run(
        root,
        "gh",
        "pr",
        "merge",
        str(number),
        "--squash",
        "--match-head-commit",
        data["headRefOid"],
        "--subject",
        f"{data['title']} (#{number})",
        "--body",
        body,
    )


def configure_github(root: Path) -> None:
    metadata = json.loads(run(root, "gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef", capture=True))
    if metadata["defaultBranchRef"]["name"] != "main":
        raise ValueError("This MVP targets a main default branch.")
    repository = metadata["nameWithOwner"]
    runs = json.loads(run(root, "gh", "api", f"repos/{repository}/actions/runs?per_page=1", capture=True))
    suite_id = runs["workflow_runs"][0]["check_suite_id"]
    suite = json.loads(run(root, "gh", "api", f"repos/{repository}/check-suites/{suite_id}", capture=True))
    if suite["app"]["slug"] != "github-actions":
        raise ValueError("Could not identify the repository's GitHub Actions integration.")
    conditions = {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
    gates = {
        "name": "local-dev-mcp-main-gates",
        "target": "branch",
        "enforcement": "active",
        "conditions": conditions,
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": True,
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "Project quality", "integration_id": suite["app"]["id"]}],
                },
            },
        ],
    }
    writer = {
        "name": "local-dev-mcp-main-writer",
        "target": "branch",
        "enforcement": "active",
        "conditions": conditions,
        "rules": [{"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}],
        "bypass_actors": [{"actor_id": suite["app"]["id"], "actor_type": "Integration", "bypass_mode": "always"}],
    }
    existing = json.loads(run(root, "gh", "api", f"repos/{repository}/rulesets?per_page=100", capture=True))
    if len(existing) >= 100:
        raise ValueError("Ruleset discovery reached its bound; inspect existing rules before applying.")
    run(
        root,
        "gh",
        "repo",
        "edit",
        repository,
        "--enable-squash-merge",
        "--enable-merge-commit=false",
        "--enable-rebase-merge=false",
    )
    for rule in (gates, writer):
        matches = [item for item in existing if item["name"] == rule["name"]]
        if len(matches) > 1:
            raise ValueError("Duplicate managed ruleset names; inspect existing rules before applying.")
        endpoint = f"repos/{repository}/rulesets"
        if matches:
            endpoint += f"/{matches[0]['id']}"
        run(
            root,
            "gh",
            "api",
            endpoint,
            "--method",
            "PUT" if matches else "POST",
            "--input",
            "-",
            input_text=json.dumps(rule),
        )
    print("Applied native main gates and Actions-only writer rules; unrelated rulesets were preserved.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m local_dev_mcp.tools.workflow")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "install", "doctor", "package", "configure-github"):
        commands.add_parser(name)
    approval = commands.add_parser("approve-main")
    approval.add_argument("--squash", action="store_true")
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("directory", type=Path)
    hooks = commands.add_parser("hook")
    hooks.add_argument("name", choices=("pre-commit", "commit-msg", "pre-push", "reference-transaction"))
    hooks.add_argument("arguments", nargs="*")
    pr = commands.add_parser("merge-pr")
    pr.add_argument("number", type=int)
    args = parser.parse_args(argv)
    try:
        root = args.workspace.resolve(strict=True)
        observed = Path(run(root, "git", "rev-parse", "--show-toplevel", capture=True).strip()).resolve()
        if observed != root:
            raise ValueError("Select the Git workspace root.")
        match args.command:
            case "check":
                quality(root)
            case "install":
                print(json.dumps(install_hooks(root, run, developer_environment()), indent=2))
            case "doctor":
                result = inspect_hooks(root, run, developer_environment())
                print(json.dumps(result, indent=2))
                return 0 if result["ready"] is True else 2
            case "approve-main":
                approve_main(root, squash=args.squash)
            case "configure-github":
                configure_github(root)
            case "hook":
                hook(root, args.name, args.arguments)
            case "artifacts":
                verify_distributions(args.directory)
            case "package":
                with TemporaryDirectory(prefix="local-dev-mcp-build-") as directory:
                    run(root, "uv", "build", "--out-dir", directory)
                    verify_distributions(Path(directory))
            case "merge-pr":
                if args.number < 1:
                    raise ValueError("A positive PR number is required.")
                merge_pr(root, args.number)
            case _:
                raise ValueError("Unknown workflow command.")
    except subprocess.CalledProcessError:
        print("Workflow refused: a required command failed. Inspect its diagnostics.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.TimeoutExpired) as error:
        print(f"Workflow refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
