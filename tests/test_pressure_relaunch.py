"""T-227 gate: a throttled managed worker is relaunched once, after the pause, by resume."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pressure_fakes import FakePressure, match
from test_native_workers import FakeTransport, finish, setup  # noqa: F401  (fixture)
from test_workers import FakeACP

from answerback_bridge.config import Config
from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.process import BridgeError
from answerback_bridge.tasks import TaskStore
from answerback_bridge.workers import THROTTLED_TWICE, Workers

THROTTLED = NativeResult("owned", "failed", None, "turn-1", evidence={"api_error_status": 429})
DONE = NativeResult("owned", "completed", "done", "turn-2")


class Sequenced(FakeTransport):
    """Each prompt returns the next scripted result."""

    results: list[NativeResult] = []

    async def prompt(self, session_id, message, *, prompt_id):
        self.prompts.append((session_id, message, prompt_id))
        return Sequenced.results.pop(0)


def by_evidence(evidence: Any, error: BaseException | None, **_: Any) -> Any:
    """Stands in for lane-detect's matcher: HTTP 429 evidence or a throttling error."""
    if isinstance(evidence, dict) and evidence.get("api_error_status") == 429:
        return match()
    if isinstance(error, BridgeError) and "ThrottlingException" in str(error):
        return match()
    return None


@pytest.fixture
def relaunch(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    config, path, store, _ = setup
    monkeypatch.setattr("answerback_bridge.native_workers._throttle_match", by_evidence)
    pressure = FakePressure(hold_on_wait=False)
    workers = NativeWorkers(
        "codex_cli", config, path, store, transport_factory=Sequenced, pressure=pressure
    )
    return workers, pressure


async def owned_session(workers: NativeWorkers) -> None:
    """Run one completed task first, so the worker has a saved session to resume."""
    Sequenced.results = [NativeResult("owned", "completed", "first", "turn-0")]
    first = await workers.submit("test", "warm up", "warm-up")
    assert (await finish(workers, first["id"]))["status"] == "completed"
    FakeTransport.opened.clear()
    FakeTransport.prompts.clear()


async def test_a_throttled_turn_is_relaunched_once_by_resume(relaunch: Any) -> None:
    workers, pressure = relaunch
    await owned_session(workers)
    Sequenced.results = [THROTTLED, DONE]
    receipt = await workers.submit("test", "the brief", "throttled-once")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed", done["detail"]
    assert done["result"] == "done"
    assert done["metadata"]["resumed_after_backoff"] is True
    assert "provider_backoff_until" in done["metadata"]
    assert done["turn_id"] == "turn-2"
    assert done["metadata"]["throttled_turn_id"] == "turn-1"
    assert FakeTransport.opened == ["owned", "owned"]
    assert [prompt[1] for prompt in FakeTransport.prompts] == ["the brief", "the brief"]
    assert pressure.waits == [("bedrock", "default")]
    assert done["message"] == "the brief"


async def test_a_second_throttle_fails_without_a_third_launch(relaunch: Any) -> None:
    workers, pressure = relaunch
    await owned_session(workers)
    Sequenced.results = [THROTTLED, THROTTLED, DONE]
    receipt = await workers.submit("test", "the brief", "throttled-twice")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "failed"
    assert done["detail"] == THROTTLED_TWICE
    assert "bedrock" not in done["detail"] and "429" not in done["detail"]
    assert FakeTransport.opened == ["owned", "owned"]
    assert len(Sequenced.results) == 1
    assert pressure.waits == [("bedrock", "default")]


async def test_cancel_during_the_backoff_wait_settles_uncertain(relaunch: Any) -> None:
    workers, pressure = relaunch
    await owned_session(workers)
    pressure.hold_on_wait = True

    # Unheld at launch; the throttled turn puts the account under pressure, as the
    # detector's signal does, so the relaunch wait blocks until the cancel.
    class HoldsAfterTurn(Sequenced):
        async def prompt(self, session_id, message, *, prompt_id):
            pressure.hold("bedrock", level=2, seconds=120.0)
            return await super().prompt(session_id, message, prompt_id=prompt_id)

    workers._factory = HoldsAfterTurn
    Sequenced.results = [THROTTLED, DONE]
    receipt = await workers.submit("test", "the brief", "cancelled-wait")
    waiting = await status_queued(workers.store, receipt["id"])
    assert waiting["detail"].startswith("provider backoff; one relaunch after ")
    assert waiting["metadata"]["resumed_after_backoff"] is False
    job = workers.jobs[receipt["id"]]
    assert job.task is not None
    job.task.cancel()
    await asyncio.gather(job.task, return_exceptions=True)
    done = workers.store.get(receipt["id"])
    assert done["status"] == "uncertain"
    assert FakeTransport.opened == ["owned"]


async def status_queued(store: TaskStore, task_id: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            task = store.get(task_id)
            if task["status"] == "queued" and "resumed_after_backoff" in task["metadata"]:
                return task
            await asyncio.sleep(0.005)


class RaisesThenAnswers(Sequenced):
    """The first prompt fails with a throttling error, the second answers."""

    failures: list[BaseException] = []

    async def prompt(self, session_id, message, *, prompt_id):
        if RaisesThenAnswers.failures:
            self.prompts.append((session_id, message, prompt_id))
            raise RaisesThenAnswers.failures.pop(0)
        return await super().prompt(session_id, message, prompt_id=prompt_id)


async def test_a_throttling_error_is_relaunched_once(relaunch: Any) -> None:
    workers, pressure = relaunch
    await owned_session(workers)
    workers._factory = RaisesThenAnswers
    RaisesThenAnswers.failures = [BridgeError("ThrottlingException: slow down")]
    Sequenced.results = [DONE]
    receipt = await workers.submit("test", "the brief", "error-once")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed", done["detail"]
    assert done["metadata"]["resumed_after_backoff"] is True
    assert FakeTransport.opened == ["owned", "owned"]
    assert pressure.waits == [("bedrock", "default")]


async def test_a_second_throttling_error_after_the_prompt_settles_uncertain(
    relaunch: Any,
) -> None:
    workers, pressure = relaunch
    await owned_session(workers)
    workers._factory = RaisesThenAnswers
    RaisesThenAnswers.failures = [BridgeError("ThrottlingException: one")] * 2
    receipt = await workers.submit("test", "the brief", "error-twice")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "uncertain"
    assert done["detail"] == THROTTLED_TWICE
    assert FakeTransport.opened == ["owned", "owned"]


async def test_a_completed_turn_is_never_relaunched(relaunch: Any) -> None:
    workers, pressure = relaunch
    Sequenced.results = [
        NativeResult("owned", "completed", "done", "turn-1", evidence={"api_error_status": 429})
    ]
    receipt = await workers.submit("test", "the brief", "recovered")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "completed"
    assert "resumed_after_backoff" not in done["metadata"]
    assert pressure.waits == []


async def test_without_a_matcher_hit_a_failed_turn_is_unchanged(relaunch: Any) -> None:
    workers, pressure = relaunch
    Sequenced.results = [NativeResult("owned", "failed", None, "turn-1", evidence={})]
    receipt = await workers.submit("test", "the brief", "plain-failure")
    done = await finish(workers, receipt["id"])
    assert done["status"] == "failed"
    assert done["detail"] == "codex_cli confirmed failed"
    assert "resumed_after_backoff" not in done["metadata"]
    assert pressure.waits == []


# --- Kiro ACP workers ---------------------------------------------------------------


class LimitedACP(FakeACP):
    texts: list[str] = []
    opened: list[str | None] = []

    async def open_session(self, session_id: str | None = None, *args: Any, **kw: Any) -> str:
        LimitedACP.opened.append(session_id)
        return await super().open_session(session_id, *args, **kw)

    async def prompt(self, session_id: str, message: str) -> Any:
        return SimpleNamespace(
            text=LimitedACP.texts.pop(0), stop_reason="end_turn", session_id=session_id
        )


def by_text(text: str | None, stop_reason: str, **_: Any) -> Any:
    return match("kiro", "kiro") if "rate limit" in (text or "") else None


async def test_kiro_throttled_turn_is_relaunched_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerback_bridge.workers.KiroACP", LimitedACP)
    monkeypatch.setattr("answerback_bridge.workers._throttle_match", by_text)
    FakeACP.exit_failure = None
    FakeACP.block_open = False
    LimitedACP.opened = []
    LimitedACP.texts = ["faster than your rate limit allows", "Kiro result"]
    config = Config("test", kiro_workspaces={"reviewer": str(tmp_path)})
    store = TaskStore(tmp_path / "tasks.sqlite")
    pressure = FakePressure(hold_on_wait=False)
    workers = Workers(config, tmp_path / "config.json", store, pressure=pressure)
    receipt = await workers.submit("reviewer", "Review", "kiro-relaunch")
    job = workers.jobs[receipt["id"]]
    assert job.task is not None
    await job.task
    done = store.get(receipt["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Kiro result"
    assert done["metadata"]["resumed_after_backoff"] is True
    assert LimitedACP.opened == [None, "kiro-session-1"]
    assert pressure.waits == [("kiro", "default")]
