from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"
    BLOCKED = "blocked"
    NOT_REACHED = "not_reached"
    NOT_RUN = "not_run"
    CANCELLED = "cancelled"


class NodeSkip(Exception):
    """An explicitly unavailable prerequisite, rather than a successful check."""


class NodeSetupError(Exception):
    """Node preparation failed before its check could be attempted."""


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    action: Callable[[], Awaitable[None]]
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    requested: tuple[str, ...]
    included: tuple[str, ...]
    requires: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class NodeResult:
    id: str
    status: Status
    reason: str | None = None
    blocked_by: tuple[str, ...] = ()
    root_causes: tuple[str, ...] = ()
    started_at: float | None = None
    finished_at: float | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "status": self.status.value,
            "reason": self.reason,
            "blocked_by": list(self.blocked_by),
            "root_causes": list(self.root_causes),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    plan: Plan
    results: tuple[NodeResult, ...]
    interrupted: bool = False

    @property
    def unfinished(self) -> tuple[str, ...]:
        observed = {result.id: result for result in self.results}
        return tuple(
            node_id
            for node_id in self.plan.included
            if node_id not in observed or observed[node_id].status in {Status.CANCELLED, Status.NOT_RUN}
        )

    @property
    def success(self) -> bool:
        return (
            not self.interrupted
            and bool(self.plan.included)
            and tuple(result.id for result in self.results) == self.plan.included
            and all(result.status is Status.PASS for result in self.results)
        )

    def result_for(self, node_id: str) -> NodeResult:
        for result in self.results:
            if result.id == node_id:
                return result
        raise KeyError(node_id)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "requested": list(self.plan.requested),
            "included": list(self.plan.included),
            "requires": {node_id: list(parents) for node_id, parents in self.plan.requires},
            "success": self.success,
            "interrupted": self.interrupted,
            "unfinished": list(self.unfinished),
            "nodes": [result.to_json() for result in self.results],
        }
