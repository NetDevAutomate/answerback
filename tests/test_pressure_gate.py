"""T-227 gate: peer_submit answers `provider_backoff` while the peer's account is held."""

import argparse
from typing import Any

import pytest
from pressure_fakes import NOTICE, WALL, FakePressure
from test_events_bridge import running_bridge

from answerback_bridge import harness
from answerback_bridge.cli import EXIT_FAILURE, exit_code_for
from answerback_bridge.pressure.gate import (
    backoff_result,
    hold_for,
    notice,
    pressure_text,
    request_id,
)
from answerback_bridge.service import Bridge
from answerback_bridge.transport import RemoteBridge


def hc_peer(kind: str) -> dict[str, Any]:
    return {
        "peer_id": "hc:t7",
        "provider": "hc",
        "target": "t7",
        "agent_kind": kind,
        "binding": "b-1",
        "send_ready": True,
    }


class ScriptedDirectory:
    def __init__(self, peer: dict[str, Any]) -> None:
        self.peer = peer

    async def resolve(self, selector: str, provider: str | None = None) -> dict[str, Any]:
        return dict(self.peer)


def script(bridge: Bridge, monkeypatch: pytest.MonkeyPatch, peer: dict[str, Any]) -> list[Any]:
    admitted: list[Any] = []

    async def submit_resolved(peer: dict[str, Any], message: str, request_id: str, **_: Any):
        admitted.append((peer["agent_kind"], message, request_id))
        return {"id": "task-1", "status": "submitted"}

    monkeypatch.setattr(bridge, "directory", lambda: ScriptedDirectory(peer))
    monkeypatch.setattr(bridge, "submit_resolved", submit_resolved)
    return admitted


async def test_peer_submit_under_pressure_returns_backoff_and_reserves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_bridge() as (service, path):
        bridge = service.bridge
        bridge.pressure = FakePressure({("kiro", "default"): (2, 120.0)})
        admitted = script(bridge, monkeypatch, hc_peer("kiro"))
        before = bridge.store.counts()
        # Over the socket, so the result passes the runtime handler unchanged.
        result = await RemoteBridge(path).call(
            "peer_submit",
            {"selector": "t7", "message": "Do it", "request_id": "r-1", "binding": "b-1"},
        )
        assert result == {
            "status": "provider_backoff",
            "provider": "kiro",
            "account_hint": "default",
            "level": 2,
            "until": WALL + 120.0,
            "retry_after_s": 120.0,
        }
        assert result["retry_after_s"] > 0
        assert bridge.store.counts() == before
        assert admitted == []


async def test_peer_submit_for_an_unheld_provider_proceeds_to_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_bridge() as (service, _):
        bridge = service.bridge
        bridge.pressure = FakePressure({("kiro", "default"): (2, 120.0)})
        admitted = script(bridge, monkeypatch, hc_peer("claude"))
        result = await bridge.peer_submit("t7", "Do it", "r-2", binding="b-1")
        assert result == {"id": "task-1", "status": "submitted"}
        assert admitted == [("claude", "Do it", "r-2")]


async def test_a_changed_binding_is_still_refused_before_the_pressure_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_bridge() as (service, _):
        bridge = service.bridge
        bridge.pressure = FakePressure({("kiro", "default"): (1, 60.0)})
        script(bridge, monkeypatch, hc_peer("kiro"))
        with pytest.raises(ValueError, match="changed since discovery"):
            await bridge.peer_submit("t7", "Do it", "r-3", binding="other")


def test_provider_backoff_is_a_failure_exit_and_a_harness_stop_state() -> None:
    args = argparse.Namespace(command="send")
    assert exit_code_for(args, {"status": "provider_backoff"}) == EXIT_FAILURE
    assert "provider_backoff" in harness.STOP_STATES


def test_gate_helpers() -> None:
    store = FakePressure({("bedrock", "default"): (3, 30.0)})
    assert hold_for(store, {"provider": "claude_code"}) is not None
    assert hold_for(store, {"provider": "kiro"}) is None
    assert hold_for(store, {"provider": "hc", "agent_kind": "mystery"}) is None
    assert notice(store, "bedrock") == NOTICE + "\n"
    assert notice(store, "kiro") is None
    held = store.status("bedrock")
    assert backoff_result(held, store)["retry_after_s"] == 30.0
    text = pressure_text("enter", held, 3)
    assert text.startswith("bedrock throttled: dispatch paused until ")
    assert text.endswith("(level 3), 3 workers in flight")
    assert pressure_text("exit", held, 0) == "bedrock pressure cleared"
    assert request_id("enter", held) == f"pressure-bedrock-default-enter-{int(WALL + 30)}"


async def test_a_repeat_of_a_request_with_a_receipt_gets_that_receipt_while_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_bridge() as (service, path):
        bridge = service.bridge
        bridge.pressure = FakePressure({("kiro", "default"): (2, 120.0)})
        bridge.config.select_provider("hc", True)  # The repeat check prepares the peer.
        admitted = script(bridge, monkeypatch, hc_peer("kiro"))
        # The receipt an earlier, unheld admission of r-7 left for this peer.
        target = bridge.core_tasks.target_key(bridge.core, "t7")
        stored, created = bridge.store.reserve(target, "Do it", "r-7", {"kind": "hc"})
        assert created
        before = bridge.store.counts()
        repeat = {"selector": "t7", "message": "Do it", "request_id": "r-7", "binding": "b-1"}
        result = await RemoteBridge(path).call("peer_submit", repeat)
        assert result["id"] == stored["id"] and result["status"] == "pending"
        assert "provider_backoff" not in result.values()
        assert bridge.store.counts() == before and admitted == []
        # The same request_id for different work is refused, as it would be unheld.
        with pytest.raises(ValueError, match="already describes different work"):
            await bridge.peer_submit("t7", "Do something else", "r-7", binding="b-1")
        # A request with no receipt is still held.
        fresh = await bridge.peer_submit("t7", "Do it", "r-8", binding="b-1")
        assert fresh["status"] == "provider_backoff" and admitted == []
