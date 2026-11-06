import asyncio
import json

import pytest

from tests.support.dag import Graph, Node, NodeResult, NodeSetupError, NodeSkip, Plan, RunResult, Status


async def _unused() -> None:
    raise AssertionError("Planning must not execute an action.")


@pytest.mark.unit
@pytest.mark.parametrize(
    "nodes",
    [
        (),
        (Node("", _unused),),
        (Node("A", _unused), Node("A", _unused)),
        (Node("A", _unused, ("missing",)),),
        (Node("A", _unused, ("A",)),),
        (Node("A", _unused, ("B",)), Node("B", _unused, ("A",))),
        (Node("A", _unused), Node("B", _unused, ("A", "A"))),
    ],
)
def test_invalid_graph_is_rejected_before_actions(nodes: tuple[Node, ...]) -> None:
    with pytest.raises(ValueError):
        Graph(nodes)


@pytest.mark.unit
def test_selection_includes_transitive_ancestors_and_excludes_other_branches() -> None:
    graph = Graph(
        (
            Node("join", _unused, ("left", "right")),
            Node("left", _unused, ("root",)),
            Node("unrelated", _unused),
            Node("root", _unused),
            Node("right", _unused, ("root",)),
        )
    )
    plan = graph.plan(("join", "join"))
    assert plan.requested == ("join",)
    assert dict(plan.requires)["join"] == ("left", "right")
    assert set(plan.included) == {"root", "left", "right", "join"}
    assert plan.included.index("root") < plan.included.index("left") < plan.included.index("join")
    assert plan.included.index("root") < plan.included.index("right") < plan.included.index("join")
    with pytest.raises(ValueError, match="Unknown selected"):
        graph.plan(("missing",))
    with pytest.raises(ValueError, match="at least one"):
        graph.plan(())


@pytest.mark.unit
def test_parent_pass_does_not_claim_an_incomplete_plan_succeeded() -> None:
    plan = Plan(("child",), ("parent", "child"), (("parent", ()), ("child", ("parent",))))
    report = RunResult(plan, (NodeResult("parent", Status.PASS),))
    assert not report.success
    assert report.result_for("parent").status is Status.PASS
    with pytest.raises(KeyError):
        report.result_for("child")
    payload = report.to_json()
    assert payload["requires"] == {"parent": [], "child": ["parent"]}
    assert payload["included"] == ["parent", "child"]


@pytest.mark.unit
def test_interrupted_snapshot_is_not_successful_even_when_observed_nodes_passed() -> None:
    plan = Plan(("parent",), ("parent",), (("parent", ()),))
    report = RunResult(plan, (NodeResult("parent", Status.PASS),), interrupted=True)

    assert not report.success
    assert report.to_json()["interrupted"] is True
    assert report.unfinished == ()


@pytest.mark.integration
def test_ready_nodes_overlap_and_join_waits_for_all_parents_once() -> None:
    async def scenario() -> None:
        started = 0
        join_calls = 0
        both_started = asyncio.Event()
        completed: set[str] = set()

        async def root() -> None:
            completed.add("root")

        async def branch(name: str) -> None:
            nonlocal started
            assert "root" in completed
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            completed.add(name)

        async def left() -> None:
            await branch("left")

        async def right() -> None:
            await branch("right")

        async def join() -> None:
            nonlocal join_calls
            assert {"left", "right"} <= completed
            join_calls += 1

        graph = Graph(
            (
                Node("join", join, ("left", "right")),
                Node("root", root),
                Node("left", left, ("root",)),
                Node("right", right, ("root",)),
            )
        )
        async with asyncio.timeout(2):
            result = await graph.run(("join",), max_workers=2)
        assert result.success and join_calls == 1
        left_result, right_result = result.result_for("left"), result.result_for("right")
        assert left_result.started_at is not None and left_result.finished_at is not None
        assert right_result.started_at is not None and right_result.finished_at is not None
        assert max(left_result.started_at, right_result.started_at) < min(
            left_result.finished_at, right_result.finished_at
        )
        assert json.loads(json.dumps(result.to_json()))["success"] is True

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure", ["assertion", "error", "setup", "skip", "pytest_fail", "pytest_skip", "pytest_xfail"]
)
def test_unsuccessful_parent_prunes_its_path_and_keeps_sibling_branch(failure: str) -> None:
    async def scenario() -> None:
        visited: list[str] = []

        async def parent() -> None:
            if failure == "assertion":
                raise AssertionError("contract violation")
            if failure == "error":
                raise RuntimeError("execution error")
            if failure == "setup":
                raise NodeSetupError("preparation error")
            if failure == "pytest_fail":
                pytest.fail("pytest contract violation")
            if failure == "pytest_skip":
                pytest.skip("pytest prerequisite unavailable")
            if failure == "pytest_xfail":
                pytest.xfail("pytest known failure")
            raise NodeSkip("unavailable")

        async def independent() -> None:
            visited.append("independent")

        async def followup() -> None:
            visited.append("followup")

        graph = Graph(
            (
                Node("parent", parent),
                Node("blocked", _unused, ("parent",)),
                Node("deeper", _unused, ("blocked",)),
                Node("independent", independent),
                Node("followup", followup, ("independent",)),
                Node("join", _unused, ("blocked", "followup")),
            )
        )
        result = await graph.run(max_workers=2)
        expected = {
            "assertion": Status.FAIL,
            "error": Status.ERROR,
            "setup": Status.ERROR,
            "skip": Status.SKIP,
            "pytest_fail": Status.FAIL,
            "pytest_skip": Status.SKIP,
            "pytest_xfail": Status.SKIP,
        }
        assert result.result_for("parent").status is expected[failure]
        assert result.result_for("blocked").status is Status.BLOCKED
        assert result.result_for("deeper").status is Status.NOT_REACHED
        assert result.result_for("join").status is Status.NOT_REACHED
        assert result.result_for("deeper").blocked_by == ("blocked",)
        assert result.result_for("deeper").root_causes == ("parent",)
        assert visited == ["independent", "followup"]
        assert not result.success
        assert result.result_for("independent").status is Status.PASS
        assert result.result_for("join").started_at is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_worker_limit_bounds_active_actions() -> None:
    async def scenario() -> None:
        active = peak = 0

        async def action() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
            finally:
                active -= 1

        graph = Graph(tuple(Node(str(index), action) for index in range(7)))
        for workers in (1, 2, 3):
            peak = 0
            result = await graph.run(max_workers=workers)
            assert result.success and peak == workers and active == 0
        with pytest.raises(ValueError, match="positive"):
            await graph.run(max_workers=0)

    asyncio.run(scenario())


@pytest.mark.integration
def test_fail_fast_stops_new_dispatch_but_drains_active_cleanup() -> None:
    async def scenario() -> None:
        finished: list[str] = []

        async def failed() -> None:
            raise AssertionError("stop dispatch")

        async def active() -> None:
            try:
                await asyncio.sleep(0.01)
            finally:
                finished.append("cleanup")

        graph = Graph(
            (
                Node("failed", failed),
                Node("active", active),
                Node("pending", _unused),
                Node("child", _unused, ("failed",)),
                Node("active_child", _unused, ("active",)),
            )
        )
        result = await graph.run(max_workers=2, fail_fast=True)
        assert result.result_for("failed").status is Status.FAIL
        assert result.result_for("active").status is Status.PASS
        assert result.result_for("child").status is Status.BLOCKED
        assert result.result_for("pending").status is Status.NOT_RUN
        assert result.result_for("active_child").status is Status.NOT_RUN
        assert finished == ["cleanup"] and not result.success

    asyncio.run(scenario())


@pytest.mark.integration
def test_cancellation_is_preserved_and_all_actions_finish_cleanup() -> None:
    async def scenario() -> None:
        started = 0
        ready = asyncio.Event()
        cleaned: list[str] = []

        async def action() -> None:
            nonlocal started
            started += 1
            if started == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append("clean")

        graph = Graph((Node("left", action), Node("right", action), Node("pending", _unused)))
        task = asyncio.create_task(graph.run(max_workers=2))
        async with asyncio.timeout(2):
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cleaned == ["clean", "clean"]
        assert not [running for running in asyncio.all_tasks() if running.get_name().startswith("test-dag:")]

    asyncio.run(scenario())


@pytest.mark.integration
def test_repeated_cancellation_does_not_abandon_action_cleanup() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleaned = False

        async def action() -> None:
            nonlocal cleaned
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
                cleaned = True

        task = asyncio.create_task(Graph((Node("node", action),)).run())
        async with asyncio.timeout(2):
            await started.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cleaned

    asyncio.run(scenario())


@pytest.mark.integration
def test_interruption_sink_preserves_completed_results_and_classifies_unfinished_nodes() -> None:
    async def scenario() -> None:
        active_started = asyncio.Event()
        cleaned = False
        observed: dict[str, NodeResult] = {}
        emitted: list[str] = []
        interruption_requested = False
        task: asyncio.Task[RunResult] | None = None

        async def passed() -> None:
            return None

        async def failed() -> None:
            await active_started.wait()
            raise AssertionError("completed contract failure")

        async def error() -> None:
            await active_started.wait()
            raise RuntimeError("completed execution error")

        async def active() -> None:
            nonlocal cleaned
            active_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned = True

        def record(result: NodeResult) -> None:
            nonlocal interruption_requested
            observed[result.id] = result
            emitted.append(result.id)
            if {"failed", "error"} <= observed.keys() and not interruption_requested:
                interruption_requested = True
                assert task is not None
                task.cancel()

        graph = Graph(
            (
                Node("passed", passed),
                Node("failed", failed, ("passed",)),
                Node("error", error, ("passed",)),
                Node("active", active, ("passed",)),
                Node("pending", _unused, ("passed",)),
                Node("blocked", _unused, ("failed",)),
                Node("deeper", _unused, ("blocked",)),
                Node("cancelled_child", _unused, ("active",)),
            )
        )
        task = asyncio.create_task(graph.run(max_workers=3, on_result=record))
        async with asyncio.timeout(2):
            with pytest.raises(asyncio.CancelledError):
                await task

        assert cleaned
        assert len(emitted) == len(set(emitted)) == len(graph.plan().included)
        assert observed["passed"].status is Status.PASS
        assert observed["failed"].status is Status.FAIL
        assert observed["error"].status is Status.ERROR
        assert observed["active"].status is Status.CANCELLED
        assert observed["active"].started_at is not None and observed["active"].finished_at is not None
        assert observed["pending"].status is Status.NOT_RUN
        assert observed["pending"].started_at is None
        assert observed["blocked"].status is Status.BLOCKED
        assert observed["blocked"].root_causes == ("failed",)
        assert observed["deeper"].status is Status.NOT_REACHED
        assert observed["cancelled_child"].status is Status.BLOCKED
        assert observed["cancelled_child"].root_causes == ("active",)
        plan = graph.plan()
        snapshot = RunResult(plan, tuple(observed[node_id] for node_id in plan.included), interrupted=True)
        assert not snapshot.success
        assert set(snapshot.unfinished) == {"active", "pending"}
        assert snapshot.to_json()["interrupted"] is True

    asyncio.run(scenario())


@pytest.mark.integration
def test_base_exception_keeps_executed_error_distinct_from_undispatched_nodes() -> None:
    class NodeAbort(BaseException):
        pass

    async def scenario() -> None:
        observed: dict[str, NodeResult] = {}
        abort = NodeAbort("stop the run")

        async def aborted() -> None:
            raise abort

        def record(result: NodeResult) -> None:
            observed[result.id] = result

        graph = Graph((Node("aborted", aborted), Node("pending", _unused)))
        with pytest.raises(NodeAbort) as caught:
            await graph.run(max_workers=1, on_result=record)

        assert caught.value is abort
        assert observed["aborted"].status is Status.ERROR
        assert observed["aborted"].started_at is not None
        assert observed["aborted"].reason == "NodeAbort: node execution interrupted."
        assert observed["pending"].status is Status.NOT_RUN
        assert observed["pending"].started_at is None

    asyncio.run(scenario())
