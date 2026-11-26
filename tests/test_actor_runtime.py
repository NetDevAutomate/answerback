"""Messaging stays live while command admission waits on a native client."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from answerback_bridge.actors import runtime
from answerback_bridge.actors.model import ActorError
from answerback_bridge.bus import coordinator
from answerback_bridge.bus.store import BusStore
from answerback_bridge.transport import reply_error


async def test_actor_progress_is_independent_of_blocked_command_and_closes_first(
    tmp_path: Path, monkeypatch
) -> None:
    events = []
    admission = asyncio.Event()
    release = asyncio.Event()
    enough_messages = asyncio.Event()
    stopping = asyncio.Event()

    class Lane:
        def __init__(self, *args):
            self.steps = 0

        async def start(self):
            events.append("actor-start")

        async def step(self):
            self.steps += 1
            if admission.is_set() and self.steps >= 3:
                enough_messages.set()
            await asyncio.sleep(0.01)
            return {"ready": True}

        async def close(self):
            events.append("actor-close")

        def status(self):
            return {"closed": True, "ready": False}

    class Bus:
        js = object()

        def __init__(self, config):
            self.fetched = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("bus-close")

        async def fetch(self, **kwargs):
            if not self.fetched:
                self.fetched = True
                return object()
            await asyncio.sleep(0.01)

        async def flush_outbox(self, store):
            return {}

    monkeypatch.setattr(runtime, "actor_lane_class", lambda: Lane)
    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: Bus)
    owner = coordinator.Coordinator(
        BusStore(tmp_path / "bus.sqlite"), SimpleNamespace(), actor_store=lambda: object()
    )

    async def wait_for_native_permission(message):
        admission.set()
        await release.wait()

    monkeypatch.setattr(owner, "_deliver", wait_for_native_permission)
    task = asyncio.create_task(owner.run(SimpleNamespace(), stopping))
    try:
        await asyncio.wait_for(enough_messages.wait(), 1)
        assert not release.is_set()
        assert owner.state["actors"]["ready"]
    finally:
        stopping.set()
        release.set()
        await asyncio.wait_for(task, 1)
    assert events.index("actor-close") < events.index("bus-close")
    assert not owner.state["actors"]["ready"]


async def test_optional_actor_lane_is_absent_until_enrollment_and_failure_stays_local(
    monkeypatch,
) -> None:
    stopping = asyncio.Event()
    state = {}
    enrollment = None
    entered = asyncio.Event()

    class RefusedLane:
        def __init__(self, *args):
            pass

        async def start(self):
            entered.set()
            raise ValueError("private broker diagnostic")

        async def close(self):
            pass

    monkeypatch.setattr(runtime, "actor_lane_class", lambda: RefusedLane)
    task = asyncio.create_task(
        runtime.run_actor_lane(object(), SimpleNamespace(), lambda: enrollment, state, stopping)
    )
    try:
        await asyncio.sleep(0)
        assert state == {"enabled": False, "ready": False}
        assert not entered.is_set()
        enrollment = object()
        await asyncio.wait_for(entered.wait(), 2)
        assert state["last_error"] == "ValueError"
        assert "private broker diagnostic" not in str(state)
        assert not task.done()
    finally:
        stopping.set()
        await asyncio.wait_for(task, 1)


def test_rpc_accepts_only_reviewed_actor_error_codes() -> None:
    known = reply_error({"kind": "actor", "code": "frontend_mismatch"}, "actor_send")
    assert isinstance(known, ActorError)
    assert known.code == "frontend_mismatch"
    private = "private-provider-diagnostic"
    for value in ({"kind": "actor", "code": private}, {"kind": "actor", "code": []}):
        failure = reply_error(value, "actor_send")
        assert not isinstance(failure, ActorError)
        assert private not in str(failure)
