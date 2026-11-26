"""T-56 review rounds 7 to 10: which identity the debug toggle takes for another machine.

Round 7 (findings B2 and B3). Only Core emits an identity whose text names
another machine. core.py:22 admits the terminal address
``<machine>/<terminal>``, core.py:285 emits the peer id
``hc:<machine>/<terminal>``, and core_tasks.py:67 builds the receipt target
``hc:<session>:<machine>/<terminal>``. Every other provider's target is its own
opaque text: a Crew chat key (crew.py:112-120) may hold ``/`` and ``@``, and
Crew talks only to a loopback gateway (crew.py:44-67).

Rounds 8 and 9. Opaque text is not the same as local. A Rex terminal id
(rex.py:497) names no host; the Rex inventory reports the host of the server
and of the block (rex.py:343 and the ``binding`` of the same record), and the
debug toggle does not read it. So the rule is: a Core address that names a
machine is refused, and a Rex terminal is not checked for its host. Round 8
refused a Rex terminal that a collector pass had shown on another host (not
one that only the public peer search had returned). Round 9 took
that out again: the case is board row T-100, held for the owner (the
orchestrator's decision of 2026-11-20, not an owner ruling). The tests at the
end of this file pin that known limit, and that it reads nothing remote: the
Rex read of such a terminal is refused whatever lease is held.

Since row T-68. Public discovery shows a Rex terminal only when its
foreground process is shown to belong to this OS user (rex.py:38, rex.py:46,
rex_service.py:411). Rex asks for no process of a block that is not on the
server's own host (rex.py:346), so no scan shows such a terminal; one test
below pins that. The debug toggle is unchanged and still checks no host. The
tests that state this of a terminal on another host, or with no host, now
take a row made by hand: the producers' record without the withholding of row
T-68 (`rex_row_made_without_the_t68_withholding`). They state what the toggle
does given such a row, not that a scan produces one. Row T-100 stays held.
The labels of these tests were measured when discovery still returned the row.

The Crew identities below are built by the producer code itself
(`answerback_bridge.crew._origin` and `_peer`), and the receipts are reserved
with the call adapters/crew.py:117-122 makes. The Core forms are copied from
the lines named beside them.

Each test carries one label:

- ``red_at_pretip``: fails at the lane's tip before the test's round.
- ``pins_existing``: passes at that tip too.

That tip is ``7d65216`` for the round 7 tests, ``62090f3`` for the round 8
tests, ``54f5b71`` for the round 9 tests and ``0458ad4`` for the round 10
test. The tests of rounds 8, 9 and 10 start at the lines that say so.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from events_helpers import Script

from answerback_bridge import rex_service
from answerback_bridge.config import Config
from answerback_bridge.crew import _origin, _peer
from answerback_bridge.events.collector import Collector
from answerback_bridge.events.debug import DebugController, is_remote, local_peer, scope_refusal
from answerback_bridge.process import BridgeError
from answerback_bridge.rex import TERMINAL, Rex, _owned_inventory, _ServerSnapshot
from answerback_bridge.service import Bridge
from answerback_bridge.transport import runtime_dir

REMOTE_TEXT = "Remote peer debug is unavailable until T-29"
# crew.py:112-120 (`_key`) admits any text of 1 to 512 characters without a
# control character, so these are keys a Crew gateway may hand out.
CREW_KEYS = ["project/review", "me@corp", "team/alice@corp"]


def clock() -> float:
    return 702.0


def crew_peer(key: str) -> tuple[str, dict[str, Any]]:
    """The origin and the peer record `Crew` builds for one chat (crew.py:147-176)."""
    origin = _origin("http://localhost:8123")
    row = {
        "key": key,
        "title": "Review",
        "project": "/tmp",
        "agent": "kiro",
        "created": 1.0,
        "running": False,
    }
    return origin, _peer(row, origin)


def crew_receipt(bridge: Bridge, key: str, request_key: str) -> tuple[dict[str, Any], str]:
    """The reservation of adapters/crew.py:117-122, for a peer of `crew_peer`."""
    origin, peer = crew_peer(key)
    task, _ = bridge.store.reserve(
        f"crew:{origin}:{key}",
        "Task T-56. PRIVATE BRIEF",
        request_key,
        {"kind": "crew", "slot": key, "peer": peer, "completion": "worker_report"},
    )
    return task, peer["peer_id"]


@pytest.mark.parametrize("key", CREW_KEYS)
def test_a_crew_key_with_a_slash_or_an_at_sign_is_a_local_identity(key: str) -> None:
    """B2, the classification. Label: red_at_pretip."""
    origin, peer = crew_peer(key)
    assert peer["peer_id"] == f"crew:{key}"  # crew.py:164
    target = f"crew:{origin}:{key}"  # adapters/crew.py:118
    for identity in (peer["peer_id"], target):
        assert is_remote(identity) is False
        assert scope_refusal(identity) is None
        assert scope_refusal(identity, exclusion=True) is None
    assert local_peer(peer["peer_id"]) is True


@pytest.mark.parametrize("key", CREW_KEYS)
async def test_debug_on_a_crew_receipt_with_such_a_key_is_granted(tmp_path: Path, key: str) -> None:
    """B2, the reviewer's probe P1: by receipt, and under `all`. Label: red_at_pretip.

    At 7d65216 `debug on <task id>` answered "Remote peer debug is unavailable
    until T-29; run on its owning node", and an `all` lease did not cover the
    peer. The lane's base c8346ec granted both.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task, peer_id = crew_receipt(bridge, key, "debug-T56-crew")
    for scope in (task["id"], "debug-T56-crew"):
        granted = await bridge.call("debug_on", {"scope": scope})
        assert [entry["scope"] for entry in granted["scopes"]] == [peer_id]
        assert granted["scopes"][0]["observed"] is True
        assert "warning" not in granted
        assert bridge.debug.until(peer_id) is not None
        await bridge.call("debug_off", {})
        assert bridge.debug.until(peer_id) is None

    await bridge.call("debug_on", {"scope": "all"})
    assert bridge.debug.until(peer_id) is not None
    removed = await bridge.call("debug_off", {"scope": task["id"]})
    assert removed["excluded"] == [peer_id]
    assert bridge.debug.until(peer_id) is None

    restarted = DebugController(runtime_dir(bridge.config_path))
    assert restarted.error is None
    assert set(restarted.leases) == {"all"}
    assert restarted.excluded == {peer_id}


@pytest.mark.parametrize("key", CREW_KEYS)
async def test_debug_on_a_typed_crew_address_with_such_a_key_is_granted(
    tmp_path: Path, key: str
) -> None:
    """B2 for an address a user types: granted, with the warning. Label: red_at_pretip.

    Decision D-20261120-07: a well-formed local address is not refused for
    being absent from the scan. With no receipt and no scan the lease is
    unobserved, and the result says so.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    peer_id = crew_peer(key)[1]["peer_id"]
    granted = await bridge.call("debug_on", {"scope": peer_id})
    assert [entry["scope"] for entry in granted["scopes"]] == [peer_id]
    assert granted["scopes"][0]["observed"] is False
    assert granted["warning"].startswith(f"{peer_id} is not in this bridge's current list")
    restarted = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    status = await restarted.call("debug_status", {})
    assert status["error"] is None
    assert [entry["scope"] for entry in status["scopes"]] == [peer_id]
    removed = await restarted.call("debug_off", {"scope": peer_id})
    assert removed["scopes"] == []


# core_tasks.py:67 builds a Core receipt target as "hc:<session or 'default'>:<target>".
# For a terminal on another machine <target> is the "<machine>/<terminal>" of core.py:22.
REMOTE_RECEIPT_TARGET = "hc:default:example-host/t61"
# The same form for a local terminal. core.py:146 admits any session name of 1 to
# 128 characters without a control character, so a session may hold '/' or '@'.
LOCAL_RECEIPT_TARGETS = ["hc:default:t61", "hc:my/session:t61", "hc:ops@corp:t61"]


def test_the_core_receipt_target_of_a_remote_terminal_is_remote() -> None:
    """B3, the classification. Label: red_at_pretip.

    At 7d65216 only the first part of the target was read, which is the session
    name in this form, so the machine prefix behind it went unseen.
    """
    assert is_remote(REMOTE_RECEIPT_TARGET) is True
    assert local_peer(REMOTE_RECEIPT_TARGET) is False
    assert scope_refusal(REMOTE_RECEIPT_TARGET) == REMOTE_TEXT
    assert scope_refusal(REMOTE_RECEIPT_TARGET, exclusion=True) == REMOTE_TEXT
    for session in ("my/session", "ops@corp", "a:b"):
        assert is_remote(f"hc:{session}:example-host/t61") is True


@pytest.mark.parametrize("target", LOCAL_RECEIPT_TARGETS)
def test_the_core_receipt_target_of_a_local_terminal_is_local(tmp_path: Path, target: str) -> None:
    """The other side of B3: the session name is not a machine prefix.

    Label: pins_existing for "hc:default:t61", red_at_pretip for the two session
    names that hold '/' or '@' (7d65216 called them remote).
    """
    assert is_remote(target) is False
    controller = DebugController(tmp_path, clock=clock)
    controller.on("all")
    controller.on(target)
    controller.off("hc:t62")
    restarted = DebugController(tmp_path, clock=clock)
    assert restarted.error is None
    assert set(restarted.leases) == {"all", target}
    assert restarted.until(target) == 1302.0


async def test_a_typed_remote_receipt_target_is_refused_and_stores_nothing(tmp_path: Path) -> None:
    """B3, the reviewer's probe P2, through the service. Label: red_at_pretip.

    No receipt holds this target here, so it reaches `resolve_scope()` as text a
    user typed. At 7d65216 `debug on` granted it with a warning and saved it,
    and `debug off` under an `all` lease saved it as an exclusion.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}; use a local provider:peer$"):
        await bridge.call("debug_on", {"scope": REMOTE_RECEIPT_TARGET})
    assert bridge.debug.leases == {}
    assert not bridge.debug.path.exists()

    await bridge.call("debug_on", {"scope": "all"})
    with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}; use a local provider:peer$"):
        await bridge.call("debug_off", {"scope": REMOTE_RECEIPT_TARGET})
    assert bridge.debug.excluded == set()
    assert bridge.debug.until(REMOTE_RECEIPT_TARGET) is None

    restarted = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    status = await restarted.call("debug_status", {})
    assert status["error"] is None
    assert [entry["scope"] for entry in status["scopes"]] == ["all"]
    assert status["excluded"] == []


def test_the_controller_and_the_loader_refuse_a_remote_receipt_target(tmp_path: Path) -> None:
    """B3 below the service: `on()`, `off()` and a saved file. Label: red_at_pretip."""
    controller = DebugController(tmp_path / "grants", clock=clock)
    controller.on("all")
    for call in (controller.on, controller.off):
        with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}$"):
            call(REMOTE_RECEIPT_TARGET)
    assert set(controller.leases) == {"all"}
    assert controller.excluded == set()

    lease = '{"started": 702.0, "until": 1302.0}'
    files = {
        "lease": '{"leases": {"hc:t61": '
        + lease
        + ', "hc:default:example-host/t61": '
        + lease
        + "}, "
        '"excluded": []}',
        "exclusion": '{"leases": {"all": '
        + lease
        + '}, "excluded": ["hc:default:example-host/t61"]}',
    }
    for name, text in files.items():
        (tmp_path / name / "debug").mkdir(parents=True)
        (tmp_path / name / "debug" / "leases.json").write_text(text)
        restored = DebugController(tmp_path / name, clock=clock)
        assert restored.leases == {}, name
        assert restored.excluded == set(), name
        assert restored.error == "ValueError", name


async def test_a_remote_receipt_without_a_nested_peer_id_is_refused_by_its_target(
    tmp_path: Path,
) -> None:
    """B3 for the column `resolve_scope()` falls back to. Label: red_at_pretip.

    Provenance, stated honestly: core_tasks.py:57-61 always stores the nested
    ``peer``, and `resolve_scope()` prefers its ``peer_id``. This receipt has
    the target of core_tasks.py:67 and no nested peer, which the current code
    does not write; it stands for a receipt that lost or never had the nested
    record. Its target alone must be enough to see that the peer is remote.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task, _ = bridge.store.reserve(
        REMOTE_RECEIPT_TARGET,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-remote-target",
        {"kind": "hc", "target": "example-host/t61", "completion": "worker_report"},
    )
    await bridge.call("debug_on", {"scope": "all"})
    for scope in (task["id"], "debug-T56-remote-target"):
        for method in ("debug_on", "debug_off"):
            with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}; run on its owning node$"):
                await bridge.call(method, {"scope": scope})
    assert set(bridge.debug.leases) == {"all"}
    assert bridge.debug.excluded == set()


@pytest.mark.parametrize("target", LOCAL_RECEIPT_TARGETS[1:])
async def test_a_typed_local_receipt_target_with_a_marked_session_is_granted(
    tmp_path: Path, target: str
) -> None:
    """The cost of B3 that must not be paid: a local session. Label: red_at_pretip."""
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    granted = await bridge.call("debug_on", {"scope": target})
    assert [entry["scope"] for entry in granted["scopes"]] == [target]
    assert granted["scopes"][0]["observed"] is False


@pytest.mark.parametrize(
    ("identity", "remote"),
    [
        ("hc:t61", False),  # core.py:285, a local terminal
        ("hc:example-host/t61", True),  # core.py:285, a terminal on another machine
        ("hc:user@host", True),  # typed text; tests/test_debug.py pins it as remote
        ("hc:user@host/t61", True),  # the same
        ("herdr:main:term_1", False),  # adapters/herdr.py:117
        ("kiro:work:/Users/me@corp/project", False),  # workers.py:182
        ("rex:terminal:session-1/block-1", False),  # rex.py:497
        ("desktop:3f0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44", False),  # adapters/codex.py:230
        ("fixtureprov:thread-1", False),  # a plugin's receipt target: <provider>:<thread>
        ("example-host/hc:t61", True),  # typed text: no provider name holds '/' or '@'
        ("user@host:hc:t61", True),  # the same
        ("hc:example-host/t61:", True),  # typed text with no address after the last ':'
        ("no-colon-at-all", False),  # not an address; refused for that reason instead
    ],
)
def test_the_classification_of_the_other_identity_forms_is_unchanged(
    identity: str, remote: bool
) -> None:
    """What B2 and B3 must leave as it was. Label: pins_existing, every row."""
    assert is_remote(identity) is remote


# Review round 8: the Rex producer chain. The labels of the tests up to the
# round 9 line are measured against ``62090f3``. The refusal they were written
# beside was removed in round 9; each statement below holds without it.
# Since row T-68 no scan shows a Rex terminal that is not on the server's own
# host. Where a test below needs the scan row of such a terminal, the row is
# made by hand (`rex_row_made_without_the_t68_withholding`).
REX_SESSION = "session:11111111-1111-4111-8111-111111111111"
REX_BLOCK = "block:22222222-2222-4222-8222-222222222222"
REX_ENDPOINT = "unix:///tmp/t56-never-connected.sock"
REX_IDENT = f"rex:terminal:{REX_SESSION}/{REX_BLOCK}"  # rex.py:497
# The diagnostic of row T-68 (rex.py:57). discovery.py:75 puts it into the
# page's ``errors`` under the name adapters/rex.py:55 gives it.
REX_WITHHELD = {
    "rex:0": "Rex terminals withheld: foreground ownership by this OS user is unverified"
}


def rex_bridge(directory: Path, monkeypatch: pytest.MonkeyPatch, block_host: str | None) -> Bridge:
    """A bridge whose Rex host is a stand-in with one terminal block.

    Only the server probe and the native calls are replaced, so no endpoint is
    contacted. The server's host is ``local-host``. ``block_host`` is the host
    the block reports as its own; ``None`` leaves the field out, as a block
    without that fact does.

    A block on the server's own host also has what a real Rex host has for it:
    the ``process`` capability and a foreground process of this OS user, which
    the listing requires since row T-68 (rex.py:38, rex_service.py:411). Rex
    asks for no process of any other block (rex.py:346), so the stand-in of
    such a block is unchanged.
    """
    on_server_host = block_host == "local-host"
    snapshot = _ServerSnapshot(
        REX_ENDPOINT,
        "local-host",
        "instance",
        11,
        frozenset({"discovery", "read", "process"} if on_server_host else {"discovery", "read"}),
    )
    native = Rex(server=REX_ENDPOINT, expected_host_id="local-host")
    block = {"block_id": REX_BLOCK, "creator_name": TERMINAL, "label": "A block"}
    if block_host is not None:
        block["host_id"] = block_host

    async def probe() -> _ServerSnapshot:
        return snapshot

    async def same(scope: object) -> None:
        assert scope == snapshot.scope

    async def call(method: str, **kwargs: Any) -> dict[str, Any]:
        answers = {
            "session.list": {"sessions": [{"session_id": REX_SESSION}]},
            "session.list_blocks": {"blocks": [{"block_id": REX_BLOCK, "creator_name": TERMINAL}]},
            "session.describe_block": {"block": block},
        }
        if on_server_host:
            answers[TERMINAL + ".process"] = {
                "foreground": {"pid": 4242, "name": "zsh", "user_id": os.getuid()}
            }
        return answers[method]

    monkeypatch.setattr(native, "_probe", probe)
    monkeypatch.setattr(native, "_check_collection_identity", same)
    monkeypatch.setattr(native, "_call", call)
    config = Config(
        provider_selection=["rex"],
        rex_enabled=True,
        rex_server=REX_ENDPOINT,
        rex_host_id="local-host",
    )
    bridge = Bridge(config, directory / "config.json")
    bridge.rex.adapter = native
    bridge.rex.adapter_binding = (config.rex_binary, config.rex_server, config.rex_host_id)
    return bridge


async def rex_scan(
    directory: Path, monkeypatch: pytest.MonkeyPatch, block_host: str | None
) -> tuple[Bridge, dict[str, Any]]:
    """A bridge of `rex_bridge`, and its real discovery page.

    This is the reviewer's probe (T-56-review-codex-3.md, appendix A), taken one
    step further out: the page is the result of `Bridge.peer_search()`, the call
    the collector's peer source makes (collector.py, ``peers``). The real
    `Directory.search`, `RexDiscovery.discover`, `RexService.inventory`,
    `Rex.inventory` and `Rex._terminal` run, and so does the ownership filter
    of row T-68.

    For a block on the server's own host the page holds the one terminal. For
    a block on another host, or with no host, it holds no peer and the
    diagnostic of row T-68: since that row discovery shows no such terminal.
    """
    bridge = rex_bridge(directory, monkeypatch, block_host)
    page = await bridge.peer_search()  # What the collector's peer source calls.
    if block_host == "local-host":
        assert [peer["peer_id"] for peer in page["peers"]] == [REX_IDENT]
    else:
        assert page["peers"] == []
        assert page["errors"] == REX_WITHHELD
    return bridge, page


async def rex_row_made_without_the_t68_withholding(
    directory: Path, monkeypatch: pytest.MonkeyPatch, block_host: str | None
) -> tuple[Bridge, dict[str, Any]]:
    """A bridge, and a page MADE BY HAND that holds the terminal no scan shows.

    Provenance. The row is the producers' record without the withholding of
    row T-68, and nothing else is changed. The same real calls as in
    `rex_scan` build it. One thing is out of force while they do: the
    ownership filter that `RexService.inventory` applies to the public listing
    (rex_service.py:411), replaced under the name that line looks up,
    ``rex_service._owned_inventory``. No foreground process is faked: Rex
    reports none for such a block, and neither does the stand-in. Since row
    T-68 no scan shows this row. A test that takes it states what the debug
    toggle does given such a row.

    First the real discovery of the same bridge is held to show no peer and to
    carry the diagnostic (`rex_scan`). The filter is back in force before this
    returns, so the bridge the test goes on with runs the real one.
    """
    assert block_host != "local-host"
    bridge, _ = await rex_scan(directory, monkeypatch, block_host)
    with monkeypatch.context() as unfiltered:
        unfiltered.setattr(rex_service, "_owned_inventory", lambda inventory: inventory)
        page = await bridge.peer_search()
    assert rex_service._owned_inventory is _owned_inventory
    assert [peer["peer_id"] for peer in page["peers"]] == [REX_IDENT]
    return bridge, page


async def observe(bridge: Bridge, page: dict[str, Any]) -> Collector:
    """One slow pass of the collector over ``page``: how a scan reaches the bridge."""
    script = Script()
    script.scan = page
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    return observer


async def test_a_local_rex_terminal_keeps_its_lease_although_its_id_holds_a_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Rex terminal on the server's own host: lease, ``all`` and exclusion.

    Label: pins_existing. Every Rex terminal id holds a '/' between the session
    and the block (rex.py:497); the '/' says nothing about the host.
    """
    bridge, page = await rex_scan(tmp_path, monkeypatch, "local-host")
    assert page["peers"][0]["local"] is True
    assert "/" in REX_IDENT
    observer = await observe(bridge, page)

    granted = await bridge.call("debug_on", {"scope": REX_IDENT})
    assert [entry["scope"] for entry in granted["scopes"]] == [REX_IDENT]
    assert granted["scopes"][0]["observed"] is True
    assert "warning" not in granted
    assert observer._debugging(REX_IDENT) is True
    await bridge.call("debug_off", {})

    await bridge.call("debug_on", {"scope": "all"})
    assert bridge.debug.until(REX_IDENT) is not None
    removed = await bridge.call("debug_off", {"scope": REX_IDENT})
    assert removed["excluded"] == [REX_IDENT]


@pytest.mark.parametrize("block_host", ["remote-host", None], ids=["another-host", "no-host"])
async def test_since_t68_discovery_shows_no_rex_terminal_that_is_not_on_the_servers_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_host: str | None
) -> None:
    """Public discovery returns no peer for such a block, and says why.

    Row T-68. Rex asks for a foreground process only of a block on the
    server's own host (rex.py:346), so a block on another host, or with no
    host, has no ownership evidence, and rex.py:48 withholds it. This says
    nothing of the debug toggle.
    """
    _, page = await rex_scan(tmp_path, monkeypatch, block_host)
    assert page["peers"] == []
    assert page["errors"] == REX_WITHHELD


async def test_a_rex_terminal_whose_host_is_not_reported_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Rex terminal whose block reports no host is granted. Label: pins_existing.

    rex.py:343 sets ``local`` to false for a block without a host id too. The
    debug toggle reads neither field: a Rex terminal is not checked for its
    host (row T-100). The collector has projected the terminal, so the lease
    is ``observed`` and carries no warning (`Bridge._observed`).

    The row is made by hand: the producers' record without the withholding of
    row T-68. Since T-68 no scan shows it. The statement is about the toggle,
    given such a row.
    """
    bridge, page = await rex_row_made_without_the_t68_withholding(tmp_path, monkeypatch, None)
    peer = page["peers"][0]
    assert peer["local"] is False
    assert peer["binding"]["block_host_id"] is None
    await observe(bridge, page)

    granted = await bridge.call("debug_on", {"scope": REX_IDENT})
    assert [entry["scope"] for entry in granted["scopes"]] == [REX_IDENT]
    assert granted["scopes"][0]["observed"] is True
    assert "warning" not in granted
    await bridge.call("debug_off", {})
    await bridge.call("debug_on", {"scope": "all"})
    assert bridge.debug.until(REX_IDENT) is not None


async def test_a_rex_address_outside_the_peer_projection_is_granted_with_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not projected and in no receipt: the hint, not a refusal. Label: pins_existing.

    `rex_scan()` has called `Bridge.peer_search()`, the real discovery, and
    since row T-68 it has not returned the terminal: the block is on another
    host. So a collector has nothing to project, the id is not in the peer
    projection (``events.state["peers"]``), and no receipt names it.
    That, and nothing about a host, is what ``observed: false`` and the
    warning say (`Bridge._observed`; decision D-20261120-07).
    """
    bridge, _ = await rex_scan(tmp_path, monkeypatch, "remote-host")
    assert REX_IDENT not in bridge.events.state["peers"]
    granted = await bridge.call("debug_on", {"scope": REX_IDENT})
    assert [entry["scope"] for entry in granted["scopes"]] == [REX_IDENT]
    assert granted["scopes"][0]["observed"] is False
    assert granted["warning"] == (
        f"{REX_IDENT} is not in this bridge's current list of discovered peers. The list "
        "is a snapshot and can be incomplete, so this does not show that the peer is "
        "absent. The lease takes effect if a run of this peer appears."
    )


ROW_CHANGES = [
    {"binding": None},
    {"binding": {"server_host_id": "", "block_host_id": "remote-host"}},
    {"binding": {"server_host_id": "local-host", "block_host_id": 7}},
    {"binding": {"server_host_id": "same-host", "block_host_id": "same-host"}},
    {"provider": "kiro"},
    {"local": None},
]


@pytest.mark.parametrize("change", ROW_CHANGES)
async def test_no_host_field_of_a_scan_row_changes_the_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    """The debug toggle reads no host fact from a scan row. Label: pins_existing.

    Provenance: each row is the row made by hand of the remote terminal (the
    producers' record without the withholding of row T-68; since T-68 no scan
    shows it) with one more field changed by hand. Rex does not emit these
    rows. The statement is about the toggle, given such a row. They were the
    edge of the round 8 predicate, which is gone; they stay because what they
    state holds without it: whatever a row says of a host, the address is
    granted. The unchanged row is the first ``known_limit`` test below.
    """
    bridge, page = await rex_row_made_without_the_t68_withholding(
        tmp_path, monkeypatch, "remote-host"
    )
    await observe(bridge, {**page, "peers": [{**page["peers"][0], **change}]})
    granted = await bridge.call("debug_on", {"scope": REX_IDENT})
    assert [entry["scope"] for entry in granted["scopes"]] == [REX_IDENT]
    assert bridge.debug.until(REX_IDENT) is not None


# Review round 9. The remote Rex case leaves row T-56 and is board row T-100,
# held for the owner (orchestrator's decision of 2026-11-20, not an owner
# ruling). The tests below pin the limit as it stands until then. Their labels
# are measured against ``54f5b71``, the tip of round 8, which refused.
# Since row T-68 the limit stands so: the toggle still checks no host, but
# discovery no longer shows such a terminal. The scan row of these tests is
# made by hand (`rex_row_made_without_the_t68_withholding`).


async def remote_rex_terminal(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Bridge, Collector]:
    """A bridge whose collector has taken a made page that shows the terminal on another host.

    The page is made by hand (`rex_row_made_without_the_t68_withholding`): it
    is the producers' record without the withholding of row T-68, and since
    T-68 no scan shows it. The server is ``local-host``, the block reports
    ``remote-host``, and rex.py:343 says ``local`` is false.
    """
    bridge, page = await rex_row_made_without_the_t68_withholding(
        directory, monkeypatch, "remote-host"
    )
    peer = page["peers"][0]
    assert peer["local"] is False
    assert peer["binding"]["server_host_id"] == "local-host"
    assert peer["binding"]["block_host_id"] == "remote-host"
    assert is_remote(REX_IDENT) is False
    observer = await observe(bridge, page)
    assert REX_IDENT in bridge.events.state["peers"]
    return bridge, observer


async def test_known_limit_pending_t100_a_remote_rex_terminal_is_granted_a_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The debug toggle does not check a Rex terminal for its host. Label: red_at_pretip.

    The lease is granted, saved and loaded again, as for any address whose
    text names no machine. Row T-100 decides whether it should be.

    The scan row is made by hand (`remote_rex_terminal`): since row T-68 no
    scan shows it. The statement is about the toggle, given such a row.
    """
    bridge, observer = await remote_rex_terminal(tmp_path, monkeypatch)
    granted = await bridge.call("debug_on", {"scope": REX_IDENT})
    assert [entry["scope"] for entry in granted["scopes"]] == [REX_IDENT]
    assert granted["scopes"][0]["observed"] is True
    assert "warning" not in granted
    assert bridge.debug.until(REX_IDENT) is not None
    assert observer._debugging(REX_IDENT) is True

    restarted = DebugController(runtime_dir(bridge.config_path))
    assert restarted.error is None
    assert set(restarted.leases) == {REX_IDENT}


async def test_known_limit_pending_t100_a_remote_rex_terminal_is_selected_by_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `all` lease selects the terminal too. Label: red_at_pretip.

    The scan row is made by hand (`remote_rex_terminal`): since row T-68 no
    scan shows it. The statement is about the toggle, given such a row.
    """
    bridge, observer = await remote_rex_terminal(tmp_path, monkeypatch)
    granted = await bridge.call("debug_on", {"scope": "all"})
    assert [entry["scope"] for entry in granted["scopes"]] == ["all"]
    assert REX_IDENT in granted["peers"]
    assert bridge.debug.until(REX_IDENT) is not None
    assert observer._debugging(REX_IDENT) is True


async def test_known_limit_pending_t100_a_remote_rex_terminal_can_be_an_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`debug off` for the terminal saves it as an exclusion. Label: red_at_pretip.

    The scan row is made by hand (`remote_rex_terminal`): since row T-68 no
    scan shows it. The statement is about the toggle, given such a row.
    """
    bridge, observer = await remote_rex_terminal(tmp_path, monkeypatch)
    await bridge.call("debug_on", {"scope": "all"})
    removed = await bridge.call("debug_off", {"scope": REX_IDENT})
    assert removed["excluded"] == [REX_IDENT]
    assert bridge.debug.until(REX_IDENT) is None
    assert observer._debugging(REX_IDENT) is False

    reloaded = DebugController(runtime_dir(bridge.config_path))
    assert reloaded.error is None
    assert set(reloaded.leases) == {"all"}
    assert reloaded.excluded == {REX_IDENT}


async def test_known_limit_pending_t100_a_lease_saved_before_the_collector_pass_stays_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector pass that shows another host ends nothing. Label: red_at_pretip.

    The lease is granted before any collector pass, loaded by the next start,
    and still in force after the pass that shows the terminal on another host.
    Public discovery is earlier than the lease: the helper has called
    `Bridge.peer_search()`. Since row T-68 the real call returns no terminal.
    The page of the pass is made by hand: the producers' record without the
    withholding of T-68, which no scan shows. The statement is about the
    toggle and the collector, given such a row.
    """
    bridge, page = await rex_row_made_without_the_t68_withholding(
        tmp_path, monkeypatch, "remote-host"
    )
    await bridge.call("debug_on", {"scope": REX_IDENT})
    restarted = Bridge(bridge.config, bridge.config_path)
    assert set(restarted.debug.leases) == {REX_IDENT}

    await observe(restarted, page)
    assert set(restarted.debug.leases) == {REX_IDENT}
    assert restarted.debug.until(REX_IDENT) is not None
    reloaded = DebugController(runtime_dir(bridge.config_path))
    assert reloaded.error is None
    assert set(reloaded.leases) == {REX_IDENT}


@pytest.mark.parametrize("lease", [None, REX_IDENT, "all"], ids=["no-lease", "its-own", "all"])
async def test_known_limit_pending_t100_the_rex_read_of_that_terminal_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lease: str | None
) -> None:
    """The guard that makes the limit safe: nothing remote is read.

    Label: pins_existing without a lease; red_at_pretip with one, at the lease
    (round 8 granted none), not at the read. A debug lease is a scope, not a
    read. The read is Rex's own (`Bridge.rex_read`, `RexService.read`,
    `Rex.read`), and rex.py:524 refuses a terminal whose record has no ``read``
    capability; rex.py:416 gives that capability to a local block only. The
    native calls of this fixture answer three methods; a capture call would be
    a `KeyError`, not this refusal.

    The scan row behind the lease is made by hand (`remote_rex_terminal`):
    since row T-68 no scan shows it. The read does not use the row.
    """
    bridge, _ = await remote_rex_terminal(tmp_path, monkeypatch)
    if lease is not None:
        await bridge.call("debug_on", {"scope": lease})
    assert (bridge.debug.until(REX_IDENT) is not None) is (lease is not None)

    with pytest.raises(BridgeError, match="^Rex terminal lacks local bounded-read capability$"):
        await bridge.call("rex_read", {"session_id": REX_SESSION, "block_id": REX_BLOCK})
    with pytest.raises(BridgeError, match="^Rex terminal lacks local bounded-read capability$"):
        await bridge.rex.adapter.read(REX_SESSION, REX_BLOCK)


# Review round 10 (fifth review, finding 3): the seven pins above name the
# terminal by its address. This one goes through a receipt.
RECEIPT_KEY = "t56-rex-receipt"


async def lease_and_release(bridge: Bridge, observer: Collector, scope: str) -> dict[str, Any]:
    """What `debug on` and `debug off` with ``scope`` do to the terminal's lease."""

    def outline(answer: dict[str, Any]) -> dict[str, Any]:
        return {
            "scopes": [(entry["scope"], entry.get("observed")) for entry in answer["scopes"]],
            "excluded": answer["excluded"],
            "warning": "warning" in answer,
        }

    seen: dict[str, Any] = {"on": outline(await bridge.call("debug_on", {"scope": scope}))}
    seen["selected"] = bridge.debug.until(REX_IDENT) is not None and observer._debugging(REX_IDENT)
    seen["off"] = outline(await bridge.call("debug_off", {"scope": scope}))
    seen["released"] = bridge.debug.until(REX_IDENT) is None
    await bridge.call("debug_on", {"scope": "all"})
    seen["excluded"] = outline(await bridge.call("debug_off", {"scope": scope}))
    saved = DebugController(runtime_dir(bridge.config_path))
    seen["saved"] = (saved.error, sorted(saved.leases), sorted(saved.excluded))
    await bridge.call("debug_off", {})
    return seen


@pytest.mark.parametrize("route", ["receipt-id", "request-key"])
async def test_known_limit_pending_t100_receipt_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A receipt that resolves to the terminal is granted and released as its address is.

    Label: pins_existing. It passes at ``0458ad4``, the tip of round 9, on
    purpose: no test went through a receipt to a Rex terminal.

    Provenance: the row is written by the store's own `TaskStore.reserve`
    (tasks.py:210), so the receipt ID is the one the store makes (tasks.py:253)
    and the request key is one its pattern admits (tasks.py:236). The target is
    set by hand, as in the reviewer's probe (T-56-review-codex-5.md, finding 3;
    ``test_rex_receipt_limit_routes`` in its file ``test_xrev5.py``). No
    producer of this bridge reserves a receipt for an unenrolled terminal:
    adapters/rex.py:107 refuses one before admission, and rex_mailbox.py:225
    reserves for the enrolled id ``rex:<uuid>``. So the row is a made case,
    not a form found in a store; it is the case the reviewer's mutation hits.

    The scan row is made by hand too (`remote_rex_terminal`): since row T-68
    no scan shows it. The statement is about the toggle, given such a row.
    """
    bridge, observer = await remote_rex_terminal(tmp_path, monkeypatch)
    receipt, created = bridge.store.reserve(REX_IDENT, "fixture", RECEIPT_KEY, {"kind": "rex"})
    assert created is True
    scope = receipt["id"] if route == "receipt-id" else RECEIPT_KEY
    assert scope != REX_IDENT

    typed = await lease_and_release(bridge, observer, REX_IDENT)
    assert await lease_and_release(bridge, observer, scope) == typed
    assert typed == {
        "on": {"scopes": [(REX_IDENT, True)], "excluded": [], "warning": False},
        "selected": True,
        "off": {"scopes": [], "excluded": [], "warning": False},
        "released": True,
        "excluded": {"scopes": [("all", None)], "excluded": [REX_IDENT], "warning": False},
        "saved": (None, ["all"], [REX_IDENT]),
    }
