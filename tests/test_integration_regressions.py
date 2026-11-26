from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import BridgeError
from codex_herdr_bridge.service import Bridge
from codex_herdr_bridge.transport import encode


def crew_peer(**changes: Any) -> dict[str, Any]:
    return {
        "peer_id": "crew:review",
        "provider": "crew",
        "target": "review",
        "name": "Invoice reviewer",
        "project": "/tmp/invoices",
        "created": 1.0,
        "agent": "kiro",
        **changes,
    }


async def test_keyword_dispatch_preserves_the_selected_crew_identity(tmp_path: Path) -> None:
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")

    class ReplacedCrew:
        origin = "http://127.0.0.1:8080"
        sends = 0

        async def peers(self) -> list[dict[str, Any]]:
            return [crew_peer(binding="discovered")]

        async def peer(self, target: str) -> dict[str, Any]:
            return crew_peer(name="Personal chat", project="/tmp/personal", created=2.0)

        async def submit(
            self, target: str, message: str, send_id: str, expected: dict[str, Any]
        ) -> dict[str, Any]:
            if expected["created"] != 2.0:
                from codex_herdr_bridge.crew import CrewIdentityChanged

                raise CrewIdentityChanged("Chat was replaced")
            self.sends += 1
            return {"ok": True, "slot": target}

    fake = ReplacedCrew()
    bridge._crew_client = fake
    # The adapter compares the selected identity with the live chat before any
    # reservation, so the replacement is refused outright: no receipt row is left
    # claiming an uncertain delivery that never happened.
    with pytest.raises(
        ValueError, match="^Selected Crew identity changed or is unavailable; search again$"
    ):
        await bridge.peer_submit(
            "Invoice reviewer",
            "Review invoices",
            "one",
            provider="crew",
            binding="discovered",
        )
    assert fake.sends == 0
    assert bridge.store.by_request("one") is None
    assert bridge.store.counts() == {}
    await bridge.close()


async def test_peer_submit_with_provider_crew_delivers_the_discovered_identity(
    tmp_path: Path,
) -> None:
    # crew_submit is no longer an RPC: provider "crew" delivers through peer_submit,
    # which hands the chat resolved at discovery to the adapter as `expected`, so the
    # Crew client's own identity check always has the discovery-time record.
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")

    class RecordingCrew:
        origin = "http://127.0.0.1:8080"

        def __init__(self) -> None:
            self.sends = 0
            self.expected: dict[str, Any] | None = None

        async def peers(self) -> list[dict[str, Any]]:
            return [crew_peer(binding="discovered")]

        async def peer(self, target: str) -> dict[str, Any]:
            return crew_peer()

        async def submit(
            self, target: str, message: str, send_id: str, expected: dict[str, Any]
        ) -> dict[str, Any]:
            self.sends += 1
            self.expected = expected
            return {"ok": True, "slot": target}

    fake = RecordingCrew()
    bridge._crew_client = fake
    peer = (await bridge.peer_search("Invoice reviewer", provider="crew"))["peers"][0]
    result = await bridge.peer_submit(
        "Invoice reviewer", "Review invoices", "one", provider="crew", binding=peer["binding"]
    )
    assert result["status"] == "submitted"
    assert fake.sends == 1
    assert fake.expected is not None and fake.expected["binding"] == "discovered"
    await bridge.close()


async def test_lost_crew_ack_cannot_erase_a_completed_worker_receipt(tmp_path: Path) -> None:
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")

    class LostAck:
        origin = "http://127.0.0.1:8080"

        async def peer(self, target: str) -> dict[str, Any]:
            return crew_peer()

        async def submit(
            self, target: str, message: str, send_id: str, expected: dict[str, Any]
        ) -> dict[str, Any]:
            bridge.complete(send_id, "Verified worker result")
            raise BridgeError("Connection lost after POST")

    bridge._crew_client = LostAck()
    result = await bridge.crew_submit("review", "Review", "lost-ack", expected=crew_peer())
    assert result["status"] == "completed"
    assert result["result"] == "Verified worker result"
    assert not result["metadata"].get("identity_invalidated")
    await bridge.close()


def test_task_listing_omits_large_results_and_stays_within_transport_bound(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")
    for number in range(6):
        task, _ = bridge.store.reserve(f"herdr:{number}", "Review", str(number), {"kind": "herdr"})
        bridge.complete(task["id"], "🐍" * 200_000)
    result = bridge.task_list()
    assert all("result" not in task and "message" not in task for task in result["tasks"])
    assert all(task["has_result"] for task in result["tasks"])
    assert len(encode({"version": 1, "ok": True, "result": result})) < 64_000


async def test_herdr_keyword_cannot_retarget_to_a_replacement_terminal(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class ReplacedHerdr:
        sends = 0

        async def check(self) -> str:
            return "fixture"

        async def get(self, target: str) -> dict[str, Any]:
            return {"terminal_id": "term-personal", "agent_status": "idle"}

        async def call(self, *args: str) -> dict[str, Any]:
            if args == ("agent", "list"):
                return {
                    "agents": [
                        {
                            "name": "worker",
                            "terminal_id": "term-payments",
                            "cwd": "/work/payments",
                            "agent_status": "idle",
                        }
                    ]
                }
            self.sends += 1
            return {"agent": {"terminal_id": "term-personal"}}

    fake = ReplacedHerdr()
    bridge.herdr = fake
    peer = (await bridge.peer_search("payments", provider="herdr"))["peers"][0]
    with pytest.raises(ValueError, match="identity"):
        await bridge.peer_submit(
            "payments",
            "Review payments",
            "identity-race",
            provider="herdr",
            binding=peer["binding"],
        )
    assert fake.sends == 0
    await bridge.close()


class _ReplaceableHerdr:
    """`agent list` and `agent get` both answer for the live occupant, so changing the
    identity between peer_search and peer_submit models a replaced Herdr agent."""

    def __init__(self) -> None:
        self.terminal_id = "term-a"
        self.agent_session = "session-a"
        self.sends = 0

    async def check(self) -> str:
        return "fixture"

    async def get(self, target: str) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "agent_status": "idle",
            "agent_session": self.agent_session,
        }

    async def call(self, *args: str) -> dict[str, Any]:
        if args == ("agent", "list"):
            return {
                "agents": [
                    {
                        "name": "worker",
                        "terminal_id": self.terminal_id,
                        "agent_session": self.agent_session,
                        "agent_status": "idle",
                    }
                ]
            }
        self.sends += 1
        return {"agent": {"terminal_id": self.terminal_id}}


@pytest.mark.parametrize("replacement", ["terminal_and_session", "session_only"])
async def test_herdr_binding_refuses_an_occupant_replaced_between_search_and_submit(
    tmp_path: Path, replacement: str
) -> None:
    # The binding covers terminal_id AND agent_session: the same terminal re-occupied
    # by another agent session is a different peer and is refused before reservation.
    bridge = Bridge(Config("test"), tmp_path / "config.json")
    fake = _ReplaceableHerdr()
    bridge.herdr = fake
    discovered = await bridge.peer_search("worker", provider="herdr")
    binding = discovered["peers"][0]["binding"]
    if replacement == "terminal_and_session":
        fake.terminal_id = "term-b"
    fake.agent_session = "session-b"
    with pytest.raises(ValueError, match="search again"):
        await bridge.peer_submit(
            "worker", "Review payments", "race", provider="herdr", binding=binding
        )
    assert fake.sends == 0
    assert bridge.store.counts() == {}
    fake.terminal_id, fake.agent_session = "term-a", "session-a"
    refreshed = await bridge.peer_search("worker", provider="herdr")
    fresh_binding = refreshed["peers"][0]["binding"]
    assert fresh_binding == binding
    result = await bridge.peer_submit(
        "worker", "Review payments", "race-ok", provider="herdr", binding=fresh_binding
    )
    assert result["status"] == "submitted"
    assert fake.sends == 1
    await bridge.close()


@pytest.mark.parametrize("replacement", ["project_and_created", "created_only"])
async def test_crew_binding_refuses_a_chat_replaced_between_search_and_submit(
    tmp_path: Path, replacement: str
) -> None:
    # The binding covers the created stamp too: a slot recreated under the same key
    # and project is a different chat and is refused before reservation.
    from codex_herdr_bridge.crew import _peer

    def raw_slot(**changes: Any) -> dict[str, Any]:
        return {
            "key": "review",
            "title": "Invoice reviewer",
            "project": "/tmp/invoices",
            "agent": "kiro",
            "created": 1.0,
            "running": False,
            **changes,
        }

    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")

    class MutableCrew:
        origin = "http://127.0.0.1:8080"

        def __init__(self) -> None:
            self.row = raw_slot()
            self.sends = 0

        async def peers(self) -> list[dict[str, Any]]:
            return [_peer(self.row, self.origin)]

        async def peer(self, target: str) -> dict[str, Any]:
            return _peer(self.row, self.origin)

        async def submit(
            self, target: str, message: str, send_id: str, expected: dict[str, Any]
        ) -> dict[str, Any]:
            self.sends += 1
            return {"ok": True, "slot": target}

    fake = MutableCrew()
    bridge._crew_client = fake
    discovered = await bridge.peer_search("Invoice reviewer", provider="crew")
    binding = discovered["peers"][0]["binding"]
    if replacement == "project_and_created":
        fake.row = raw_slot(project="/tmp/personal", created=2.0)
    else:
        fake.row = raw_slot(created=2.0)
    with pytest.raises(ValueError, match="search again"):
        await bridge.peer_submit(
            "Invoice reviewer", "Review invoices", "race", provider="crew", binding=binding
        )
    assert fake.sends == 0
    assert bridge.store.counts() == {}
    fake.row = raw_slot()
    refreshed = await bridge.peer_search("Invoice reviewer", provider="crew")
    fresh_binding = refreshed["peers"][0]["binding"]
    assert fresh_binding == binding
    result = await bridge.peer_submit(
        "Invoice reviewer", "Review invoices", "race-ok", provider="crew", binding=fresh_binding
    )
    assert result["status"] == "submitted"
    assert fake.sends == 1
    await bridge.close()


@pytest.mark.parametrize("fast_report", [True, False])
async def test_wrong_crew_ack_invalidates_fast_and_late_worker_reports(
    tmp_path: Path, fast_report: bool
) -> None:
    from codex_herdr_bridge.crew import Crew

    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")

    class WrongAck(Crew):
        async def peer(self, target: str) -> dict[str, Any]:
            return crew_peer()

        async def _request(
            self, method: str, path: str, body: Any = None, headers: Any = None
        ) -> Any:
            if fast_report:
                bridge.complete(body["meta"]["sendId"], "Unverified worker")
            return {"ok": True, "slot": "different-chat", "mid": "wrong"}

    bridge._crew_client = WrongAck()
    result = await bridge.crew_submit("review", "Review", "wrong-ack", expected=crew_peer())
    assert result["status"] == "uncertain"
    assert result["metadata"]["identity_invalidated"] is True
    assert result["result"] is None
    with pytest.raises(ValueError, match="identity"):
        bridge.complete(result["id"], "Unverified worker")
    await bridge.close()
