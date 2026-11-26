"""An absent discovery-time identity is refused before any lookup, reservation or send.

`Bridge.herdr_submit` and `Bridge.crew_submit` take a keyword-only `expected`, but
`expected=None` is still a legal call at runtime (R2-1V finding 1). The adapters must
refuse it themselves, before they touch the provider or the task store, so the
identity check can never be skipped by handing them nothing.
"""

from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.crew import _peer
from answerback_bridge.service import Bridge


class _Herdr:
    """Herdr TUI stand-in: one idle agent; counts lookups and prompt deliveries apart."""

    def __init__(self, terminal_id: str = "term-a", agent_session: str = "sess-a") -> None:
        self.terminal_id = terminal_id
        self.agent_session = agent_session
        self.lookups = 0
        self.sends = 0

    def agent(self) -> dict[str, Any]:
        return {
            "name": "worker",
            "terminal_id": self.terminal_id,
            "agent_session": self.agent_session,
            "agent_status": "idle",
        }

    async def check(self) -> str:
        self.lookups += 1
        return "fixture"

    async def get(self, target: str) -> dict[str, Any]:
        self.lookups += 1
        return self.agent()

    async def call(self, *args: str) -> dict[str, Any]:
        if args[:2] == ("agent", "list"):
            self.lookups += 1
            return {"agents": [self.agent()]}
        self.sends += 1
        return {"agent": {"terminal_id": self.terminal_id}}


class _Crew:
    """Crew stand-in: one chat row; counts lookups and sends apart."""

    origin = "http://127.0.0.1:9"

    def __init__(self) -> None:
        self.row = {
            "key": "review",
            "title": "Reviewer chat",
            "project": "/tmp/r21f",
            "agent": "kiro",
            "created": 11.0,
            "running": False,
        }
        self.lookups = 0
        self.sends = 0

    async def peers(self) -> list[dict[str, Any]]:
        self.lookups += 1
        return [_peer(self.row, self.origin)]

    async def peer(self, target: str) -> dict[str, Any]:
        self.lookups += 1
        return _peer(self.row, self.origin)

    async def submit(
        self, target: str, message: str, send_id: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        self.sends += 1
        return {"ok": True, "slot": target}


def _bridge(tmp_path: Path) -> tuple[Bridge, _Herdr, _Crew]:
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")
    herdr, crew = _Herdr(), _Crew()
    bridge.herdr = herdr
    bridge._crew_client = crew
    return bridge, herdr, crew


def _untouched(bridge: Bridge, herdr: _Herdr, crew: _Crew, request_id: str) -> None:
    assert herdr.lookups == 0 and crew.lookups == 0
    assert herdr.sends == 0 and crew.sends == 0
    assert bridge.store.by_request(request_id) is None
    assert bridge.store.counts() == {}


async def test_herdr_adapter_refuses_an_absent_identity_before_any_lookup(
    tmp_path: Path,
) -> None:
    bridge, herdr, crew = _bridge(tmp_path)
    try:
        adapter = bridge.adapters.get("herdr")
        # The annotation no longer admits None; the runtime guard is what this proves.
        with pytest.raises(ValueError, match="search again$"):
            await adapter.submit_native("worker", "Review", "none-herdr", None)
        _untouched(bridge, herdr, crew, "none-herdr")
    finally:
        await bridge.close()


async def test_crew_adapter_refuses_an_absent_identity_before_any_lookup(
    tmp_path: Path,
) -> None:
    bridge, herdr, crew = _bridge(tmp_path)
    try:
        adapter = bridge.adapters.get("crew")
        with pytest.raises(ValueError, match="search again$"):
            await adapter.submit_native("review", "Review", "none-crew", None)
        _untouched(bridge, herdr, crew, "none-crew")
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    ("method", "target"), [("herdr_submit", "worker"), ("crew_submit", "review")]
)
async def test_internal_submits_refuse_an_explicit_none_identity_before_reservation(
    tmp_path: Path, method: str, target: str
) -> None:
    bridge, herdr, crew = _bridge(tmp_path)
    try:
        submit = getattr(bridge, method)
        with pytest.raises(ValueError, match="search again$"):
            await submit(target, "Review", f"none-{method}", expected=None)
        _untouched(bridge, herdr, crew, f"none-{method}")
    finally:
        await bridge.close()


async def test_herdr_adapter_still_delivers_to_the_discovered_identity(tmp_path: Path) -> None:
    bridge, herdr, _ = _bridge(tmp_path)
    try:
        expected = {"terminal_id": "term-a", "agent_session": "sess-a"}
        result = await bridge.adapters.get("herdr").submit_native(
            "worker", "Review", "ok-herdr", expected
        )
        assert result["status"] == "submitted"
        assert herdr.sends == 1
        assert bridge.store.by_request("ok-herdr") is not None
    finally:
        await bridge.close()


async def test_herdr_adapter_still_refuses_a_changed_occupant(tmp_path: Path) -> None:
    bridge, herdr, _ = _bridge(tmp_path)
    try:
        with pytest.raises(ValueError, match="changed or is unavailable; search again$"):
            await bridge.adapters.get("herdr").submit_native(
                "worker", "Review", "stale-herdr", {"terminal_id": "term-gone"}
            )
        assert herdr.sends == 0
        assert bridge.store.counts() == {}
    finally:
        await bridge.close()


async def test_crew_adapter_still_delivers_the_discovered_identity(tmp_path: Path) -> None:
    bridge, _, crew = _bridge(tmp_path)
    try:
        expected = _peer(crew.row, crew.origin)
        result = await bridge.adapters.get("crew").submit_native(
            "review", "Review", "ok-crew", expected
        )
        assert result["status"] == "submitted"
        assert crew.sends == 1
        row = bridge.store.by_request("ok-crew")
        assert row is not None
        # The reserved row records the identity the caller discovered, never a substitute.
        assert bridge.store.get(row["id"])["metadata"]["peer"] == expected
    finally:
        await bridge.close()
