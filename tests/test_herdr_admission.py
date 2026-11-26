"""Herdr TUI admission and recipient identity, including T-78 regressions."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from answerback_bridge import receipt_version
from answerback_bridge.adapters.contracts import Peer, TaskReceipt
from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError
from answerback_bridge.providers.herdr import herdr as herdr_client
from answerback_bridge.service import Bridge


class FakeHerdr:
    """Herdr TUI stand-in: one agent, counts every prompt delivery."""

    def __init__(self, agent: dict[str, Any]) -> None:
        self.agent = agent
        self.sends = 0
        self.checks = 0
        self.gets: list[str] = []
        self.on_prompt: Callable[[], None] | None = None

    async def check(self) -> str:
        self.checks += 1
        return "fixture"

    async def get(self, target: str) -> dict[str, Any]:
        self.gets.append(target)
        return dict(self.agent)

    async def call(self, *args: str) -> dict[str, Any]:
        if args[:2] == ("agent", "list"):
            return {"agents": [dict(self.agent)]}
        self.sends += 1
        receipt = {"agent": {"terminal_id": self.agent.get("terminal_id")}}
        if self.on_prompt is not None:
            self.on_prompt()
        return receipt


def busy_bridge(tmp_path: Path, agent: dict[str, Any]) -> tuple[Bridge, FakeHerdr]:
    bridge = Bridge(Config("test"), tmp_path / "config.json")
    fake = FakeHerdr(agent)
    bridge.herdr = fake
    return bridge, fake


async def test_busy_herdr_agent_is_refused_before_reservation_and_request_id_survives(
    tmp_path: Path,
) -> None:
    bridge, fake = busy_bridge(
        tmp_path,
        {"name": "worker", "terminal_id": "t1", "agent_status": "working", "agent_session": "s1"},
    )
    try:
        peer = (await bridge.peer_search("worker", provider="herdr"))["peers"][0]
        with pytest.raises(ValueError, match="not ready"):
            await bridge.peer_submit(
                "worker", "Review", "r1", provider="herdr", binding=peer["binding"]
            )
        assert bridge.store.by_request("r1") is None
        assert fake.sends == 0
        fake.agent["agent_status"] = "idle"
        result = await bridge.peer_submit(
            "worker", "Review", "r1", provider="herdr", binding=peer["binding"]
        )
        assert result["status"] == "submitted"
        assert fake.sends == 1
    finally:
        await bridge.close()


async def test_missing_terminal_id_is_refused_before_reservation(tmp_path: Path) -> None:
    bridge, fake = busy_bridge(tmp_path, {"name": "worker", "agent_status": "idle"})
    try:
        adapter = bridge.adapters.get("herdr")
        with pytest.raises(ValueError, match="terminal identity"):
            await adapter.submit_native("worker", "Review", "r2", {"terminal_id": "t1"})
        assert bridge.store.by_request("r2") is None
        assert fake.sends == 0
    finally:
        await bridge.close()


async def test_done_herdr_agent_is_accepted(tmp_path: Path) -> None:
    bridge, fake = busy_bridge(
        tmp_path,
        {"name": "worker", "terminal_id": "t1", "agent_status": "done", "agent_session": "s1"},
    )
    try:
        peer = (await bridge.peer_search("worker", provider="herdr"))["peers"][0]
        result = await bridge.peer_submit(
            "worker", "Review", "r3", provider="herdr", binding=peer["binding"]
        )
        assert result["status"] == "submitted"
        assert fake.sends == 1
    finally:
        await bridge.close()


@pytest.fixture
async def herdr_case(tmp_path: Path) -> AsyncIterator[tuple[Bridge, FakeHerdr]]:
    bridge, fake = busy_bridge(
        tmp_path,
        {
            "name": "worker",
            "terminal_id": "t1",
            "agent_session": "s1",
            "agent": "claude",
            "agent_status": "idle",
        },
    )
    try:
        yield bridge, fake
    finally:
        await bridge.close()


async def selected_peer(bridge: Bridge) -> dict[str, Any]:
    return (await bridge.peer_search("worker", provider="herdr"))["peers"][0]


@pytest.mark.parametrize("field", ["agent", "display_agent", "agent_kind", "kind"])
@pytest.mark.parametrize("live_kind", ["kiro", ""])
async def test_herdr_kind_drift_is_refused_before_reservation(
    herdr_case: tuple[Bridge, FakeHerdr], field: str, live_kind: str
) -> None:
    """red_at_base: card 1, a reused terminal is not proof of the same agent."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    fake.agent.pop("agent")
    fake.agent[field] = live_kind
    with pytest.raises(ValueError, match="Selected Herdr identity changed.*search again"):
        await bridge.herdr_submit("worker", "Review", "kind-race", expected=expected)
    assert fake.sends == 0
    assert bridge.store.counts() == {}


@pytest.mark.parametrize("field", ["agent", "display_agent", "agent_kind", "kind"])
async def test_herdr_binding_refuses_kind_only_replacement(
    herdr_case: tuple[Bridge, FakeHerdr], field: str
) -> None:
    """red_at_base: card 1, the cross-call binding includes the resolved kind."""
    bridge, fake = herdr_case
    fake.agent.pop("agent")
    fake.agent[field] = "claude"
    expected = await selected_peer(bridge)
    fake.agent[field] = "kiro"
    with pytest.raises(ValueError, match="Selected peer changed since discovery; search again"):
        await bridge.peer_submit(
            "worker", "Review", "binding-race", provider="herdr", binding=expected["binding"]
        )
    assert fake.sends == 0
    assert bridge.store.counts() == {}
    fake.agent[field] = "claude"
    result = await bridge.peer_submit(
        "worker", "Review", "binding-race", provider="herdr", binding=expected["binding"]
    )
    assert result["status"] == "submitted"
    assert fake.sends == 1


@pytest.mark.parametrize(
    "kind_fields",
    [
        {"agent": "claude", "display_agent": "Claude", "agent_kind": "other", "kind": "other"},
        {"agent": "", "display_agent": "claude", "agent_kind": "other", "kind": "other"},
        {"agent_kind": "claude", "kind": "other"},
        {"kind": "claude"},
    ],
)
async def test_herdr_kind_aliases_preserve_valid_delivery(
    herdr_case: tuple[Bridge, FakeHerdr], kind_fields: dict[str, str]
) -> None:
    """pins_existing: card 1, native kind aliases retain discovery precedence."""
    bridge, fake = herdr_case
    fake.agent.pop("agent")
    fake.agent.update(kind_fields)
    expected = await selected_peer(bridge)
    assert expected["agent_kind"] == "claude"
    result = await bridge.herdr_submit("worker", "Review", "same-kind", expected=expected)
    assert result["status"] == "submitted"
    assert fake.sends == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent", "kiro"),
        ("agent", ""),
        ("terminal_id", "t2"),
        ("agent_session", "s2"),
        ("agent_session", None),
    ],
)
async def test_herdr_post_send_identity_drift_invalidates_receipt(
    herdr_case: tuple[Bridge, FakeHerdr], field: str, value: str | None
) -> None:
    """red_at_base: card 1, a matching acknowledgement cannot vouch for the live occupant."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    fake.on_prompt = lambda: fake.agent.update({field: value})
    result = await bridge.herdr_submit("worker", "Review", "after-race", expected=expected)
    assert result["status"] == "uncertain"
    assert result["metadata"]["identity_invalidated"] is True
    assert "identity changed after send" in result["detail"]
    assert fake.gets == ["worker", "worker"]
    assert fake.sends == 1
    with pytest.raises(ValueError, match="identity was invalidated"):
        bridge.complete(result["id"], "Unverified result")


@pytest.mark.parametrize("session_fields", [{}, {"agent_session": None}], ids=["missing", "null"])
async def test_herdr_post_send_first_reported_session_keeps_receipt_valid(
    herdr_case: tuple[Bridge, FakeHerdr], session_fields: dict[str, None]
) -> None:
    """pins_existing: card 1, first-reported sessions remain completable; red at review tip."""
    bridge, fake = herdr_case
    fake.agent.pop("agent_session")
    fake.agent.update(session_fields)
    expected = await selected_peer(bridge)
    fake.on_prompt = lambda: fake.agent.update(agent_session="s2")
    result = await bridge.herdr_submit("worker", "Review", "new-session", expected=expected)
    assert result["status"] == "submitted"
    assert not result["metadata"].get("identity_invalidated")
    assert fake.sends == 1
    completed = bridge.complete(result["id"], "Verified result")
    assert completed["status"] == "completed"
    assert completed["result"] == "Verified result"


async def test_herdr_post_send_session_uses_pre_send_observation(
    herdr_case: tuple[Bridge, FakeHerdr],
) -> None:
    """pins_existing: round 2, discovery can precede the session observed before sending."""
    bridge, fake = herdr_case
    fake.agent.pop("agent_session")
    expected = await selected_peer(bridge)
    fake.agent["agent_session"] = "s1"
    fake.on_prompt = lambda: fake.agent.update(agent_session="s2")
    result = await bridge.herdr_submit("worker", "Review", "observed-session", expected=expected)
    assert result["status"] == "uncertain"
    assert result["metadata"]["identity_invalidated"] is True
    assert "identity changed after send" in result["detail"]
    with pytest.raises(ValueError, match="identity was invalidated"):
        bridge.complete(result["id"], "Unverified result")


async def test_herdr_post_send_drift_revokes_fast_completion(
    herdr_case: tuple[Bridge, FakeHerdr],
) -> None:
    """red_at_base: card 1, identity failure must revoke an already recorded worker result."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)

    def finish_then_replace() -> None:
        task = bridge.store.by_request("fast-completion")
        assert task is not None
        bridge.complete(task["id"], "Fast result")
        fake.agent["agent"] = "kiro"

    fake.on_prompt = finish_then_replace
    result = await bridge.herdr_submit("worker", "Review", "fast-completion", expected=expected)
    assert result["status"] == "uncertain"
    assert bridge.store.get(result["id"])["result"] is None
    assert result["metadata"]["identity_invalidated"] is True


@pytest.mark.parametrize("error_type", [BridgeError, OSError, ValueError])
async def test_herdr_post_send_probe_failure_invalidates_receipt(
    herdr_case: tuple[Bridge, FakeHerdr],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """red_at_base: card 1, an unavailable post-send identity cannot certify delivery."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    monkeypatch.setattr(
        fake, "get", AsyncMock(side_effect=[dict(fake.agent), error_type("Fixture lookup failed")])
    )
    result = await bridge.herdr_submit("worker", "Review", "after-error", expected=expected)
    assert result["status"] == "uncertain"
    assert result["metadata"]["identity_invalidated"] is True
    assert result["detail"]
    assert fake.sends == 1


async def test_herdr_post_send_probe_cancellation_invalidates_receipt(
    herdr_case: tuple[Bridge, FakeHerdr], monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: card 1, interrupted verification persists uncertainty and propagates."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    monkeypatch.setattr(
        fake, "get", AsyncMock(side_effect=[dict(fake.agent), asyncio.CancelledError()])
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.herdr_submit("worker", "Review", "after-cancel", expected=expected)
    task = bridge.store.by_request("after-cancel")
    assert task is not None
    assert task["status"] == "uncertain"
    assert task["metadata"]["identity_invalidated"] is True
    assert task["detail"]


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Review\x00this", "Core message must be nonempty text without NUL characters"),
        ("  ", "Core message must be nonempty text without NUL characters"),
        (None, "Core message must be nonempty text without NUL characters"),
        ("\ud800", "Core message must be valid UTF-8 text"),
        ("é" * (64 * 1024), "Core message exceeds the 128 KiB bound"),
        ("é" * (64 * 1024) + "x", "Core message exceeds the 128 KiB bound"),
        ("x" * 100_001, "Message must contain 1–100000 characters"),
    ],
    ids=[
        "nul",
        "blank",
        "nontext",
        "invalid-utf8",
        "byte-bound-exact",
        "byte-bound",
        "character-bound",
    ],
)
async def test_herdr_invalid_message_is_refused_before_any_probe(
    herdr_case: tuple[Bridge, FakeHerdr], message: Any, error: str
) -> None:
    """red_at_base: card 2, invalid text triggers neither provider probes nor reservation."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    with pytest.raises(ValueError, match=error):
        await bridge.herdr_submit("worker", message, "bad-message", expected=expected)
    assert fake.checks == 0
    assert fake.gets == []
    assert fake.sends == 0
    assert bridge.store.counts() == {}
    result = await bridge.herdr_submit("worker", "Review", "bad-message", expected=expected)
    assert result["status"] == "submitted"


async def test_herdr_delivery_value_error_records_uncertainty(
    herdr_case: tuple[Bridge, FakeHerdr],
) -> None:
    """red_at_base: card 2, a delivery ValueError cannot strand a pending reservation."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)

    def fail_delivery() -> None:
        raise ValueError("Fixture delivery failed")

    fake.on_prompt = fail_delivery
    result = await bridge.herdr_submit("worker", "Review", "send-error", expected=expected)
    assert result["status"] == "uncertain"
    assert result["detail"] == "Fixture delivery failed"
    assert bridge.store.by_request("send-error")["status"] == "uncertain"


@pytest.mark.parametrize(
    ("phase", "answer", "error_name", "detail_prefix", "invalidated"),
    [
        ("lookup", [], "AttributeError", "Herdr identity could not be verified after send", True),
        ("lookup", None, "TypeError", "Herdr identity could not be verified after send", True),
        (
            "lookup",
            {"result": None},
            "AttributeError",
            "Herdr identity could not be verified after send",
            True,
        ),
        (
            "prompt",
            {"agent": None},
            "AttributeError",
            "Herdr prompt acknowledgement could not be read",
            True,
        ),
        ("prompt", [], "AttributeError", "Herdr prompt call failed", False),
        ("prompt", None, "TypeError", "Herdr prompt call failed", False),
        (
            "prompt",
            {"result": None},
            "AttributeError",
            "Herdr prompt acknowledgement could not be read",
            True,
        ),
    ],
    ids=[
        "lookup-list",
        "lookup-null",
        "lookup-result-null",
        "ack-agent-null",
        "prompt-list",
        "prompt-null",
        "ack-result-null",
    ],
)
async def test_herdr_malformed_json_settles_reserved_task(
    herdr_case: tuple[Bridge, FakeHerdr],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    answer: Any,
    error_name: str,
    detail_prefix: str,
    invalidated: bool,
) -> None:
    """red_at_base: round 2, malformed native JSON cannot leave a pending reservation."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    answers = [{"agent": dict(fake.agent)}]
    if phase == "lookup":
        answers.append({"agent": {"terminal_id": "t1"}})
    answers.append(answer)
    run = AsyncMock(side_effect=[SimpleNamespace(stdout=json.dumps(value)) for value in answers])
    monkeypatch.setattr(herdr_client, "run", run)
    bridge.herdr = herdr_client.Herdr(bridge.config)
    monkeypatch.setattr(bridge.herdr, "check", AsyncMock(return_value="fixture"))
    result = await bridge.herdr_submit("worker", "Review", "malformed-json", expected=expected)
    assert result["status"] == "uncertain"
    assert result["detail"] == (
        f"{detail_prefix} ({error_name}); delivery is uncertain. "
        "Inspect the target before taking action."
    )
    assert bool(result["metadata"].get("identity_invalidated")) is invalidated
    assert (await bridge.get_task(result["id"])) == result
    assert run.await_count == len(answers)
    if invalidated:
        with pytest.raises(ValueError, match="identity was invalidated"):
            bridge.complete(result["id"], "Unverified result")
    else:
        assert bridge.complete(result["id"], "Worker result")["status"] == "completed"


@pytest.mark.parametrize("phase", ["prompt", "lookup"])
async def test_herdr_unexpected_exception_detail_omits_response_text(
    herdr_case: tuple[Bridge, FakeHerdr], monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """red_at_base: round 2, unexpected errors expose only a fixed detail and type name."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)
    error = RuntimeError("Fixture response fragment must stay private")
    if phase == "prompt":
        monkeypatch.setattr(fake, "call", AsyncMock(side_effect=error))
        prefix = "Herdr prompt call failed"
    else:
        monkeypatch.setattr(fake, "get", AsyncMock(side_effect=[dict(fake.agent), error]))
        prefix = "Herdr identity could not be verified after send"
    result = await bridge.herdr_submit("worker", "Review", "unexpected-error", expected=expected)
    assert result["status"] == "uncertain"
    assert result["detail"] == (
        f"{prefix} (RuntimeError); delivery is uncertain. Inspect the target before taking action."
    )
    assert "Fixture response fragment" not in result["detail"]
    assert bool(result["metadata"].get("identity_invalidated")) is (phase == "lookup")


@pytest.mark.parametrize("phase", ["acknowledgement", "lookup"])
async def test_herdr_unreadable_identity_revokes_fast_completion(
    herdr_case: tuple[Bridge, FakeHerdr], monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """red_at_base: round 2, malformed identity must revoke even a raced worker result."""
    bridge, fake = herdr_case
    expected = await selected_peer(bridge)

    def complete_during_send() -> None:
        task = bridge.store.by_request("unreadable-fast")
        bridge.complete(task["id"], "Fast result")

    if phase == "acknowledgement":

        async def unreadable_ack(*args: str) -> dict[str, Any]:
            complete_during_send()
            return {"agent": None}

        monkeypatch.setattr(fake, "call", unreadable_ack)
    else:
        fake.on_prompt = complete_during_send
        monkeypatch.setattr(
            fake, "get", AsyncMock(side_effect=[dict(fake.agent), TypeError("Fixture lookup")])
        )
    result = await bridge.herdr_submit("worker", "Review", "unreadable-fast", expected=expected)
    assert result["status"] == "uncertain"
    assert result["result"] is None
    assert result["metadata"]["identity_invalidated"] is True
    with pytest.raises(ValueError, match="identity was invalidated"):
        bridge.complete(result["id"], "Unverified result")


@pytest.mark.parametrize("message", ["x" * 100_000, "é" * (64 * 1024 - 1) + "x"])
async def test_herdr_valid_message_bounds_are_preserved(
    herdr_case: tuple[Bridge, FakeHerdr], message: str
) -> None:
    """pins_existing: card 2, preserve valid ASCII and multibyte message boundaries.

    The second text is 131,071 bytes of UTF-8, the largest text that is accepted.
    """
    bridge, _ = herdr_case
    expected = await selected_peer(bridge)
    result = await bridge.herdr_submit("worker", message, "valid-message", expected=expected)
    assert result["status"] == "submitted"


@pytest.mark.parametrize(
    ("current", "stored"),
    [("t1", "t2"), ("t1", None), (None, None), ("", ""), (1, 1)],
)
async def test_herdr_matches_requires_terminal_identity(
    herdr_case: tuple[Bridge, FakeHerdr], current: Any, stored: Any
) -> None:
    """red_at_base: card 3, a reusable name or absent terminal identity is insufficient."""
    bridge, _ = herdr_case
    peer = Peer.from_record({**await selected_peer(bridge), "terminal_id": current})
    task, _ = bridge.store.reserve(
        "herdr:test:old",
        "Review",
        "old-terminal",
        {
            "kind": "herdr",
            "target": "worker",
            "terminal_id": stored,
            "agent_kind": "claude",
            "agent_session": "s1",
        },
    )
    assert not bridge.adapters.get("herdr").matches(peer, TaskReceipt.from_record(task))


@pytest.mark.parametrize("name_field", ["target", "slot", "alias"])
async def test_herdr_matches_keeps_same_terminal_receipts(
    herdr_case: tuple[Bridge, FakeHerdr], name_field: str
) -> None:
    """pins_existing: card 3, retain matching identities under each supported name field."""
    bridge, _ = herdr_case
    peer = Peer.from_record(await selected_peer(bridge))
    task, _ = bridge.store.reserve(
        "herdr:test:t1",
        "Review",
        "same-terminal",
        {
            "kind": "herdr",
            name_field: "worker",
            "terminal_id": "t1",
            "agent_kind": "claude",
            "agent_session": "s1",
        },
    )
    adapter = bridge.adapters.get("herdr")
    assert adapter.matches(peer, TaskReceipt.from_record(task))
    task["metadata"]["kind"] = "crew"
    assert not adapter.matches(peer, TaskReceipt.from_record(task))
    task["metadata"].update(kind="herdr", **{name_field: "different-worker"})
    assert not adapter.matches(peer, TaskReceipt.from_record(task))


async def test_herdr_task_for_peer_excludes_previous_terminal(
    herdr_case: tuple[Bridge, FakeHerdr],
) -> None:
    """red_at_base: card 3, the public lookup must not return a previous occupant's result."""
    bridge, fake = herdr_case
    receipt = await bridge.peer_submit(
        "worker",
        "Review",
        "old-task",
        provider="herdr",
        binding=(await selected_peer(bridge))["binding"],
    )
    bridge.complete(receipt["id"], "Earlier occupant")
    assert (await bridge.task_for_peer("herdr:worker"))["id"] == receipt["id"]
    fake.agent["terminal_id"] = "t2"
    with pytest.raises(ValueError, match="No receipt for this peer"):
        await bridge.task_for_peer("herdr:worker")


@pytest.mark.parametrize(
    ("field", "value"), [("agent", "kiro"), ("agent_session", "s2"), ("agent_session", None)]
)
async def test_herdr_task_for_peer_excludes_replaced_occupant(
    herdr_case: tuple[Bridge, FakeHerdr], field: str, value: str | None
) -> None:
    """red_at_base: round 2, a new kind or known session in the same terminal is distinct."""
    bridge, fake = herdr_case
    receipt = await bridge.peer_submit(
        "worker",
        "Review",
        "old-occupant",
        provider="herdr",
        binding=(await selected_peer(bridge))["binding"],
    )
    bridge.complete(receipt["id"], "Earlier occupant")
    fake.agent[field] = value
    with pytest.raises(ValueError, match="No receipt for this peer"):
        await bridge.task_for_peer("herdr:worker")
    assert (await bridge.get_task(receipt["id"]))["result"] == "Earlier occupant"


@pytest.mark.parametrize(
    ("kind", "before_session", "after_session"),
    [("claude", None, "s2"), ("claude", "s1", "s1"), ("claude", "", ""), ("", None, "s2")],
    ids=["first-session", "same-session", "empty-session", "empty-kind"],
)
async def test_herdr_receipt_stores_pre_send_identity_and_keeps_valid_lookup(
    herdr_case: tuple[Bridge, FakeHerdr],
    kind: str,
    before_session: str | None,
    after_session: str,
) -> None:
    """red_at_base: round 2, stored native identity permits unchanged/first-session lookups."""
    bridge, fake = herdr_case
    fake.agent["agent"] = kind
    fake.agent.pop("agent_session")
    expected = await selected_peer(bridge)
    fake.agent["agent_session"] = before_session
    reserved_metadata = []

    def observe_reservation() -> None:
        reserved_metadata.append(bridge.store.by_request("stored-identity")["metadata"])
        fake.agent["agent_session"] = after_session

    fake.on_prompt = observe_reservation
    receipt = await bridge.herdr_submit("worker", "Review", "stored-identity", expected=expected)
    assert receipt["status"] == "submitted"
    identity = {
        "kind": "herdr",
        "target": "worker",
        "terminal_id": "t1",
        "agent_kind": kind,
        "agent_session": before_session,
    }
    assert reserved_metadata == [{**identity, receipt_version.CREATED: receipt_version.current()}]
    bridge.complete(receipt["id"], "Verified result")
    public = await bridge.get_task(receipt["id"])
    assert public["metadata"] == identity
    assert (await bridge.task_for_peer("herdr:worker")) == public


@pytest.mark.parametrize("kind", ["claude", ""])
async def test_herdr_receipt_without_stored_kind_is_available_only_by_id(
    herdr_case: tuple[Bridge, FakeHerdr], kind: str
) -> None:
    """red_at_base: round 2, an absent stored kind differs from an explicitly empty kind."""
    bridge, fake = herdr_case
    fake.agent["agent"] = kind
    task, _ = bridge.store.reserve(
        "herdr:test:t1",
        "Review",
        "legacy-receipt",
        {"kind": "herdr", "target": "worker", "terminal_id": "t1"},
    )
    bridge.complete(task["id"], "Legacy result")
    assert (await bridge.get_task(task["id"]))["result"] == "Legacy result"
    peer = Peer.from_record(await selected_peer(bridge))
    assert not bridge.adapters.get("herdr").matches(peer, TaskReceipt.from_record(task))
    with pytest.raises(ValueError, match="No receipt for this peer"):
        await bridge.task_for_peer("herdr:worker")
