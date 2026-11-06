"""Bounded, no-follow raw-file observations for cooperating workspace features."""

import os
from dataclasses import dataclass

from ...settings import Policy
from ._access import Workspace
from ._reads import identity
from ._response import FILE_BYTES
from .errors import WorkspaceChanged, WorkspaceError

type FileStamp = tuple[int, int, int, int, int]


class SnapshotChanged(Exception):
    """A captured file cannot be confirmed as the same observable contents."""


@dataclass(frozen=True, slots=True)
class SnapshotObservation:
    path: str
    content: bytes
    stamp: FileStamp


@dataclass(frozen=True, slots=True)
class SnapshotReader:
    policy: Policy

    def read(self, relative: str) -> SnapshotObservation:
        """Read one complete regular file using existing workspace path guards."""
        scoped = Workspace(self.policy).path(relative)
        try:
            descriptor, before = scoped.open_binary()
            with os.fdopen(descriptor, "rb") as stream:
                if before.st_size > FILE_BYTES:
                    raise WorkspaceError.size()
                content = stream.read(FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if len(content) > FILE_BYTES:
                raise WorkspaceError.size()
            if b"\0" in content:
                raise WorkspaceError.text()
            current = scoped.checked().stat()
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise SnapshotChanged()
            if len(content) != after.st_size:
                raise SnapshotChanged()
            return SnapshotObservation(scoped.relative, content, identity(after))
        except WorkspaceChanged:
            raise SnapshotChanged() from None
        except OSError:
            raise WorkspaceError.access() from None

    def verify(self, observation: SnapshotObservation) -> None:
        """Fail if the selected file cannot be re-observed unchanged.

        This is optimistic change detection, not an atomic filesystem snapshot.
        """
        try:
            current = self.read(observation.path)
        except WorkspaceError, SnapshotChanged:
            raise SnapshotChanged() from None
        if current != observation:
            raise SnapshotChanged()
