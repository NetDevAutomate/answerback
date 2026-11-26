import asyncio
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


async def test_worker_result_survives_bridge_restart(tmp_path: Path) -> None:
    config = Config("test-session")
    bridge = Bridge(config, tmp_path / "config.json")
    task, _ = bridge.store.reserve(
        "herdr:test-session:terminal-1", "Review", "test-1", {"kind": "herdr"}
    )
    bridge.complete(task["id"], "Reviewed")
    restarted = Bridge(config, tmp_path / "config.json")
    receipt = await restarted.get_task(task["id"], refresh=False)
    assert receipt["status"] == "completed"
    assert receipt["result"] == "Reviewed"
    assert "message" not in receipt


async def test_submission_ack_cannot_overwrite_a_fast_completion(tmp_path: Path) -> None:
    bridge = Bridge(Config("test-session"), tmp_path / "config.json")
    task, _ = bridge.store.reserve("herdr:test", "Review", "test-1", {"kind": "herdr"})
    bridge.complete(task["id"], "Already done")
    bridge.store.update(task["id"], "submitted")
    assert (await bridge.get_task(task["id"], refresh=False))["result"] == "Already done"


async def test_cancelled_submission_retains_an_uncertain_receipt(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class CancelledProvider:
        async def check(self) -> str:
            return "fixture"

        async def get(self, target: str) -> dict[str, str]:
            return {"terminal_id": "term", "agent_status": "idle"}

        async def call(self, *args: str) -> dict[str, Any]:
            raise asyncio.CancelledError

    bridge.herdr = CancelledProvider()
    with pytest.raises(asyncio.CancelledError):
        await bridge.herdr_submit(
            "worker", "Possibly sent", "cancelled", expected={"terminal_id": "term"}
        )
    retry = await bridge.herdr_submit(
        "worker", "Possibly sent", "cancelled", expected={"terminal_id": "term"}
    )
    assert retry["status"] == "uncertain"


class _CountingHerdr:
    def __init__(self, terminal_id: str = "term-a") -> None:
        self.terminal_id = terminal_id
        self.sends = 0

    async def check(self) -> str:
        return "fixture"

    async def get(self, target: str) -> dict[str, Any]:
        return {"terminal_id": self.terminal_id, "agent_status": "idle"}

    async def call(self, *args: str) -> dict[str, Any]:
        self.sends += 1
        return {"agent": {"terminal_id": self.terminal_id}}


class _CountingCrew:
    origin = "http://127.0.0.1:8080"

    def __init__(self) -> None:
        self.sends = 0

    async def peer(self, target: str) -> dict[str, Any]:
        return {
            "peer_id": f"crew:{target}",
            "provider": "crew",
            "target": target,
            "name": "Reviewer",
            "project": "/tmp/review",
            "created": 1.0,
            "agent": "kiro",
        }

    async def submit(
        self, target: str, message: str, send_id: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        self.sends += 1
        return {"ok": True, "slot": target}


@pytest.mark.parametrize("method", ["herdr_submit", "crew_submit"])
async def test_internal_submit_methods_require_an_explicit_expected_identity(
    tmp_path: Path, method: str
) -> None:
    # The internal methods are the only place a caller could still hand the adapter
    # expected=None and skip its identity check; the signature forbids it, and the
    # identity is keyword-only so it cannot be confused with a positional argument.
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")
    herdr, crew = _CountingHerdr(), _CountingCrew()
    bridge.herdr = herdr
    bridge._crew_client = crew
    submit = getattr(bridge, method)
    with pytest.raises(TypeError, match="expected"):
        await submit("worker", "Review", "no-identity")
    with pytest.raises(TypeError):
        await submit("worker", "Review", "positional", {"terminal_id": "term-a"})
    assert herdr.sends == 0 and crew.sends == 0
    assert bridge.store.counts() == {}
    await bridge.close()


async def test_herdr_submit_refuses_a_changed_occupant_when_expected_is_passed(
    tmp_path: Path,
) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")
    herdr = _CountingHerdr(terminal_id="term-b")
    bridge.herdr = herdr
    with pytest.raises(ValueError, match="search again"):
        await bridge.herdr_submit("worker", "Review", "race", expected={"terminal_id": "term-a"})
    assert herdr.sends == 0
    assert bridge.store.counts() == {}
    await bridge.close()


async def test_failed_history_reads_do_not_starve_later_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.process import BridgeError

    bridge = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    for number in range(5):
        thread_id = f"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee{number}"
        bridge.config.desktop_threads[str(number)] = thread_id
        bridge.store.reserve(
            f"desktop:{thread_id}",
            "Review",
            str(number),
            {"kind": "desktop", "alias": str(number), "thread_id": thread_id},
        )

    class UnavailableHistory:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> None:
            raise BridgeError("Provider unavailable")

        async def __aexit__(self, *_: Any) -> None:
            pass

    monkeypatch.setattr(
        "answerback_bridge.providers.codex.history.HistoryReader", UnavailableHistory
    )
    batch = bridge.store.reconcilable_ids(limit=4)
    for task_id in batch:
        with pytest.raises(BridgeError):
            await bridge.get_task(task_id)
    assert bridge.store.reconcilable_ids(limit=1)[0] not in batch
