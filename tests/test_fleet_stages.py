"""fleet: every stage of a cycle ends its tasks and children before the cycle returns."""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from test_fleet_fakes import Fleet, Sink, linux, writer

from answerback_bridge.fleet import loop, probes

WORKERS = ("worker-a", "worker-b")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class WatchingSink(Sink):
    """Records, at the moment of each publication, which children and tasks still run."""

    def __init__(self, pids: list[int]) -> None:
        super().__init__()
        self.pids = pids
        self.alive: list[list[int]] = []
        self.tasks: list[int] = []

    def publish(self, data: bytes) -> None:
        self.alive.append([pid for pid in self.pids if _alive(pid)])
        current = asyncio.current_task()
        self.tasks.append(len([t for t in asyncio.all_tasks() if t is not current]))
        super().publish(data)


def _runner(
    fleet: Fleet, tmp_path: Path, *, sleeper: str | None, broken: str | None
) -> tuple[Any, list[int]]:
    """A runner whose ``sleeper`` listing runs a real ``sleep`` child and whose ``broken``
    listing raises ``KeyError``. ``first`` is ``hc ls --json``, ``labelled`` the other."""
    pids: list[int] = []

    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        kind = (
            "first"
            if argv[1:] == ["ls", "--json"]
            else "labelled"
            if argv[1:4] == ["ls", "--json", "--label"]
            else None
        )
        if kind is not None and kind == broken:
            raise KeyError("surprise")
        if kind is not None and kind == sleeper:
            pid_file = tmp_path / f"{kind}.pid"
            script = f'echo $$ > "{pid_file}"; exec sleep 30'
            task = asyncio.ensure_future(
                probes.run_bounded(["/bin/sh", "-c", script], env=dict(os.environ))
            )
            for _ in range(500):
                if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
                    break
                await asyncio.sleep(0.01)
            pids.append(int(pid_file.read_text(encoding="utf-8")))
            return await task
        return await Fleet.__call__(fleet, argv)

    return runner, pids


def _fleet() -> Fleet:
    return Fleet({"local": linux(), **{n: linux() for n in WORKERS}})


@pytest.mark.parametrize(("sleeper", "broken"), [("first", "labelled"), ("labelled", "first")])
async def test_a_listing_that_raises_ends_and_no_child_or_task_runs_at_publication(
    tmp_path: Path, sleeper: str, broken: str
) -> None:
    fleet = _fleet()
    runner, pids = _runner(fleet, tmp_path, sleeper=sleeper, broken=broken)
    sink = WatchingSink(pids)
    w, _, _ = writer(fleet, sink=sink, timings=loop.Timings(hc_ls=0.3, cycle=2.0, reserve=0.2))
    w.runner = runner
    assert await w.once()
    assert len(pids) == 1
    assert sink.alive == [[]] and sink.tasks == [0]
    errors = [e for e in sink.docs[-1]["errors"] if "KeyError" in (e["text"] or "")]
    assert len(errors) == 1
    assert not any(e["where"] == "cycle" for e in sink.docs[-1]["errors"])


async def test_the_first_listing_is_used_when_only_the_labelled_one_raises(tmp_path: Path) -> None:
    fleet = _fleet()
    runner, _ = _runner(fleet, tmp_path, sleeper=None, broken="labelled")
    w, _, sink = writer(fleet)
    w.runner = runner
    assert await w.once()
    doc = sink.docs[-1]
    assert [m["link"]["state"] for m in doc["machines"][1:]] == ["connected", "connected"]
    assert sorted(fleet.probed()) == list(WORKERS)
    assert doc["machines"][1]["reading"]["agents"]["lanes"] is None
    assert doc["machines"][1]["reading"]["agents"]["active"] == 0
    assert [e["where"] for e in doc["errors"] if "KeyError" in (e["text"] or "")] == ["hc-label"]


async def test_the_first_listing_that_raises_is_no_listing(tmp_path: Path) -> None:
    fleet = _fleet()
    runner, _ = _runner(fleet, tmp_path, sleeper=None, broken="first")
    w, _, sink = writer(fleet)
    w.runner = runner
    assert await w.once()
    doc = sink.docs[-1]
    assert fleet.probed() == []
    assert [e["text"] for e in doc["errors"] if e["where"] == "hc"] == [
        "hc ls --json failed: unexpected KeyError"
    ]


async def test_a_signal_during_the_listings_ends_both_children_before_the_cycle_returns(
    tmp_path: Path,
) -> None:
    fleet = _fleet()
    first, first_pids = _runner(fleet, tmp_path / "a", sleeper="first", broken=None)
    lanes, lane_pids = _runner(fleet, tmp_path / "b", sleeper="labelled", broken=None)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        return await (lanes if "--label" in argv else first)(argv)

    w, _, sink = writer(fleet, timings=loop.Timings(hc_ls=5.0))
    w.runner = runner
    stop = asyncio.Event()
    task = asyncio.create_task(w.once_until(stop))
    for _ in range(500):
        if len(first_pids) + len(lane_pids) == 2:
            break
        await asyncio.sleep(0.01)
    pids = first_pids + lane_pids
    stop.set()
    assert await asyncio.wait_for(task, 5) is None
    assert [pid for pid in pids if _alive(pid)] == []
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current and not t.done()] == []
    assert sink.docs == []


async def test_a_child_whose_end_is_not_confirmed_gives_one_error_and_the_cycle_goes_on() -> None:
    from answerback_bridge.process import CommandCleanupError

    fleet = _fleet()

    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        if argv[1:4] == ["exec", "--on", "worker-a"]:
            raise CommandCleanupError("Command cleanup failed to confirm owned child reaping")
        return await Fleet.__call__(fleet, argv)

    w, _, sink = writer(fleet)
    w.runner = runner
    assert await w.once()
    doc = sink.docs[-1]
    entries = [e for e in doc["errors"] if e["where"] == "child"]
    assert len(entries) == 1 and "not confirm" in entries[0]["text"]
    assert doc["machines"][1]["reading"]["error"] == "probe failed"
    assert doc["machines"][2]["reading"]["state"] == "ok"


async def test_an_ordinary_error_of_the_worker_wait_still_ends_the_probes_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _fleet()
    pids: list[int] = []
    pid_file = tmp_path / "probe.pid"

    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        if argv[1:4] == ["exec", "--on", "worker-a"]:
            script = f'echo $$ > "{pid_file}"; exec sleep 30'
            return await probes.run_bounded(["/bin/sh", "-c", script], env=dict(os.environ))
        return await Fleet.__call__(fleet, argv)

    real_wait = asyncio.wait

    async def broken_wait(aws: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            return await real_wait(aws, *args, **kwargs)
        for _ in range(500):  # the wait of workers and service: once the child runs
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
                break
            await asyncio.sleep(0.01)
        pids.append(int(pid_file.read_text(encoding="utf-8")))
        raise RuntimeError("the wait broke")

    monkeypatch.setattr(asyncio, "wait", broken_wait)
    sink = WatchingSink(pids)
    w, _, _ = writer(fleet, sink=sink)
    w.runner = runner
    try:
        assert await w.once()
        assert len(pids) == 1
        assert sink.alive == [[]]
        assert any("RuntimeError" in (e["text"] or "") for e in sink.docs[-1]["errors"])
    finally:
        for pid in pids:
            if _alive(pid):
                os.kill(pid, 9)
