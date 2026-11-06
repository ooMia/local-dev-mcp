"""Own pytest DAG options, fixtures and node reporting; load through the repository plugin."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tests.support.dag import Graph, NodeResult, RunResult


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("local-dev-mcp DAG")
    group.addoption("--dag-node", action="append", default=None, help="Select a node and all of its prerequisites.")
    group.addoption("--dag-workers", type=int, default=4, help="Maximum concurrent nodes inside one DAG.")
    group.addoption(
        "--dag-fail-fast", action="store_true", help="Stop new node dispatch after a failure; drain active nodes."
    )
    group.addoption("--dag-report", default=None, help="Directory for one JSON report per DAG test.")


@pytest.fixture
def dag_selection(request: pytest.FixtureRequest) -> tuple[str, ...] | None:
    values: list[str] | None = request.config.getoption("dag_node")
    return tuple(values) if values is not None else None


@pytest.fixture
def dag_workers(request: pytest.FixtureRequest) -> int:
    workers: object = request.config.getoption("dag_workers")
    if not isinstance(workers, int) or workers < 1:
        raise pytest.UsageError("--dag-workers must be positive.")
    return workers


@pytest.fixture
def run_dag(
    request: pytest.FixtureRequest, dag_selection: tuple[str, ...] | None, dag_workers: int
) -> Callable[[Graph], Awaitable[RunResult]]:
    # FixtureRequest.node varies with scope in pytest's dynamic interface.
    item: object = getattr(request, "node")
    if not isinstance(item, pytest.Item):
        raise TypeError("DAG reports require a function-scoped test item.")

    async def run(graph: Graph) -> RunResult:
        try:
            plan = graph.plan(dag_selection)
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error
        print("DAG plan: " + ", ".join(plan.included))
        fail_fast: bool = request.config.getoption("dag_fail_fast") or request.config.option.maxfail == 1
        observed: dict[str, NodeResult] = {}
        report: RunResult | None = None

        def record(result: NodeResult) -> None:
            observed[result.id] = result

        try:
            report = await graph.run(
                selected=dag_selection, max_workers=dag_workers, fail_fast=fail_fast, on_result=record
            )
            return report
        finally:
            snapshot = report or RunResult(
                plan, tuple(observed[node_id] for node_id in plan.included if node_id in observed), interrupted=True
            )
            payload = json.dumps(snapshot.to_json(), ensure_ascii=False, indent=2)
            item.add_report_section("call", "DAG nodes", payload)
            for result in snapshot.results:
                item.user_properties.append((f"dag.node.{result.id}", json.dumps(result.to_json())))
            target: str | None = request.config.getoption("dag_report")
            if target is not None:
                try:
                    folder = Path(target)
                    folder.mkdir(parents=True, exist_ok=True)
                    name = hashlib.sha256(item.nodeid.encode()).hexdigest()[:16]
                    (folder / f"{name}.json").write_text(payload + "\n", encoding="utf-8")
                except (OSError, ValueError) as error:
                    if not snapshot.interrupted:
                        raise
                    item.add_report_section("call", "DAG report write error", type(error).__name__)

    return run
