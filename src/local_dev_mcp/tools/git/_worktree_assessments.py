"""Explicit assessment input and policy checks for managed worktrees."""

import hashlib
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field, ValidationError

from ..release import Assessment
from ._execution import GitExecution, text_output

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

type AssessmentId = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=128)]
type AssessmentToml = Annotated[str, Field(min_length=1, max_length=16384)]
type AssessmentDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def parse(content: bytes) -> Assessment:
    if not content or len(content) > 16384 or b"\0" in content:
        raise ToolError("Provide an explicit UTF-8 assessment of at most 16 KiB without NUL.")
    try:
        record = Assessment.model_validate(tomllib.loads(content.decode("utf-8")))
    except UnicodeError, tomllib.TOMLDecodeError, ValidationError:
        raise ToolError("Assessment TOML has invalid or missing judgement fields.") from None
    if record.supersedes:
        raise ToolError("This tool records an initial judgement; superseding assessments require a separate workflow.")
    return record


def selected_path(settings: ServerSettings, target: ServerSettings, identifier: str) -> Path:
    if len(identifier) > 128 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) is None:
        raise ToolError("Use a lowercase assessment ID with digits and hyphen-separated segments.")
    relative = Path(".changes") / (identifier + ".toml")
    for selected, root in (
        (settings, settings.policy.root),
        (settings, target.policy.root),
        (target, target.policy.root),
    ):
        for path in (root / ".changes", root / relative):
            try:
                selected.policy.require_path_access(path, write=True)
            except PermissionError as error:
                raise ToolError(str(error)) from None
            if path.is_symlink():
                raise ToolError("Assessment paths must not traverse symlinks.")
        parent = root / ".changes"
        if parent.exists() and not parent.is_dir():
            raise ToolError("The assessment parent must be a directory.")
    return target.policy.root / relative


async def require_new(repo: GitExecution, head: str, identifier: str, record: Assessment) -> None:
    result = await repo.run("ls-tree", "-r", "-z", head, "--", ".changes", operation="Observe committed assessments")
    entries = list(filter(None, result.output.split(b"\0")))
    if len(entries) > 256:
        raise ToolError("Assessment count exceeds the supported limit.")
    count = 0
    for entry in entries:
        metadata, raw_path = entry.split(b"\t", 1)
        path = text_output(raw_path)
        if path == ".changes/README.md":
            continue
        count += 1
        if (
            metadata.split(b" ")[0] != b"100644"
            or re.fullmatch(r"\.changes/[a-z0-9]+(?:-[a-z0-9]+)*\.toml", path) is None
        ):
            raise ToolError("Existing assessments must be flat regular TOML files.")
        try:
            repo.settings.policy.require_path_access(repo.root / path)
        except PermissionError as error:
            raise ToolError(str(error)) from None
        if path == f".changes/{identifier}.toml":
            raise ToolError("This assessment ID already exists; it will not be overwritten.")
        previous = await repo.run("show", head + ":" + path, operation="Read committed assessment")
        try:
            existing = Assessment.model_validate(tomllib.loads(previous.output.decode("utf-8")))
        except UnicodeError, tomllib.TOMLDecodeError, ValidationError:
            raise ToolError("An existing committed assessment is invalid.") from None
        if existing == record:
            raise ToolError("An identical judgement already exists under another ID.")
    if count >= 256:
        raise ToolError("At most 256 pending assessments are supported.")
    ignored = await repo.run(
        "--no-literal-pathspecs",
        "check-ignore",
        "--no-index",
        "-q",
        "--",
        f".changes/{identifier}.toml",
        operation="Check assessment ignore rules",
        allow_failure=True,
    )
    if ignored.code != 1:
        raise ToolError("Assessment path is ignored or its ignore state could not be verified.")


def reviewed_bytes(path: Path, expected_digest: str) -> bytes:
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise ToolError("Provide the observed SHA-256 assessment digest.")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384 or path.stat().st_mode & 0o111:
        raise ToolError("The assessment must be a nonexecutable regular file of at most 16 KiB.")
    with path.open("rb") as stream:
        content = stream.read(16385)
    parse(content)
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ToolError("Assessment bytes changed since review; read them again before committing.")
    return content
