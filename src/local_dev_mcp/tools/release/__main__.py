"""Developer release commands; no MCP startup, dotenv or credentials loading."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ._git import LocalGit
from ._prepare import prepare_release
from ._snapshots import check_integration, read_snapshot


class Arguments(argparse.Namespace):
    workspace: Path
    command: str
    base: str
    head: str | None
    expected_head: str
    expected_plan: str


async def _run(args: Arguments) -> object:
    repository = LocalGit(args.workspace.resolve(strict=True))
    current = await repository.head()
    head = args.head if args.head is not None else current
    if args.command == "check":
        return await check_integration(repository.read, args.base, head)
    if args.command == "prepare":
        return await prepare_release(repository.root, args.expected_head, args.expected_plan)
    return (await read_snapshot(repository.read, head)).plan()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m local_dev_mcp.tools.release")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate exact base and source integration snapshots")
    check.add_argument("--base", required=True)
    check.add_argument("--head")
    plan = commands.add_parser("plan", help="Show cumulative impact, causes and proposed version")
    plan.add_argument("--head")
    prepare = commands.add_parser("prepare", help="Prepare version, lock, notes and receipt without committing")
    prepare.add_argument("--expected-head", required=True)
    prepare.add_argument("--expected-plan", required=True)
    prepare.set_defaults(head=None)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        result = asyncio.run(_run(args))
    except ValueError as error:
        print(f"Release validation failed: {error} Inspect Git state before retrying.", file=sys.stderr)
        return 2
    except OSError:
        print("Release workspace I/O failed. Inspect the selected root and Git state before retrying.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
