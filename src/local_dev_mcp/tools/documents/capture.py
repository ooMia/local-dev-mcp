"""Capture registered Working Tree input with a public workspace snapshot reader."""

from fastmcp.exceptions import ToolError

from ...settings import Policy
from ..workspace import SnapshotChanged, SnapshotObservation, SnapshotReader
from ..workspace.errors import WorkspaceError
from .diagnostic import DiagnosticResult, diagnose_captured
from .registry import GUIDE_PATH, REPOSITORY_ID, parse_guide


def _read(reader: SnapshotReader, relative: str) -> SnapshotObservation:
    """Convert workspace capture failures into bounded diagnostic error codes."""
    try:
        return reader.read(relative)
    except SnapshotChanged:
        raise ToolError("SNAPSHOT_CHANGED: A managed file changed during capture.") from None
    except WorkspaceError:
        raise ToolError("DOCUMENT_READ_FAILED: Managed file access was denied or unavailable.") from None


def diagnose_repository(policy: Policy, repository_id: str) -> DiagnosticResult:
    """The initial registered repository is the selected workspace, not a caller path."""
    if repository_id != REPOSITORY_ID or policy.root.name != REPOSITORY_ID:
        raise ToolError("REPOSITORY_NOT_REGISTERED: Select the registered local-dev-mcp workspace.")
    try:
        policy.require_project_access()
    except PermissionError:
        raise ToolError("REPOSITORY_ACCESS_DENIED: The configured workspace is not allowed.") from None
    reader = SnapshotReader(policy)
    guide = _read(reader, GUIDE_PATH)
    try:
        registry = parse_guide(guide.content.decode("utf-8"))
    except UnicodeError, ValueError:
        raise ToolError("REGISTRY_INVALID: Guide registration is missing or malformed.") from None

    observed: dict[str, SnapshotObservation] = {GUIDE_PATH: guide}
    sources: dict[str, str] = {}
    for entry in registry.documents:
        capture = _read(reader, entry.path)
        observed[entry.path] = capture
        try:
            sources[entry.path] = capture.content.decode("utf-8")
        except UnicodeError:
            raise ToolError("DOCUMENT_READ_FAILED: Managed document is not UTF-8.") from None

    result = diagnose_captured(registry, sources, repository_id=repository_id)
    for original in observed.values():
        try:
            reader.verify(original)
        except SnapshotChanged:
            raise ToolError("SNAPSHOT_CHANGED: Managed inputs changed during diagnosis.") from None
    return result
