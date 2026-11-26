"""T-227 gate: pressure enter/exit is told to every registered client, once each."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pressure_fakes import WALL, ClockedStore, match

from answerback_bridge.config import Config
from answerback_bridge.pressure import Pressure, PressureStore
from answerback_bridge.pressure.gate import ResumeTimers, clock_time, pressure_text, request_id
from answerback_bridge.service import Bridge

CLIENTS = ["kiro", "claude"]


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def bridge(tmp_path: Path) -> AsyncIterator[Bridge]:
    path = tmp_path / "config.json"
    config = Config(provider_selection=[], client_registrations=list(CLIENTS))
    config.save(path)
    made = Bridge(config, path)
    try:
        yield made
    finally:
        await made.close()


def messages(bridge: Bridge, client: str) -> list[dict]:
    return [
        row
        for row in bridge.mailbox.pending(client)["messages"]
        if row["topic"] == "provider_pressure"
    ]


def held(level: int = 2, until: float = 1_800_000_120.0) -> Pressure:
    return Pressure("kiro", "default", level, until, 10_120.0, 1_800_000_000.0, None)


def test_enter_queues_one_message_per_client_and_is_idempotent(bridge: Bridge) -> None:
    pressure = held()
    bridge._on_pressure("enter", pressure)
    for client in CLIENTS:
        [row] = messages(bridge, client)
        assert row["message"] == (
            f"kiro throttled: dispatch paused until {clock_time(pressure.until_wall)} "
            "(level 2), 0 workers in flight"
        )
    # The same transition at the same `until` is the same notification.
    bridge._on_pressure("enter", pressure)
    for client in CLIENTS:
        assert len(messages(bridge, client)) == 1
        status = bridge.mailbox.status(client, f"pressure-kiro-default-enter-{1_800_000_120}")
        assert status["status"] == "queued"


def test_raise_queues_nothing_and_exit_queues_one(bridge: Bridge) -> None:
    bridge._on_pressure("raise", held(level=3))
    assert all(messages(bridge, client) == [] for client in CLIENTS)
    cleared = Pressure("kiro", "default", 0, None, None, 1_800_000_000.0, 1_800_000_700.0)
    bridge._on_pressure("exit", cleared)
    for client in CLIENTS:
        [row] = messages(bridge, client)
        assert row["message"] == "kiro pressure cleared"


def test_the_store_listener_is_registered_and_counts_work_in_flight(bridge: Bridge) -> None:
    # The bridge's own store, as constructed: a signal reaches the inboxes through it.
    bridge.workers.jobs["a"] = object()  # type: ignore[assignment]
    bridge.core_tasks.jobs["b"] = object()  # type: ignore[assignment]
    try:
        bridge.pressure.signal(match("kiro", "kiro"))
    finally:
        bridge.workers.jobs.clear()
        bridge.core_tasks.jobs.clear()
    for client in CLIENTS:
        [row] = messages(bridge, client)
        assert row["message"].startswith("kiro throttled: dispatch paused until ")
        assert row["message"].endswith("(level 1), 2 workers in flight")


def test_a_full_inbox_never_raises_into_the_store(bridge: Bridge, monkeypatch) -> None:
    def full(*args, **kwargs):
        raise ValueError("Notification inbox capacity reached for this client")

    monkeypatch.setattr(bridge.mailbox, "notify", full)
    bridge._on_pressure("enter", held())
    assert bridge.pressure_notify_state == {"errors": 2, "last_error": "ValueError"}


def test_two_pressure_cycles_each_notify_their_exit(bridge: Bridge) -> None:
    mono, wall = Clock(1_000.0), Clock(1_800_000_000.0)
    settings = bridge.pressure.settings
    bridge.pressure = PressureStore(None, settings=settings, clock=mono, wall=wall)
    bridge.pressure.subscribe(bridge._on_pressure)
    for _ in range(2):
        bridge.pressure.signal(match("kiro", "kiro"))
        mono.now += 700
        wall.now += 700
        bridge.pressure.decay()
        mono.now += 100
        wall.now += 100
    texts = [row["message"] for row in messages(bridge, "claude")]
    assert texts.count("kiro pressure cleared") == 2
    assert len(texts) == 4


class Timers:
    """A scheduler the test fires by hand: records (delay, callback), cancels by handle."""

    def __init__(self) -> None:
        self.armed: list[tuple[float, Any]] = []
        self.cancelled: list[float] = []

    def __call__(self, delay: float, callback: Any) -> Any:
        self.armed.append((delay, callback))
        return SimpleNamespace(cancel=lambda: self.cancelled.append(delay))

    def fire(self) -> None:
        self.armed[-1][1]()


@pytest.fixture
def clocked(bridge: Bridge) -> tuple[Bridge, ClockedStore, Timers]:
    store = ClockedStore(None, settings=bridge.pressure.settings)
    timers = Timers()
    bridge.pressure = store
    bridge.pressure_resume = ResumeTimers(store, bridge._pressure_resumed, schedule=timers)
    store.subscribe(bridge._on_pressure)
    return bridge, store, timers


def test_the_resume_notice_is_due_when_the_hold_ends_and_a_new_signal_re_arms_it(
    clocked: tuple[Bridge, ClockedStore, Timers],
) -> None:
    bridge, store, timers = clocked
    store.signal(match("kiro", "kiro"))
    assert [delay for delay, _ in timers.armed] == [60.0]
    store.advance(30)
    store.signal(match("kiro", "kiro"))  # Level 2 while held: 120 s from now.
    assert [delay for delay, _ in timers.armed] == [60.0, 120.0]
    assert timers.cancelled == [60.0]
    store.advance(120)
    timers.fire()
    until = store.status("kiro").until_wall
    for client in CLIENTS:
        texts = [row["message"] for row in messages(bridge, client)]
        assert texts == [
            f"kiro throttled: dispatch paused until {clock_time(WALL + 60)} (level 1), "
            "0 workers in flight",
            "kiro backoff ended: dispatch resumed (level 2)",
        ]
        status = bridge.mailbox.status(client, f"pressure-kiro-default-resume-{int(until)}")  # type: ignore[arg-type]
        assert status["status"] == "queued"
    # The level-0 decay two clean windows later adds nothing: the owner heard at `until`.
    for _ in range(2):
        store.advance(600)
        store.decay()
    assert store.status("kiro").level == 0
    assert all(len(messages(bridge, client)) == 2 for client in CLIENTS)


def test_clear_while_held_cancels_the_resume_notice_and_says_cleared(
    clocked: tuple[Bridge, ClockedStore, Timers],
) -> None:
    bridge, store, timers = clocked
    store.signal(match("bedrock", "codex"))
    store.advance(10)
    store.clear("bedrock")
    assert timers.cancelled == [60.0] and bridge.pressure_resume.pending == {}
    for client in CLIENTS:
        texts = [row["message"] for row in messages(bridge, client)]
        assert texts[-1] == "bedrock pressure cleared" and len(texts) == 2


def test_a_due_callback_re_arms_when_the_hold_was_extended_meanwhile(
    clocked: tuple[Bridge, ClockedStore, Timers],
) -> None:
    bridge, store, timers = clocked
    store.signal(match("kiro", "kiro"))
    # Fired early (the loop's clock and the store's can differ): still held, re-armed.
    store.advance(59)
    timers.fire()
    assert [delay for delay, _ in timers.armed] == [60.0, 1.0]
    assert all(len(messages(bridge, client)) == 1 for client in CLIENTS)
    store.advance(1)
    timers.fire()
    assert all(len(messages(bridge, client)) == 2 for client in CLIENTS)
    # After the hold: a new signal is a new hold, with a new resume notice of its own.
    store.signal(match("kiro", "kiro"))
    assert [delay for delay, _ in timers.armed][-1] == 60.0
    store.advance(60)
    timers.fire()
    assert all(len(messages(bridge, client)) == 4 for client in CLIENTS)


def test_without_a_running_loop_nothing_is_armed(bridge: Bridge) -> None:
    # The sync tests above call the listener outside the loop: no timer, no error.
    bridge._on_pressure("enter", held())
    assert bridge.pressure_resume.pending == {}
    assert pressure_text("resume", held(), 0) == "kiro backoff ended: dispatch resumed (level 2)"
    assert request_id("resume", held()) == f"pressure-kiro-default-resume-{1_800_000_120}"
