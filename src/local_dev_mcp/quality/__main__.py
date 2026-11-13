"""Run a shared quality profile directly, for example from a Worktrunk Hook.

MCP integrations instead submit the selected registered recipes through TaskManager.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from ..settings import Policy
from .manifest import load_quality_manifest


def _git_snapshot(root: Path) -> tuple[str, bytes]:
    head = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=15,
        )
        .stdout.decode("ascii")
        .strip()
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    return head, status


def run_profile(root: Path, profile_name: str) -> int:
    """Fail closed on changed source, invalid profile, or failed checks."""
    policy = Policy.load(root)
    policy.require_project_access()
    manifest = load_quality_manifest(policy.root)
    profile, recipes = manifest.select(profile_name, policy.jobs)
    source = policy.root / "config.toml"
    config_digest = hashlib.sha256(source.read_bytes()).digest()
    head_before, status_before = _git_snapshot(policy.root)
    if profile.require_clean and status_before:
        raise ValueError("Source worktree is not clean; quality profile was not started.")
    for recipe in recipes:
        policy.require_command(recipe.permission)
        directory = (policy.root / recipe.cwd).resolve()
        if not directory.is_relative_to(policy.root):
            raise ValueError("Quality recipe directory leaves the project.")
        policy.require_path_access(directory)
        if not directory.is_dir():
            raise ValueError("Quality recipe directory is unavailable.")
        args = (*recipe.argv, *(recipe.default_targets if recipe.kind == "test" else ()))
        print(f"quality: running {recipe.name}", flush=True)
        try:
            result = subprocess.run(args, cwd=directory, timeout=recipe.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            print(f"quality: timeout {recipe.name}", file=sys.stderr)
            return 1
        except OSError:
            print(f"quality: cannot start {recipe.name}", file=sys.stderr)
            return 1
        if result.returncode != 0:
            print(f"quality: failed {recipe.name} (exit={result.returncode})", file=sys.stderr)
            return 1
    head_after, status_after = _git_snapshot(policy.root)
    if head_after != head_before or (profile.require_clean and status_after):
        raise ValueError("Source Git state changed while checks ran.")
    if load_quality_manifest(policy.root).digest != manifest.digest:
        raise ValueError("Quality manifest changed while checks ran.")
    if hashlib.sha256(source.read_bytes()).digest() != config_digest:
        raise ValueError("Task definitions changed while checks ran.")
    print(f"quality: PASS {profile_name} ({len(recipes)} checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run project-owned quality checks")
    parser.add_argument("--profile", required=True)
    arguments = parser.parse_args()
    try:
        return run_profile(Path.cwd(), arguments.profile)
    except (ValueError, OSError, PermissionError, subprocess.SubprocessError) as error:
        print(f"quality: BLOCKED ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
