"""The loop-lag guard: a blocked event loop is counted and logged once it runs again."""

import asyncio
import logging
import tempfile
import threading
import time
from pathlib import Path

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.runtime import LOOP_LAG_SECONDS, LOOP_WATCH_SECONDS, Service
from codex_herdr_bridge.transport import RemoteBridge


class Clock:
    """A scripted monotonic clock: the readings in order, then one watch interval per
    reading, which the guard measures as no lag at all. `exhausted` is set once the last
    scripted reading has been read."""

    def __init__(self, *readings: float) -> None:
        self.readings = list(readings)
        self.now = 0.0
        self.exhausted = asyncio.Event()

    def __call__(self) -> float:
        if self.readings:
            self.now = self.readings.pop(0)
            if not self.readings:
                self.exhausted.set()
        else:
            self.now += LOOP_WATCH_SECONDS
        return self.now


def block(seconds: float) -> None:
    time.sleep(seconds)


def recording(service: Service) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    service.event = lambda name, **fields: events.append((name, fields))  # type: ignore[method-assign]
    return events


async def watch(service: Service, clock: Clock) -> None:
    """Run the guard until it has read every scripted reading, then stop it."""
    service.clock = clock
    watcher = asyncio.create_task(service.watch_loop())
    await clock.exhausted.wait()
    service.stopping.set()
    await watcher


async def test_blocks_longer_than_the_threshold_are_counted_and_logged(tmp_path: Path) -> None:
    """The guard subtracts its own interval from the wake delay: the clock reads 0.0 before
    the first sleep and 1.6 after it, so the loop was blocked 1.5 s, not 1.6; a wake 1.05 s
    late is a 0.95 s block, under the 1 s threshold, and is not counted."""
    service = Service(tmp_path / "config.json")
    events = recording(service)
    await watch(service, Clock(0.0, 1.6, 1.7, 2.75, 4.5))
    assert service.loop_lag["lag_events"] == 2
    assert service.loop_lag["max_lag_seconds"] == pytest.approx(1.65)
    assert service.loop_lag["last_lag_seconds"] == pytest.approx(1.65)
    assert service.loop_lag["last_lag_at"] == pytest.approx(time.time(), abs=5)
    assert [name for name, _ in events] == ["loop_lag", "loop_lag"]
    assert [fields["seconds"] for _, fields in events] == [pytest.approx(1.5), pytest.approx(1.65)]


async def test_short_blocks_are_not_counted(tmp_path: Path) -> None:
    service = Service(tmp_path / "config.json")
    events = recording(service)
    await watch(service, Clock(0.0, 0.6, 1.2))
    assert service.loop_lag["lag_events"] == 0
    assert service.loop_lag["max_lag_seconds"] == 0.0
    assert service.loop_lag["last_lag_seconds"] is None
    assert events == []


async def test_the_guard_reads_the_running_loops_clock_by_default(tmp_path: Path) -> None:
    """One real block, measured with the loop's own clock, within scheduling slack."""
    service = Service(tmp_path / "config.json")
    events = recording(service)
    watcher = asyncio.create_task(service.watch_loop())
    asyncio.get_running_loop().call_later(0.05, block, LOOP_LAG_SECONDS + 0.3)
    await asyncio.sleep(1.6)
    service.stopping.set()
    await watcher
    assert service.loop_lag["lag_events"] == 1
    assert LOOP_LAG_SECONDS < service.loop_lag["max_lag_seconds"] < LOOP_LAG_SECONDS + 0.5
    assert events == [("loop_lag", {"seconds": service.loop_lag["last_lag_seconds"]})]


async def test_a_slow_log_handler_does_not_delay_the_loop_or_the_count(tmp_path: Path) -> None:
    """The `loop_lag` line is written from a worker thread: the count `status` reads is in
    place while the handler is still blocked, and the loop keeps running on time."""
    service = Service(tmp_path / "config.json")
    service.clock = Clock(0.0, 1.6)
    emitting = threading.Event()
    released = threading.Event()
    records: list[tuple[int, str]] = []

    class SlowHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitting.set()
            released.wait(timeout=1)
            records.append((threading.get_ident(), record.getMessage()))

    gaps: list[float] = []

    async def tick() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not service.stopping.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last - 0.01)
            last = now

    service.log.addHandler(SlowHandler())
    ticker = asyncio.create_task(tick())
    watcher = asyncio.create_task(service.watch_loop())
    try:
        assert await asyncio.to_thread(emitting.wait, 5)
        # The handler is blocked in its thread; the count is already in place.
        assert service.loop_lag["lag_events"] == 1  # what `status` copies into `loop`
        assert records == []
    finally:
        released.set()
        service.stopping.set()
        await asyncio.gather(watcher, ticker)
    assert max(gaps) < 0.2, f"the loop stalled {max(gaps):.3f} s during the write"
    assert len(records) == 1
    assert records[0][0] != threading.get_ident()
    assert '"event": "loop_lag"' in records[0][1]
    assert '"seconds": 1.5' in records[0][1]


async def test_a_running_service_reports_a_block_through_status() -> None:
    """`serve()` starts the guard and `status` carries what it counted. The block is real,
    so the test polls for the count instead of trusting the runner's scheduling."""
    with tempfile.TemporaryDirectory(prefix="hb-", dir="/tmp") as directory:
        config = Path(directory) / "config.json"
        Config(provider_selection=[]).save(config)
        service = Service(config)
        serving = asyncio.create_task(service.serve())
        for _ in range(400):  # up to 20 s for the socket to appear
            if service.path.exists():
                break
            await asyncio.sleep(0.05)
        remote = RemoteBridge(config)
        assert (await remote.call("status", {}))["loop"]["lag_events"] == 0
        asyncio.get_running_loop().call_later(0.05, block, LOOP_LAG_SECONDS + 0.5)
        deadline = time.monotonic() + 5
        while True:
            status = await remote.call("status", {})
            if status["loop"]["lag_events"] or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.1)
        service.stopping.set()
        await serving
    assert status["loop"]["lag_events"] == 1
    assert status["loop"]["max_lag_seconds"] > LOOP_LAG_SECONDS
    assert status["loop"]["last_lag_at"] is not None
