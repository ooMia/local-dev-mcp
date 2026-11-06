"""Immutable workspace paths with repeated policy and no-follow checks."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ...settings import Policy
from ...settings import protected_name as protected
from .errors import WorkspaceError


@dataclass(frozen=True)
class Workspace:
    policy: Policy

    def require_access(self) -> None:
        try:
            self.policy.require_project_access()
        except PermissionError:
            raise WorkspaceError.access() from None
        try:
            if self.policy.root.is_symlink() or self.policy.root.resolve(strict=True) != self.policy.root:
                raise WorkspaceError.symlink()
            if not self.policy.root.is_dir():
                raise WorkspaceError.access()
        except OSError, ValueError:
            raise WorkspaceError.access() from None

    def path(self, value: str) -> ScopedPath:
        self.require_access()
        if not value or "\0" in value or Path(value).is_absolute():
            raise WorkspaceError.path()
        parts: list[str] = []
        # Inspect before normalizing '..': link/../file must not hide a link.
        for part in Path(value).parts:
            if part == ".":
                continue
            if part == "..":
                if not parts:
                    raise WorkspaceError.path()
                parts.pop()
                continue
            if protected(part):
                raise WorkspaceError.protected()
            current = self.policy.root.joinpath(*parts, part)
            try:
                if current.is_symlink():
                    raise WorkspaceError.symlink()
            except OSError:
                raise WorkspaceError.access() from None
            parts.append(part)
        return ScopedPath(self, "/".join(parts) or ".")


@dataclass(frozen=True)
class ScopedPath:
    workspace: Workspace
    relative: str

    def __post_init__(self) -> None:
        path = Path(self.relative)
        if not self.relative or path.is_absolute() or "\0" in self.relative or ".." in path.parts:
            raise WorkspaceError.path()
        if any(protected(part) for part in path.parts):
            raise WorkspaceError.protected()

    def join(self, name: str) -> ScopedPath:
        if Path(name).is_absolute():
            raise WorkspaceError.path()
        return self.workspace.path(str(Path(self.relative) / name))

    def checked(self, *, final_link: bool = False, policy_metadata: bool = False) -> Path:
        self.workspace.require_access()
        current = self.workspace.policy.root
        parts = Path(self.relative).parts
        try:
            if not (policy_metadata and Path(self.relative).name == ".gitignore"):
                self.workspace.policy.require_path_access(self.workspace.policy.root / self.relative, traverse=True)
            for index, part in enumerate(parts):
                if part in ("..", "") or protected(part):
                    raise WorkspaceError.path()
                current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) and not (final_link and index == len(parts) - 1):
                    raise WorkspaceError.symlink()
                # Use the actual directory entry spelling on case-insensitive hosts.
                if not stat.S_ISLNK(info.st_mode) and protected(current.resolve(strict=True).name):
                    raise WorkspaceError.protected()
            if not current.is_relative_to(self.workspace.policy.root):
                raise WorkspaceError.path()
            return current
        except OSError, ValueError:
            raise WorkspaceError.access() from None

    def require_workspace(self, workspace: Workspace) -> None:
        if self.workspace != workspace:
            raise WorkspaceError.path()

    def open_binary(self, *, policy_metadata: bool = False) -> tuple[int, os.stat_result]:
        path = self.checked(policy_metadata=policy_metadata)
        try:
            if not (policy_metadata and path.name == ".gitignore"):
                self.workspace.policy.require_path_access(path)
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceError.access()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise WorkspaceError.changed()
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor, opened
        except OSError:
            raise WorkspaceError.access() from None
