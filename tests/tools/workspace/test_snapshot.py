"""The public snapshot reader preserves workspace guards and detects stale files."""

from pathlib import Path

import pytest

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.workspace import SnapshotChanged, SnapshotReader
from local_dev_mcp.tools.workspace._access import ScopedPath
from local_dev_mcp.tools.workspace.errors import WorkspaceError

pytestmark = pytest.mark.integration


def test_snapshot_reader_preserves_raw_content_and_rechecks(tmp_path: Path) -> None:
    path = tmp_path / "article.md"
    path.write_bytes(b"first\r\nsecond\n")
    reader = SnapshotReader(Policy(tmp_path, (tmp_path,), ()))
    observed = reader.read("article.md")
    assert observed.path == "article.md"
    assert observed.content == b"first\r\nsecond\n"
    reader.verify(observed)
    path.write_bytes(b"first\nsecond\n")
    with pytest.raises(SnapshotChanged):
        reader.verify(observed)


def test_snapshot_reader_enforces_access_and_symlink_denial(tmp_path: Path) -> None:
    data = tmp_path / "source.md"
    data.write_text("data")
    other = tmp_path / "secrets.md"
    other.write_text("do not disclose")
    reader = SnapshotReader(Policy(tmp_path, (tmp_path,), (), denied_paths=(other,)))
    with pytest.raises(WorkspaceError):
        reader.read("secrets.md")
    (tmp_path / "link.md").symlink_to(data)
    with pytest.raises(WorkspaceError):
        reader.read("link.md")


def test_snapshot_reader_rejects_non_text_and_lost_file(tmp_path: Path) -> None:
    path = tmp_path / "article.md"
    path.write_bytes(b"not\0text")
    reader = SnapshotReader(Policy(tmp_path, (tmp_path,), ()))
    with pytest.raises(WorkspaceError):
        reader.read("article.md")
    path.write_bytes(b"valid\n")
    observation = reader.read("article.md")
    path.unlink()
    with pytest.raises(SnapshotChanged):
        reader.verify(observation)


def test_detected_change_during_initial_open_has_distinct_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "article.md"
    path.write_text("# content\\n")
    reader = SnapshotReader(Policy(tmp_path, (tmp_path,), ()))

    def changed_before_open(self: ScopedPath) -> tuple[int, object]:
        raise WorkspaceError.changed()

    monkeypatch.setattr(ScopedPath, "open_binary", changed_before_open)
    with pytest.raises(SnapshotChanged):
        reader.read("article.md")
