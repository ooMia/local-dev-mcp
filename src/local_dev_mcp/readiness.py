"""Immutable, safe startup outcomes shared by diagnostics and tool dispatch."""

from dataclasses import dataclass
from typing import Literal, TypedDict

from .errors import CliError, ExitCode


class ReadinessIssue(TypedDict):
    operation: str
    target: str
    reason: str
    action: str
    restart_required: Literal[True]


@dataclass(frozen=True)
class PreparationIssue:
    code: ExitCode
    operation: str
    target: str
    reason: str
    action: str

    @classmethod
    def from_error(cls, error: CliError) -> PreparationIssue:
        return cls(error.code, error.operation, error.target, error.reason, error.action)

    def diagnostic(self) -> str:
        return CliError(self.code, self.operation, self.target, self.reason, self.action).diagnostic() + (
            "\nStartup settings are fixed; restart after correcting the preparation issue."
        )

    def public(self) -> ReadinessIssue:
        return {
            "operation": self.operation,
            "target": self.target,
            "reason": self.reason,
            "action": self.action,
            "restart_required": True,
        }


@dataclass(frozen=True)
class ToolReadiness:
    name: str
    issue: PreparationIssue | None
