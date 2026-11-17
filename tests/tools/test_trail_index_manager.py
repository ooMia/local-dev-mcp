"""Trail index management only targets the immutable configured project root."""

import asyncio
from pathlib import Path

import pytest

from local_dev_mcp.settings import Policy
from local_dev_mcp.tools.indexing._manager import IndexManager, TrailProcessResult, deep_state, structural_state

pytestmark = pytest.mark.unit


def manager(tmp_path: Path, *, auto: bool = False) -> IndexManager:
    policy = Policy(tmp_path, (tmp_path,), ("trail",))
    return IndexManager(policy, {"PATH": ""}, refresh_on_write=auto)


def test_readiness_defaults_to_unavailable_without_opt_in(tmp_path: Path) -> None:
    instance = IndexManager(Policy(tmp_path, (tmp_path,), ()), {"PATH": ""})

    async def scenario() -> None:
        assert (await instance.status())["structural_state"] == "unavailable"
        assert (await instance.refresh())["outcome"] == "unavailable"
        assert (await instance.status())["refresh_performed"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text", "structural", "deep"),
    [
        ("trail build --check: STALE\ngraph check: OK\n", "fresh", "stale"),
        ("trail build --check: STALE\ngraph check: STALE\nchanged (1):\n", "stale", "stale"),
        (
            "trail build --check: STALE\n\nchanged (29):\n~ previous.py\n"
            "graph check: STALE\n\nchanged (9):\n~ module.py#handler\n"
            "added (13):\n+ new_module.py\nremoved (3):\n- old_module.py\n"
            "stale summaries (36):\n! module.py#handler\n"
            "Run `trail build` to rebuild the structure.\n",
            "stale",
            "stale",
        ),
        ("trail build --check: OK\ngraph check: OK\n", "fresh", "fresh"),
        ("deep layer: not built\ngraph check: OK\n", "fresh", "unavailable"),
        (
            "trail build --check: STALE\ngraph check: STALE\n"
            "stale summaries (36):\n  ! source.py#handler\n"
            "Run `trail build --deep` to refresh stale summaries.\n",
            "fresh",
            "stale",
        ),
        ("trail build --check: NO GRAPH\nNo code map found.\n", "missing", "unknown"),
        (
            "trail build --check: STALE\nchanged (29):\n~ file.py\n"
            "graph check: OK\nRun `trail build --deep` to refresh stale summaries.\n",
            "fresh",
            "stale",
        ),
    ],
)
def test_check_states_keep_structural_and_deep_separate(
    text: str,
    structural: str,
    deep: str,
) -> None:
    run = TrailProcessResult(1 if "STALE" in text or "NO GRAPH" in text else 0, text)
    assert structural_state(run) == structural
    assert deep_state(run) == deep


def test_refresh_only_when_structural_stale_and_rechecks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    commands: list[tuple[str, ...]] = []

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        commands.append(args)
        if len(commands) == 1:
            return TrailProcessResult(1, "trail build --check: STALE\ngraph check: STALE\nchanged (1):\n")
        if len(commands) == 2:
            return TrailProcessResult(0, "rebuilt")
        return TrailProcessResult(1, "trail build --check: STALE\ngraph check: OK\n")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        index.changed()
        result = await index.refresh()
        assert result["outcome"] == "updated"
        assert result["refresh_performed"] is True
        assert result["structural_state"] == "fresh"
        assert result["deep_state"] == "stale"
        assert result["ready_for_structural_search"] is True
        assert result["pending_changes"] is False

    asyncio.run(scenario())
    assert commands == [
        ("build", "--check", str(tmp_path)),
        ("build", str(tmp_path)),
        ("build", "--check", str(tmp_path)),
    ]


def test_already_fresh_never_runs_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    calls: list[tuple[str, ...]] = []

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        calls.append(args)
        return TrailProcessResult(0, "graph check: OK\n")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        result = await index.refresh()
        assert result["outcome"] == "already_fresh"

    asyncio.run(scenario())
    assert calls == [("build", "--check", str(tmp_path))]


def test_failed_build_stays_failed_without_raw_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    calls = 0

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        nonlocal calls
        calls += 1
        return (
            TrailProcessResult(1, "graph check: STALE\nchanged (1):\n")
            if calls == 1
            else TrailProcessResult(2, "secret output")
        )

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        result = await index.refresh()
        assert result["outcome"] == "failed"
        assert result["reason"] == "build_nonzero"
        assert "secret" not in str(result)

    asyncio.run(scenario())


def test_two_concurrent_refreshes_do_not_duplicate_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    built = False
    builds = 0

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        nonlocal built, builds
        if "--check" in args:
            return (
                TrailProcessResult(0, "graph check: OK\n")
                if built
                else TrailProcessResult(1, "graph check: STALE\nchanged (1):\n")
            )
        builds += 1
        await asyncio.sleep(0)
        built = True
        return TrailProcessResult(0, "")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        results = await asyncio.gather(index.refresh(), index.refresh())
        assert [r["outcome"] for r in results] == ["updated", "already_fresh"]
        assert builds == 1

    asyncio.run(scenario())


def test_auto_refresh_coalesces_changes_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path, auto=True)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    checks = 0
    builds = 0

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        nonlocal checks, builds
        if "--check" in args:
            checks += 1
            return TrailProcessResult(
                1 if builds == 0 else 0, "graph check: STALE\nchanged (1):\n" if builds == 0 else "graph check: OK\n"
            )
        builds += 1
        return TrailProcessResult(0, "")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        index.changed()
        index.changed()
        index.changed()
        await index.wait_for_pending_refresh()
        assert checks == 2
        assert builds == 1
        assert (await index.status())["pending_changes"] is False
        await index.close()
        index.changed()
        assert builds == 1

    asyncio.run(scenario())


def test_invalid_check_is_unknown_and_never_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    calls = 0

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        nonlocal calls
        calls += 1
        return TrailProcessResult(1, "untrusted different output")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        assert (await index.refresh())["outcome"] == "failed"

    asyncio.run(scenario())
    assert calls == 1


def test_one_full_reparse_repairs_incremental_cache_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    commands: list[tuple[str, ...]] = []
    check_count = 0

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        nonlocal check_count
        commands.append(args)
        if "--check" in args:
            check_count += 1
            if check_count < 3:
                return TrailProcessResult(1, "trail build --check: STALE\ngraph check: STALE\nchanged (1):\n")
            # Deep is still stale, but structure has been repaired.
            return TrailProcessResult(
                1,
                "trail build --check: STALE\ngraph check: STALE\nstale summaries (2):\n  ! file.py#function\n",
            )
        return TrailProcessResult(0, "build succeeded")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        result = await index.refresh()
        assert result["outcome"] == "updated"
        assert result["structural_state"] == "fresh"
        assert result["deep_state"] == "stale"
        assert result["ready_for_structural_search"] is True

    asyncio.run(scenario())
    assert commands == [
        ("build", "--check", str(tmp_path)),
        ("build", str(tmp_path)),
        ("build", "--check", str(tmp_path)),
        ("build", "--no-reuse", str(tmp_path)),
        ("build", "--check", str(tmp_path)),
    ]


def test_full_reparse_failure_reports_only_safe_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = manager(tmp_path)
    monkeypatch.setattr(index, "_binary", lambda: Path("/unused/trail"))
    commands: list[tuple[str, ...]] = []

    async def fake(binary: Path, args: tuple[str, ...]) -> TrailProcessResult:
        commands.append(args)
        if "--check" in args:
            return TrailProcessResult(1, "graph check: STALE\nchanged (1):\n")
        if "--no-reuse" in args:
            return TrailProcessResult(7, "secret diagnostic")
        return TrailProcessResult(0, "incremental built")

    monkeypatch.setattr(index, "_run", fake)

    async def scenario() -> None:
        result = await index.refresh()
        assert result["outcome"] == "failed"
        assert result["reason"] == "full_build_nonzero"
        assert result["refresh_performed"]
        assert "secret" not in str(result)

    asyncio.run(scenario())
    assert len(commands) == 4
