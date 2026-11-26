"""T-227 gate: managed launches wait out provider pressure; the prompt carries the notice."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pressure_fakes import NOTICE, WALL, FakePressure
from test_native_workers import FakeTransport, finish
from test_native_workers import setup as native_setup
from test_workers import FakeACP

from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget
from answerback_bridge.config import Config
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.tasks import TaskStore
from answerback_bridge.workers import Workers

# The native workers' `setup` fixture, under its own name in this module.
setup = native_setup


async def status_of(store: TaskStore, task_id: str, expected: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            task = store.get(task_id)
            if task["status"] == expected:
                return task
            await asyncio.sleep(0.005)


class Budgets:
    """Counts execution budgets, each far shorter than the hold the test keeps."""

    def __init__(self) -> None:
        self.made = 0

    def __call__(self, seconds: float) -> RealActiveBudget:
        self.made += 1
        return RealActiveBudget(0.05)


# --- native CLI workers ---------------------------------------------------------------


def native(setup: Any, pressure: FakePressure, factory: Any = FakeTransport) -> NativeWorkers:
    config, path, store, _ = setup
    return NativeWorkers(
        "codex_cli", config, path, store, transport_factory=factory, pressure=pressure
    )


async def test_native_launch_waits_queued_without_spending_budget(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets = Budgets()
    monkeypatch.setattr("answerback_bridge.native_workers.ActiveBudget", budgets)
    pressure = FakePressure({("bedrock", "default"): (2, 120.0)})
    workers = native(setup, pressure)
    receipt = await workers.submit("test", "one", "held-1")
    queued = await status_of(workers.store, receipt["id"], "queued")
    assert queued["metadata"]["provider_backoff_until"] == WALL + 120.0
    assert queued["detail"].startswith("provider backoff until ")
    assert queued["detail"].endswith("(level 2)")
    # Held for four times the budget: the wait is outside it.
    await asyncio.sleep(0.2)
    assert FakeTransport.opened == []
    assert budgets.made == 0
    pressure.release()
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed"
    assert FakeTransport.opened == [None]
    assert budgets.made == 1
    assert pressure.waits == [("bedrock", "default")]
    assert done["message"] == "one"


async def test_native_prompt_carries_the_notice_only_while_held(setup: Any) -> None:
    pressure = FakePressure()

    class PressureDuringOpen(FakeTransport):
        async def open_session(self, session_id=None, mcp_servers=None):
            pressure.hold("bedrock", level=1, seconds=60.0)
            return await super().open_session(session_id, mcp_servers)

    workers = native(setup, pressure, PressureDuringOpen)
    receipt = await workers.submit("test", "the brief", "notice-1")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed"
    assert FakeTransport.prompts[0][1] == NOTICE + "\n" + "the brief"
    assert done["message"] == "the brief"


async def test_native_prompt_is_the_message_exactly_when_not_held(setup: Any) -> None:
    pressure = FakePressure({("kiro", "default"): (3, 60.0)})  # another account
    workers = native(setup, pressure)
    receipt = await workers.submit("test", "the brief", "plain-1")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed"
    assert FakeTransport.prompts[0][1] == "the brief"
    assert "provider_backoff_until" not in done["metadata"]
    assert pressure.waits == []


async def test_native_without_a_store_is_unchanged(setup: Any) -> None:
    _, _, _, workers = setup
    receipt = await workers.submit("test", "the brief", "none-1")
    assert (await finish(workers, receipt["id"]))["status"] == "completed"
    assert FakeTransport.prompts[0][1] == "the brief"


# --- Kiro ACP workers ---------------------------------------------------------------


class RecordingACP(FakeACP):
    messages: list[str] = []
    on_open: Any = None

    async def open_session(self, *args: Any, **kwargs: Any) -> str:
        if RecordingACP.on_open is not None:
            RecordingACP.on_open()
        return await super().open_session(*args, **kwargs)

    async def prompt(self, session_id: str, message: str) -> Any:
        RecordingACP.messages.append(message)
        FakeACP.sends += 1
        return SimpleNamespace(text="Kiro result", stop_reason="end_turn", session_id=session_id)


@pytest.fixture
def kiro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    RecordingACP.messages = []
    RecordingACP.on_open = None
    FakeACP.sends = 0
    FakeACP.launch_definitions = []
    FakeACP.session_definitions = []
    FakeACP.exit_failure = None
    monkeypatch.setattr("answerback_bridge.workers.KiroACP", RecordingACP)
    config = Config("test", kiro_workspaces={"reviewer": str(tmp_path)})
    store = TaskStore(tmp_path / "tasks.sqlite")

    def make(pressure: FakePressure | None) -> Workers:
        return Workers(config, tmp_path / "config.json", store, pressure=pressure)

    return make


async def wait_job(workers: Workers, task_id: str) -> dict[str, Any]:
    job = workers.jobs.get(task_id)
    if job is not None and job.task is not None:
        await job.task
    return workers.store.get(task_id)


async def test_kiro_launch_waits_queued_without_spending_budget(
    kiro: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets = Budgets()
    monkeypatch.setattr("answerback_bridge.workers.ActiveBudget", budgets)
    pressure = FakePressure({("kiro", "default"): (2, 120.0)})
    workers = kiro(pressure)
    receipt = await workers.submit("reviewer", "Review", "kiro-held")
    queued = await status_of(workers.store, receipt["id"], "queued")
    assert queued["metadata"]["provider_backoff_until"] == WALL + 120.0
    await asyncio.sleep(0.2)
    assert FakeACP.launch_definitions == []
    assert budgets.made == 0
    pressure.release()
    done = await wait_job(workers, receipt["id"])
    assert done["status"] == "completed"
    assert len(FakeACP.launch_definitions) == 1
    assert budgets.made == 1
    assert RecordingACP.messages == ["Review"]


async def test_kiro_prompt_carries_the_notice_only_while_held(kiro: Any) -> None:
    pressure = FakePressure()
    RecordingACP.on_open = lambda: pressure.hold("kiro", level=1, seconds=60.0)
    workers = kiro(pressure)
    receipt = await workers.submit("reviewer", "Review", "kiro-notice")
    done = await wait_job(workers, receipt["id"])
    assert done["status"] == "completed"
    assert RecordingACP.messages == [NOTICE + "\n" + "Review"]
    assert done["message"] == "Review"


async def test_kiro_prompt_is_the_message_exactly_when_not_held(kiro: Any) -> None:
    pressure = FakePressure({("bedrock", "default"): (1, 60.0)})
    workers = kiro(pressure)
    receipt = await workers.submit("reviewer", "Review", "kiro-plain")
    done = await wait_job(workers, receipt["id"])
    assert done["status"] == "completed"
    assert RecordingACP.messages == ["Review"]
    assert pressure.waits == []
