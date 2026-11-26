"""fleet: the client for the service's socket, its build outside the event loop, and the
deadline of the service step."""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from test_fleet_fakes import Fleet, Service, linux, writer

from answerback_bridge import transport
from answerback_bridge.fleet import loop, service


class FakeBridge:
    built = 0
    fail_next = False

    def __init__(self, config_path: Path) -> None:
        if FakeBridge.fail_next:
            FakeBridge.fail_next = False
            raise OSError("config-owner.json could not be read")
        FakeBridge.built += 1

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"method": method}


async def test_the_service_client_is_built_once_and_used_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeBridge.built = 0
    monkeypatch.setattr(transport, "RemoteBridge", FakeBridge)
    call = service.remote(tmp_path / "config.json")
    for _ in range(5):
        await call.prepare(1.0)
        assert await call("status", {}) == {"method": "status"}
    assert FakeBridge.built == 1


async def test_a_client_that_could_not_be_built_is_built_at_the_next_try(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeBridge.built = 0
    FakeBridge.fail_next = True
    monkeypatch.setattr(transport, "RemoteBridge", FakeBridge)
    call = service.remote(tmp_path / "config.json")
    with pytest.raises(OSError):
        await call.prepare(1.0)
    await call.prepare(1.0)
    assert await call("status", {}) == {"method": "status"}
    await call.prepare(1.0)
    assert FakeBridge.built == 1


class SlowClient:
    """A client whose build blocks for ``seconds``, as reading files on a slow disk does."""

    builds = 0

    def __init__(self, answers: Service, seconds: float) -> None:
        SlowClient.builds += 1
        time.sleep(seconds)
        self.answers = answers

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self.answers(method, params)


async def test_a_client_build_longer_than_the_cycle_does_not_make_the_cycle_longer() -> None:
    SlowClient.builds = 0
    answers = Service()
    answers.up = True
    remote = service.Remote(lambda: SlowClient(answers, 0.6))
    timings = loop.Timings(cycle=0.3, reserve=0.05)
    w, clock, sink = writer(Fleet({"local": linux(), "worker-a": linux()}), timings=timings)
    w.call = remote
    begun = time.monotonic()
    assert await w.once()
    elapsed = time.monotonic() - begun
    assert elapsed < 0.3, elapsed
    doc = sink.docs[-1]
    assert doc["health"]["service"]["state"] == "unreachable"
    assert doc["health"]["error"] == "service did not answer in time"
    assert doc["machines"][1]["reading"]["state"] == "ok"
    await asyncio.sleep(0.6)  # the build finishes in its thread
    clock.move(15)
    assert await w.once()
    assert sink.docs[-1]["health"]["service"]["state"] == "running"
    assert SlowClient.builds == 1
    assert answers.methods[0] == "status"


async def test_no_service_call_starts_at_or_after_the_deadline_of_the_cycle() -> None:
    starts: list[tuple[str, float]] = []
    answers = Service()
    answers.up = True
    running = asyncio.get_running_loop()

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        starts.append((method, running.time()))
        if method == "status":
            # Blocking work inside the answer, on purpose: it ends past the deadline.
            time.sleep(0.15)  # noqa: ASYNC251
        return await answers(method, params)

    timings = loop.Timings(cycle=0.12, reserve=0.02)
    w, _, sink = writer(Fleet({"local": linux()}), timings=timings)
    w.call = call
    begun = running.time()
    await w.once()
    assert [method for method, _ in starts] == ["status"]
    assert all(at < begun + 0.1 for _, at in starts)
    rc = sink.docs[-1]["receipts"]
    assert rc["stale"] is True and rc["counts"] is None


async def test_a_build_not_ready_when_its_own_wait_ends_is_kept_and_used_next_time() -> None:
    import threading

    builds: list[int] = []
    release = threading.Event()

    class Client:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"method": method}

    def build() -> Any:
        builds.append(1)
        release.wait(5)
        return Client()

    remote = service.Remote(build)
    running = asyncio.get_running_loop()
    deadline = running.time() + 10  # the cycle still has time
    first = await service.ask(remote, timeout=0.02, deadline=deadline)
    assert first.status_error == "service did not answer in time"
    release.set()
    await remote.prepare(2.0)
    assert await remote("status", {}) == {"method": "status"}
    assert builds == [1]
