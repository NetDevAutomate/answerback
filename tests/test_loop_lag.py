"""The loop-lag guard: a blocked event loop is counted and logged once it runs again."""

import asyncio
import time
from pathlib import Path

from codex_herdr_bridge.runtime import LOOP_LAG_SECONDS, Service


def block(seconds: float) -> None:
    time.sleep(seconds)


async def test_a_block_longer_than_the_threshold_is_counted_and_logged(tmp_path: Path) -> None:
    service = Service(tmp_path / "config.json")
    events: list[tuple[str, dict]] = []
    service.event = lambda name, **fields: events.append((name, fields))  # type: ignore[method-assign]
    watcher = asyncio.create_task(service.watch_loop())
    # Armed first, then blocked for the threshold plus the watch interval and more, so the
    # measured overrun exceeds the threshold whatever the sleep's phase was when it began.
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, block, LOOP_LAG_SECONDS + 0.5)
    await asyncio.sleep(1.9)
    service.stopping.set()
    await watcher
    assert service.loop_lag["lag_events"] == 1
    assert LOOP_LAG_SECONDS < service.loop_lag["max_lag_seconds"] <= LOOP_LAG_SECONDS + 0.5
    assert service.loop_lag["last_lag_seconds"] == service.loop_lag["max_lag_seconds"]
    assert service.loop_lag["last_lag_at"] is not None
    assert events == [("loop_lag", {"seconds": service.loop_lag["last_lag_seconds"]})]


async def test_short_blocks_are_not_counted(tmp_path: Path) -> None:
    service = Service(tmp_path / "config.json")
    watcher = asyncio.create_task(service.watch_loop())
    asyncio.get_running_loop().call_later(0.05, block, LOOP_LAG_SECONDS / 2)
    await asyncio.sleep(0.8)
    service.stopping.set()
    await watcher
    assert service.loop_lag["lag_events"] == 0
    assert service.loop_lag["max_lag_seconds"] == 0.0
