"""Pinned managed-worktree context supplied internally by the Git facade."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ...settings import Policy


@dataclass(frozen=True)
class TaskWorktree:
    branch: str
    head: str
    original_branch: str
    original_head: str
    prepare: Callable[[], Awaitable[Policy]]
    verify: Callable[[], Awaitable[None]]
    backend: Literal["native", "worktrunk"] = "native"

    @property
    def identity(self) -> tuple[str, ...]:
        return (self.backend, self.branch, self.head, self.original_branch, self.original_head)
