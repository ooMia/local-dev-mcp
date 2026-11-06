import asyncio
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from types import MappingProxyType

import pytest

from ._model import Node, NodeResult, NodeSkip, Plan, RunResult, Status


async def _invoke(node: Node, on_result: Callable[[NodeResult], None]) -> NodeResult:
    started = asyncio.get_running_loop().time()
    status = Status.PASS
    reason: str | None = None
    try:
        await node.action()
    except asyncio.CancelledError:
        on_result(
            NodeResult(
                node.id,
                Status.CANCELLED,
                reason="Node execution was cancelled.",
                root_causes=(node.id,),
                started_at=started,
                finished_at=asyncio.get_running_loop().time(),
            )
        )
        raise
    except (NodeSkip, pytest.skip.Exception, pytest.xfail.Exception) as error:
        status, reason = Status.SKIP, str(error)
    except (AssertionError, pytest.fail.Exception) as error:
        status, reason = Status.FAIL, str(error)
    except Exception as error:
        status, reason = Status.ERROR, f"{type(error).__name__}: {error}"
    except BaseException as error:
        on_result(
            NodeResult(
                node.id,
                Status.ERROR,
                reason=f"{type(error).__name__}: node execution interrupted.",
                root_causes=(node.id,),
                started_at=started,
                finished_at=asyncio.get_running_loop().time(),
            )
        )
        raise
    result = NodeResult(
        node.id,
        status,
        reason=reason,
        root_causes=() if status is Status.PASS else (node.id,),
        started_at=started,
        finished_at=asyncio.get_running_loop().time(),
    )
    on_result(result)
    return result


def _unreached(node: Node, parents: Sequence[NodeResult]) -> NodeResult:
    direct = any(parent.status in {Status.FAIL, Status.ERROR, Status.SKIP, Status.CANCELLED} for parent in parents)
    causes = tuple(dict.fromkeys(cause for parent in parents for cause in parent.root_causes))
    return NodeResult(
        node.id,
        Status.BLOCKED if direct else Status.NOT_REACHED,
        reason="Required parent did not pass." if direct else "Required dependency path was not reached.",
        blocked_by=tuple(parent.id for parent in parents),
        root_causes=causes,
    )


class Graph:
    """Validate a fixed test graph and own only its in-process action tasks."""

    _nodes: Mapping[str, Node]

    def __init__(self, nodes: Sequence[Node]) -> None:
        entries: dict[str, Node] = {}
        for node in nodes:
            if not node.id or node.id in entries:
                raise ValueError(f"Empty or duplicate node ID: {node.id!r}")
            entries[node.id] = node
        if not entries:
            raise ValueError("A test graph must contain at least one node.")
        for node in entries.values():
            if len(set(node.requires)) != len(node.requires):
                raise ValueError(f"Duplicate prerequisites for node {node.id!r}.")
            missing = set(node.requires).difference(entries)
            if missing:
                raise ValueError(f"Unknown prerequisites for {node.id!r}: {sorted(missing)}")
        try:
            TopologicalSorter({node.id: node.requires for node in entries.values()}).prepare()
        except CycleError as error:
            raise ValueError("Test dependency graph contains a cycle.") from error
        self._nodes = MappingProxyType(entries)

    def plan(self, selected: Sequence[str] | None = None) -> Plan:
        requested = tuple(self._nodes) if selected is None else tuple(dict.fromkeys(selected))
        if not requested:
            raise ValueError("Select at least one test node.")
        missing = set(requested).difference(self._nodes)
        if missing:
            raise ValueError(f"Unknown selected nodes: {sorted(missing)}")
        included: set[str] = set()
        pending = list(requested)
        while pending:
            node_id = pending.pop()
            if node_id not in included:
                included.add(node_id)
                pending.extend(self._nodes[node_id].requires)
        sorter = TopologicalSorter(
            {node_id: node.requires for node_id, node in self._nodes.items() if node_id in included}
        )
        ordered = tuple(sorter.static_order())
        return Plan(requested, ordered, tuple((node_id, self._nodes[node_id].requires) for node_id in ordered))

    async def run(
        self,
        selected: Sequence[str] | None = None,
        *,
        max_workers: int = 4,
        fail_fast: bool = False,
        on_result: Callable[[NodeResult], None] | None = None,
    ) -> RunResult:
        if max_workers < 1:
            raise ValueError("max_workers must be positive.")
        plan = self.plan(selected)
        sorter = TopologicalSorter({node_id: self._nodes[node_id].requires for node_id in plan.included})
        sorter.prepare()
        ready: deque[str] = deque()
        active: dict[asyncio.Task[NodeResult], str] = {}
        results: dict[str, NodeResult] = {}
        failures: list[str] = []
        stopped = False

        def record(result: NodeResult) -> None:
            results[result.id] = result
            if on_result is not None:
                on_result(result)

        try:
            while sorter.is_active():
                ready.extend(sorter.get_ready())
                finalized = False
                for _ in range(len(ready)):
                    node_id = ready.popleft()
                    node = self._nodes[node_id]
                    parents = [results[parent] for parent in node.requires if results[parent].status is not Status.PASS]
                    if parents:
                        record(_unreached(node, parents))
                    elif stopped:
                        record(
                            NodeResult(
                                node_id,
                                Status.NOT_RUN,
                                reason="Global fail-fast stopped new dispatch.",
                                root_causes=tuple(failures),
                            )
                        )
                    elif len(active) < max_workers:
                        task = asyncio.create_task(_invoke(node, record), name=f"test-dag:{node_id}")
                        active[task] = node_id
                        continue
                    else:
                        ready.append(node_id)
                        continue
                    sorter.done(node_id)
                    finalized = True
                if finalized:
                    continue
                if active:
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    # Preserve the plan's report order even when completions arrive together.
                    for task in sorted(done, key=lambda item: plan.included.index(active[item])):
                        node_id = active[task]
                        result = task.result()
                        active.pop(task)
                        sorter.done(node_id)
                        if result.status in {Status.FAIL, Status.ERROR}:
                            failures.append(node_id)
                            stopped = stopped or fail_fast
            return RunResult(plan, tuple(results[node_id] for node_id in plan.included))
        finally:
            # Cancellation belongs to the caller; drain action cleanup before propagating it.
            for task in active:
                if not task.done():
                    task.cancel()
            cancelled = False
            if active:
                drain = asyncio.gather(*active, return_exceptions=True)
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        cancelled = True
                for task, node_id in active.items():
                    if task.cancelled() and node_id not in results:
                        record(
                            NodeResult(
                                node_id,
                                Status.CANCELLED,
                                reason="Dispatched node was cancelled before execution began.",
                                root_causes=(node_id,),
                                finished_at=asyncio.get_running_loop().time(),
                            )
                        )
            # A cancelled run still reports every included node, in dependency order.
            for node_id in plan.included:
                if node_id in results:
                    continue
                node = self._nodes[node_id]
                parents = [results[parent] for parent in node.requires if results[parent].status is not Status.PASS]
                if parents:
                    record(_unreached(node, parents))
                else:
                    record(NodeResult(node_id, Status.NOT_RUN, reason="Execution was interrupted before dispatch."))
            if cancelled:
                raise asyncio.CancelledError
