"""Exact immutable inputs and the integration gate shared with Git MCP."""

import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from pydantic import ValidationError

from ._models import COMMIT_ID, IDENTIFIER, Assessment, WorkflowConfig, canonical_version
from ._plan import ReleasePlan, build_plan
from ._prepared import verify_release

type ReadGit = Callable[..., Awaitable[bytes]]
type AuthorizePath = Callable[[str], None]


def table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Release configuration must contain TOML tables.")
    return cast(dict[str, object], value)


def parse_toml(content: bytes) -> dict[str, object]:
    try:
        return cast(dict[str, object], tomllib.loads(content.decode("utf-8")))
    except UnicodeError, tomllib.TOMLDecodeError:
        raise ValueError("Release input must be valid UTF-8 TOML.") from None


def workflow_config(document: dict[str, object]) -> WorkflowConfig | None:
    tools = table(document.get("tool", {}))
    local = table(tools.get("local-dev-mcp", {}))
    if "release" not in local:
        return None
    try:
        return WorkflowConfig.model_validate(local["release"])
    except ValidationError:
        raise ValueError("The declared release workflow configuration is invalid.") from None


@dataclass(frozen=True)
class Snapshot:
    head: str
    config: WorkflowConfig | None
    project: dict[str, object]
    version: str | None
    records: dict[str, Assessment]
    blobs: dict[str, bytes]
    modes: dict[str, str]

    def plan(self) -> ReleasePlan:
        if self.config is None or self.version is None:
            raise ValueError("Enable the release workflow in pyproject.toml before planning a release.")
        return build_plan(self.head, self.version, self.records)


class IntegrationCheck(TypedDict):
    kind: Literal["legacy", "change", "release"]
    assessments: list[str]
    plan: ReleasePlan | None


async def read_snapshot(read_git: ReadGit, head: str, *, authorize: AuthorizePath | None = None) -> Snapshot:
    if COMMIT_ID.fullmatch(head) is None:
        raise ValueError("Use a complete observed commit SHA.")
    raw = await read_git("ls-tree", "-r", "-z", "--full-tree", head)
    modes: dict[str, str] = {}
    try:
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, _, _ = metadata.split(b" ")
            modes[path.decode("utf-8")] = mode.decode("ascii")
    except ValueError, UnicodeError:
        raise ValueError("Release Git tree entries could not be safely decoded.") from None
    blobs: dict[str, bytes] = {}

    async def load(path: str, limit: int = 1024 * 1024) -> bytes | None:
        if path not in modes:
            return None
        if modes[path] != "100644":
            raise ValueError("Release inputs must be regular non-executable Git files, never symlinks.")
        if authorize is not None:
            authorize(path)
        content = await read_git("show", f"{head}:{path}")
        if len(content) > limit or b"\0" in content:
            raise ValueError("A release input exceeds its size limit or contains NUL.")
        blobs[path] = content
        return content

    # A symlink/executable manifest cannot opt in. Do not follow or reject it
    # for a legacy workspace; a configured base still rejects this as removal.
    if modes.get("pyproject.toml") not in (None, "100644"):
        return Snapshot(head, None, {}, None, {}, blobs, modes)
    project_content = await load("pyproject.toml", 128 * 1024)
    project = parse_toml(project_content) if project_content is not None else {}
    config = workflow_config(project)
    if config is None:
        return Snapshot(head, None, project, None, {}, blobs, modes)
    await read_git("merge-base", "--is-ancestor", config.initial_base, head)
    value = table(project.get("project", {})).get("version")
    if not isinstance(value, str):
        raise ValueError("An enabled release workflow requires a static project version.")
    canonical_version(value)
    if ".changes" in modes:
        raise ValueError("The assessment directory must not be a symlink or Git file.")
    records: dict[str, Assessment] = {}
    for path in sorted(modes):
        if not path.startswith(".changes/") or path == ".changes/README.md":
            continue
        name = path.removeprefix(".changes/").removesuffix(".toml")
        if not path.endswith(".toml") or IDENTIFIER.fullmatch(name) is None:
            raise ValueError("Use flat .changes/<lowercase-id>.toml assessment files.")
        if len(records) >= 256:
            raise ValueError("The release plan exceeds 256 pending assessments.")
        content = await load(path, 16 * 1024)
        if content is None:
            raise ValueError("An assessment disappeared from the observed snapshot.")
        try:
            records[name] = Assessment.model_validate(parse_toml(content))
        except ValidationError:
            raise ValueError("A release assessment has invalid or missing judgement fields.") from None
    for path in ("CHANGELOG.md", ".release.toml", "uv.lock"):
        await load(path)
    snapshot = Snapshot(head, config, project, value, records, blobs, modes)
    snapshot.plan()
    return snapshot


async def check_integration(
    read_git: ReadGit, base: str, head: str, *, authorize: AuthorizePath | None = None
) -> IntegrationCheck:
    if any(COMMIT_ID.fullmatch(ref) is None for ref in (base, head)):
        raise ValueError("Use full observed base and source commit SHAs.")
    previous = await read_snapshot(read_git, base, authorize=authorize)
    candidate = await read_snapshot(read_git, head, authorize=authorize)
    if previous.config is None and candidate.config is None:
        return {"kind": "legacy", "assessments": [], "plan": None}
    if candidate.config is None or (previous.config is not None and previous.config != candidate.config):
        raise ValueError("Do not remove, disable or change the base release workflow during integration.")
    await read_git("merge-base", "--is-ancestor", base, head)
    if previous.version is not None and candidate.version != previous.version:
        changed = (
            (await read_git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head))
            .decode()
            .strip("\0")
            .split("\0")
        )
        consumed = verify_release(previous, candidate, [name for name in changed if name])
        return {"kind": "release", "assessments": consumed, "plan": previous.plan()}
    for path in ("CHANGELOG.md", ".release.toml"):
        if previous.blobs.get(path) != candidate.blobs.get(path):
            raise ValueError("Changelog and release receipt changes require verified release preparation.")
    for name in previous.records:
        path = f".changes/{name}.toml"
        if previous.blobs[path] != candidate.blobs.get(path):
            raise ValueError("Keep earlier assessments immutable; add an explicit superseding assessment.")
    added = sorted(candidate.records.keys() - previous.records.keys())
    if not added:
        raise ValueError("Integration requires a new assessment, including an explicit none decision.")
    return {"kind": "change", "assessments": added, "plan": candidate.plan()}
