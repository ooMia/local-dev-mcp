"""Exercise the pytest DAG adapter through its registered fixture and reports."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from tests.support.dag import Graph, Node, RunResult


@pytest.mark.integration
@pytest.mark.parametrize("writable_report", [True, False])
def test_pytest_adapter_preserves_partial_snapshot_and_cancellation(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_dag: Callable[[Graph], Awaitable[RunResult]],
    writable_report: bool,
) -> None:
    target = tmp_path / "reports"
    if not writable_report:
        target.write_text("owned obstruction", encoding="utf-8")
    monkeypatch.setattr(request.config.option, "dag_report", str(target))

    async def scenario() -> None:
        active_started = asyncio.Event()
        cleaned = False

        async def passed() -> None:
            return None

        async def active() -> None:
            nonlocal cleaned
            active_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned = True

        graph = Graph((Node("passed", passed), Node("active", active, ("passed",))))

        async def execute() -> RunResult:
            return await run_dag(graph)

        task = asyncio.create_task(execute())
        async with asyncio.timeout(2):
            await active_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert cleaned
        if writable_report:
            files = tuple(target.glob("*.json"))
            assert len(files) == 1
            payload = TypeAdapter(dict[str, object]).validate_json(files[0].read_text(encoding="utf-8"))
            assert payload["success"] is False and payload["interrupted"] is True
            assert payload["included"] == ["passed", "active"]
            assert payload["unfinished"] == ["active"]
            nodes = TypeAdapter(list[dict[str, object]]).validate_python(payload["nodes"])
            assert [node["status"] for node in nodes] == ["pass", "cancelled"]
        else:
            assert target.read_text(encoding="utf-8") == "owned obstruction"
        item: object = getattr(request, "node")
        assert isinstance(item, pytest.Item)
        properties = dict(item.user_properties)
        encoded = properties["dag.node.active"]
        assert isinstance(encoded, str)
        active_property = TypeAdapter(dict[str, object]).validate_json(encoded)
        assert active_property["status"] == "cancelled"

    asyncio.run(scenario())
