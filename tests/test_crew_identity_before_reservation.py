"""The Crew adapter compares the caller's identity with the live chat before any reservation.

R2-1FV findings 1 and 2: `CrewAdapter.submit_native` looked the chat up before
reserving but discarded the result, so the identity comparison ran only inside
`Crew.submit`, after `store.reserve`. An identity the caller never discovered
(`{}`, another slot's peer, a record missing a field, a different origin) left an
`uncertain`, `identity_invalidated` receipt row for a delivery that never happened.
The adapter now runs the comparison the Crew client itself uses, against the live
peer from its pre-reservation lookup, before `admission_guard` and `store.reserve`:
no row, no send, and the same refusal shape the Herdr adapter raises.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.crew import Crew, CrewIdentityChanged, _peer, check_identity
from answerback_bridge.service import Bridge

CREW_REFUSAL = "Selected Crew identity changed or is unavailable; search again"
HERDR_REFUSAL = "Selected Herdr identity changed or is unavailable; search again"
ORIGIN = "http://127.0.0.1:9"


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "key": "review",
        "title": "Reviewer chat",
        "project": "/tmp/r21f2",
        "agent": "kiro",
        "created": 11.0,
        "running": False,
        **changes,
    }


class _Crew:
    """Crew stand-in: one chat row; counts lookups and sends apart. `listed`, when set,
    is what discovery saw, so the live row can differ from the discovered one."""

    origin = ORIGIN

    def __init__(self) -> None:
        self.row = _row()
        self.listed: dict[str, Any] | None = None
        self.lookups = 0
        self.sends = 0
        self.delivered: dict[str, Any] | None = None

    async def peers(self) -> list[dict[str, Any]]:
        self.lookups += 1
        return [self.listed or _peer(self.row, self.origin)]

    async def peer(self, target: str) -> dict[str, Any]:
        self.lookups += 1
        return _peer(self.row, self.origin)

    async def submit(
        self, target: str, message: str, send_id: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        self.sends += 1
        self.delivered = expected
        return {"ok": True, "slot": target}


class _Herdr:
    """Herdr stand-in whose `agent list` names the discovered occupant and `agent get`
    the one holding the terminal now; the two differ when `replaced` is set."""

    def __init__(self) -> None:
        self.replaced = False
        self.sends = 0

    def _agent(self, terminal_id: str) -> dict[str, Any]:
        return {
            "name": "worker",
            "terminal_id": terminal_id,
            "agent_session": "sess-a",
            "agent_status": "idle",
        }

    async def check(self) -> str:
        return "fixture"

    async def get(self, target: str) -> dict[str, Any]:
        return self._agent("term-b" if self.replaced else "term-a")

    async def call(self, *args: str) -> dict[str, Any]:
        if args[:2] == ("agent", "list"):
            return {"agents": [self._agent("term-a")]}
        self.sends += 1
        return {"agent": {"terminal_id": "term-a"}}


def _watch_reserves(bridge: Bridge) -> list[int]:
    """Count `store.reserve` calls: a refusal before reservation must leave this at 0."""
    calls = [0]
    original = bridge.store.reserve

    def reserve(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return original(*args, **kwargs)

    bridge.store.reserve = reserve  # type: ignore[method-assign]
    return calls


def _bridge(tmp_path: Path) -> tuple[Bridge, _Crew, _Herdr, list[int]]:
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")
    crew, herdr = _Crew(), _Herdr()
    bridge._crew_client = crew
    bridge.herdr = herdr
    return bridge, crew, herdr, _watch_reserves(bridge)


def _refused_before_reservation(
    bridge: Bridge, crew: _Crew, reserves: list[int], request_id: str
) -> None:
    assert crew.lookups == 1, "exactly the one pre-reservation lookup of the live chat"
    assert reserves[0] == 0
    assert crew.sends == 0
    assert bridge.store.by_request(request_id) is None
    assert bridge.store.counts() == {}


def _without(record: dict[str, Any], name: str) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != name}


DISCOVERED = _peer(_row(), ORIGIN)
OTHER_SLOT = _peer(
    _row(key="other", title="Other chat", project="/tmp/other", created=22.0), ORIGIN
)
# Another slot named with every other identity field of the discovered chat: created,
# project, agent and origin all match, so only the slot-key clause of the comparison
# (the caller's own slot must be the submit target, crew.py `expected_key != target`)
# refuses it. OTHER_SLOT above also differs in created and project, so it cannot pin
# that clause (R2-1F2V finding 1). The slot is read from `target`, else from `key`.
OTHER_SLOT_SAME_FIELDS: list[tuple[str, dict[str, Any]]] = [
    ("other_slot_same_fields", {**DISCOVERED, "target": "other"}),
    ("other_slot_same_fields_key", {**_without(DISCOVERED, "target"), "key": "other"}),
]
UNDISCOVERED: list[tuple[str, dict[str, Any]]] = [
    ("empty", {}),
    ("other_slot", OTHER_SLOT),
    ("missing_created", _without(DISCOVERED, "created")),
    ("missing_project", _without(DISCOVERED, "project")),
    ("missing_agent", _without(DISCOVERED, "agent")),
    # The live chat names an origin, so a record without one is incomplete evidence.
    ("missing_origin", _without(DISCOVERED, "origin")),
    ("different_origin", {**DISCOVERED, "origin": "http://127.0.0.1:10"}),
    *OTHER_SLOT_SAME_FIELDS,
]
CASE_IDS = [name for name, _ in UNDISCOVERED]


@pytest.mark.parametrize(("case", "expected"), UNDISCOVERED, ids=CASE_IDS)
async def test_crew_adapter_refuses_an_undiscovered_identity_before_reservation(
    tmp_path: Path, case: str, expected: dict[str, Any]
) -> None:
    bridge, crew, _, reserves = _bridge(tmp_path)
    guarded: list[str] = []
    try:
        with pytest.raises(ValueError, match=f"^{re.escape(CREW_REFUSAL)}$"):
            await bridge.adapters.get("crew").submit_native(
                "review",
                "Review",
                f"adapter-{case}",
                expected,
                admission_guard=lambda: guarded.append(case),
            )
        _refused_before_reservation(bridge, crew, reserves, f"adapter-{case}")
        # The comparison runs before admission, as the Herdr adapter's does.
        assert guarded == []
    finally:
        await bridge.close()


@pytest.mark.parametrize(("case", "expected"), UNDISCOVERED, ids=CASE_IDS)
async def test_crew_submit_refuses_an_undiscovered_identity_before_reservation(
    tmp_path: Path, case: str, expected: dict[str, Any]
) -> None:
    bridge, crew, _, reserves = _bridge(tmp_path)
    try:
        with pytest.raises(ValueError, match=f"^{re.escape(CREW_REFUSAL)}$"):
            await bridge.crew_submit("review", "Review", f"bridge-{case}", expected=expected)
        _refused_before_reservation(bridge, crew, reserves, f"bridge-{case}")
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    ("case", "expected"),
    OTHER_SLOT_SAME_FIELDS,
    ids=[name for name, _ in OTHER_SLOT_SAME_FIELDS],
)
def test_check_identity_refuses_another_slot_whose_other_identity_fields_match(
    case: str, expected: dict[str, Any]
) -> None:
    # The public wrapper the adapter calls, on the live chat it looked up: the caller's
    # record matches it in created, project, agent and origin and names another slot.
    same = ("created", "project", "agent", "origin")
    assert {name: expected[name] for name in same} == {name: DISCOVERED[name] for name in same}
    with pytest.raises(CrewIdentityChanged):
        check_identity("review", expected, _peer(_row(), ORIGIN))


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        # `target` is the slot compared; `key` is read only when `target` is absent, so
        # a record whose target names another slot is refused whatever its key says.
        ({**DISCOVERED, "target": "other", "key": "review"}, DISCOVERED),
        # The live record must itself be the submit target's chat.
        (DISCOVERED, {**DISCOVERED, "target": "other"}),
        # A live record without an origin cannot match a record that names one.
        (DISCOVERED, _without(DISCOVERED, "origin")),
    ],
    ids=["target_names_other_slot_key_names_this", "live_names_other_slot", "live_without_origin"],
)
def test_check_identity_refuses_a_slot_or_origin_mismatch(
    expected: dict[str, Any], actual: dict[str, Any]
) -> None:
    with pytest.raises(CrewIdentityChanged):
        check_identity("review", expected, actual)


def test_check_identity_accepts_the_discovered_chat_named_by_key() -> None:
    # Without `target`, the slot is read from `key` (the field a raw Crew chat row uses),
    # so the discovered identity in that form is the same chat and is not refused.
    check_identity("review", {**_without(DISCOVERED, "target"), "key": "review"}, DISCOVERED)


async def test_crew_submit_still_delivers_the_discovered_identity(tmp_path: Path) -> None:
    bridge, crew, _, reserves = _bridge(tmp_path)
    # Renamed since discovery: the identity fields match, so it is the same chat, but
    # the live record differs, so substituting it for the discovered one shows.
    crew.row = _row(title="Renamed chat")
    try:
        result = await bridge.crew_submit("review", "Review", "ok-bridge", expected=DISCOVERED)
        assert result["status"] == "submitted"
        assert reserves[0] == 1
        assert crew.sends == 1
        row = bridge.store.by_request("ok-bridge")
        assert row is not None
        stored = bridge.store.get(row["id"])
        # The row and the send carry exactly the discovered identity, never the live copy.
        assert stored["metadata"]["peer"] == DISCOVERED
        assert crew.delivered == DISCOVERED
        assert not stored["metadata"].get("identity_invalidated")
    finally:
        await bridge.close()


async def test_peer_submit_with_provider_crew_reserves_once_and_sends_once(
    tmp_path: Path,
) -> None:
    bridge, crew, _, reserves = _bridge(tmp_path)
    try:
        result = await bridge.peer_submit(
            "Reviewer chat", "Review", "ok-peer", provider="crew", binding=DISCOVERED["binding"]
        )
        assert result["status"] == "submitted"
        assert reserves[0] == 1
        assert crew.sends == 1
        row = bridge.store.by_request("ok-peer")
        assert row is not None
        assert bridge.store.get(row["id"])["metadata"]["peer"]["binding"] == DISCOVERED["binding"]
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    ("provider", "selector", "refusal"),
    [("herdr", "worker", HERDR_REFUSAL), ("crew", "Reviewer chat", CREW_REFUSAL)],
)
async def test_peer_submit_surfaces_both_providers_pre_reservation_refusals_the_same_way(
    tmp_path: Path, provider: str, selector: str, refusal: str
) -> None:
    # A chat or terminal replaced between peer_submit's discovery and the adapter's
    # own live lookup is refused before any reservation, with a ValueError ending in
    # "search again" for either provider, so a caller handles both alike.
    bridge, crew, herdr, reserves = _bridge(tmp_path)
    try:
        if provider == "crew":
            crew.listed = DISCOVERED
            crew.row = _row(created=99.0)
        else:
            herdr.replaced = True
        peer = (await bridge.peer_search(selector, provider=provider))["peers"][0]
        with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
            await bridge.peer_submit(
                selector, "Review", f"race-{provider}", provider=provider, binding=peer["binding"]
            )
        assert reserves[0] == 0
        assert crew.sends == 0 and herdr.sends == 0
        assert bridge.store.by_request(f"race-{provider}") is None
        assert bridge.store.counts() == {}
    finally:
        await bridge.close()


class _RaceCrew(Crew):
    """The real `Crew.submit`; `peer` answers the discovered chat once, then a replacement,
    so the identity changes between the adapter's lookup and the send."""

    def __init__(self) -> None:
        super().__init__()
        self.origin = ORIGIN
        self.lookups = 0
        self.posts = 0

    async def peer(self, target: str) -> dict[str, Any]:
        self.lookups += 1
        return _peer(_row() if self.lookups == 1 else _row(created=99.0), self.origin)

    async def _request(self, method: str, path: str, body: Any = None, headers: Any = None) -> Any:
        self.posts += 1
        return {"ok": True, "slot": body["slot"], "mid": "m-1"}


async def test_identity_changed_between_lookup_and_send_is_still_invalidated_after_reservation(
    tmp_path: Path,
) -> None:
    # Regression pin for the check that stays inside Crew.submit: the adapter's lookup
    # matched, so a row was reserved; the client's own send-time check then sees the
    # replacement and the row is invalidated (kept, not deleted), with nothing posted.
    bridge = Bridge(Config("test", crew_enabled=True), tmp_path / "config.json")
    crew = _RaceCrew()
    bridge._crew_client = crew
    reserves = _watch_reserves(bridge)
    try:
        result = await bridge.crew_submit("review", "Review", "race-send", expected=DISCOVERED)
        assert result["status"] == "uncertain"
        assert result["metadata"]["identity_invalidated"] is True
        assert "identity" in result["detail"]
        assert reserves[0] == 1
        assert crew.posts == 0
        assert crew.lookups == 2
    finally:
        await bridge.close()
