"""T-40: bounded debug leases, scopes that name no other machine, and metadata-only evidence."""

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from events_helpers import PEER, RUN, Script, peer, receipt
from test_tracing import Clock

from answerback_bridge.cli import parser
from answerback_bridge.config import Config
from answerback_bridge.events.collector import Collector
from answerback_bridge.events.hub import EventHub
from answerback_bridge.service import Bridge


@pytest.fixture(autouse=True)
def close_debug_writers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close controllers created by this file, including ones owned by fake bridges."""
    from answerback_bridge.events.debug import DebugController

    controllers: list[DebugController] = []
    original = DebugController.__init__

    def tracked(self: DebugController, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        controllers.append(self)

    monkeypatch.setattr(DebugController, "__init__", tracked)
    yield
    for controller in controllers:
        # Allows the regression cases to run against BASE, before close existed.
        close = getattr(controller, "close", None)
        if close is not None:
            close()


async def flush_drawer(controller: Any) -> None:
    """Drain new writers; BASE writes synchronously and needs no flush."""
    flush = getattr(controller, "flush", None)
    if flush is not None:
        await asyncio.to_thread(flush)


async def test_t74_drawer_burst_never_writes_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: 3000 publishes leave all drawer filesystem work to the writer."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    threads: list[int] = []
    original = module.private_directory

    def observed(directory: Path) -> None:
        threads.append(threading.get_ident())
        original(directory)

    monkeypatch.setattr(module, "private_directory", observed)
    hub = EventHub(debug=controller)
    for index in range(3000):
        hub.publish(model.run_started("T-74", str(index), peer_id=PEER))
    await flush_drawer(controller)
    assert threads and threading.get_ident() not in threads


@pytest.mark.parametrize(("queue_size", "drawer_bytes"), [(2, 4 * 1024 * 1024), (256, 1024)])
async def test_t74_blocked_drawer_drops_and_counts_without_blocking_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queue_size: int, drawer_bytes: int
) -> None:
    """red_at_base: count and byte admission stay bounded while storage is blocked."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    monkeypatch.setattr(module, "DRAWER_QUEUE_SIZE", queue_size, raising=False)
    monkeypatch.setattr(module, "DRAWER_WRITES_PER_SECOND", 5000, raising=False)
    monkeypatch.setattr(module, "DRAWER_BYTES", drawer_bytes)
    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    started, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    original = module.private_directory

    def blocked(directory: Path) -> None:
        if threading.get_ident() != main_thread:
            started.set()
            assert release.wait(5), "test did not release the writer"
        original(directory)

    monkeypatch.setattr(module, "private_directory", blocked)
    hub = EventHub(debug=controller)
    try:
        hub.publish(model.run_started("T-74", "first", peer_id=PEER))
        assert await asyncio.to_thread(started.wait, 1), "no background writer started"
        for index in range(3000):
            hub.publish(model.run_started("T-74", str(index), peer_id=PEER))
        assert controller.status()["droppedWrites"] >= 2990
        assert controller.status()["error"] == "DebugDrawerWriteQueueFull"
        assert controller._writes.qsize() <= queue_size
        assert controller._queued_bytes <= drawer_bytes
    finally:
        release.set()
        await flush_drawer(controller)
    assert controller._queued_bytes == 0


async def test_t74_drawer_admission_rate_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: fast storage cannot bypass the independent per-second write budget."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    clock = Clock()
    monkeypatch.setattr(module, "monotonic", clock, raising=False)
    monkeypatch.setattr(module, "DRAWER_WRITES_PER_SECOND", 3, raising=False)
    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    hub = EventHub(debug=controller)
    for index in range(10):
        hub.publish(model.run_started("T-74", str(index), peer_id=PEER))
        await flush_drawer(controller)  # The queue is empty before every next publish.
    trace = controller.directory / "trace.ndjson"
    assert len(trace.read_text().splitlines()) == 3  # Baseline plus two run events.
    assert controller.status()["droppedWrites"] == 8
    assert controller.status()["error"] == "DebugDrawerWriteRateLimited"
    clock.value += 1
    hub.publish(model.run_started("T-74", "next-window", peer_id=PEER))
    await flush_drawer(controller)
    assert len(trace.read_text().splitlines()) == 4


async def test_t74_dropped_drawer_delta_restarts_with_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: lossy admission reports the gap and restores the projection base."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    clock = Clock()
    monkeypatch.setattr(module, "monotonic", clock, raising=False)
    monkeypatch.setattr(module, "DRAWER_WRITES_PER_SECOND", 2, raising=False)
    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    hub = EventHub(debug=controller)
    hub.publish(model.run_started("T-74", RUN, peer_id=PEER))
    await flush_drawer(controller)
    hub.set_section("peers", {PEER: {"name": "new"}})
    await flush_drawer(controller)
    assert controller.status().get("droppedWrites", 0) == 1
    clock.value += 1
    hub.publish(model.run_finished("T-74", RUN, peer_id=PEER))
    await flush_drawer(controller)
    records = [
        json.loads(line)
        for line in (controller.directory / "trace.ndjson").read_text().splitlines()
    ]
    assert records[-2]["event"]["type"] == "STATE_SNAPSHOT"
    assert records[-2]["event"]["snapshot"]["peers"] == {PEER: {"name": "new"}}


async def test_t74_drawer_write_failure_is_best_effort(tmp_path: Path) -> None:
    """pins_existing: failed storage does not end the lease or stop later events."""
    from answerback_bridge.events import model
    from answerback_bridge.events.debug import DebugController

    controller = DebugController(tmp_path)
    controller.on(PEER)
    trace = controller.directory / "trace.ndjson"
    trace.mkdir()
    hub = EventHub(debug=controller)
    hub.publish(model.run_started("T-74", RUN, peer_id=PEER))
    await flush_drawer(controller)
    assert controller.status()["error"] == "IsADirectoryError"
    assert controller.until(PEER) is not None
    trace.rmdir()
    hub.publish(model.run_finished("T-74", RUN, peer_id=PEER))
    await flush_drawer(controller)
    assert trace.is_file()


async def test_t74_drawer_keeps_its_per_record_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: oversized debug records remain refused without ending the lease."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    monkeypatch.setattr(module, "DRAWER_BYTES", 1024)
    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    hub = EventHub(debug=controller)
    hub.publish(model.run_started("T-74", "x" * 2048, peer_id=PEER))
    await flush_drawer(controller)
    assert controller.status()["error"] == "DebugEventExceedsDrawerBound"
    assert controller.until(PEER) is not None
    assert (controller.directory / "trace.ndjson").stat().st_size <= 1024


@pytest.mark.parametrize("adapter_failure", [False, True])
async def test_t74_bridge_close_drains_and_stops_the_drawer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter_failure: bool
) -> None:
    """red_at_base: teardown owns the writer even without an enabled collector."""
    from answerback_bridge.events import model

    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    bridge.debug.on(PEER)
    bridge.events.publish(model.run_started("T-74", RUN, peer_id=PEER))

    class Adapter:
        async def close(self) -> None:
            if adapter_failure:
                raise RuntimeError("test adapter close")

    monkeypatch.setattr(bridge, "adapters", [Adapter()])
    if adapter_failure:
        with pytest.raises(RuntimeError, match="test adapter close"):
            await bridge.close()
    else:
        await bridge.close()
    writer = getattr(bridge.debug, "_writer", None)
    assert writer is not None and not writer.is_alive()
    trace = bridge.debug.directory / "trace.ndjson"
    assert RUN in trace.read_text()
    before = trace.read_bytes()
    bridge.events.publish(model.run_finished("T-74", RUN, peer_id=PEER))
    await asyncio.to_thread(bridge.debug.close)
    assert trace.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFO support")
@pytest.mark.parametrize("through_bridge", [False, True])
@pytest.mark.parametrize("has_reader", [False, True])
async def test_t74r1_fifo_drawer_is_refused_without_stalling_shutdown(
    tmp_path: Path, through_bridge: bool, has_reader: bool
) -> None:
    """red_at_review_tip: special files cannot block open or receive drawer bytes."""
    from answerback_bridge.events import model

    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    controller = bridge.debug
    controller.on(PEER)
    path = controller.directory / "trace.ndjson"
    os.mkfifo(path, 0o600)
    reader = os.open(path, os.O_RDWR | os.O_NONBLOCK) if has_reader else None
    publishing = asyncio.create_task(
        asyncio.to_thread(bridge.events.publish, model.run_started("T-74", RUN, peer_id=PEER))
    )
    try:
        # Bound publish itself: synchronous FIFO open must fail this test, not hang it.
        # The burst regression separately pins offloading from the publishing thread.
        await asyncio.wait_for(asyncio.shield(publishing), timeout=0.5)
        close = bridge.close() if through_bridge else asyncio.to_thread(controller.close)
        await asyncio.wait_for(close, timeout=1.5)
        status = controller.status()
        assert status["droppedWrites"] == 2  # Scoped baseline and lifecycle event.
        assert status["error"]
        assert controller._writer is not None and not controller._writer.is_alive()
        if reader is not None:
            with pytest.raises(BlockingIOError):
                os.read(reader, 4096)
    finally:
        # Unblock the old writer even when the regression fails, so executor/fixture
        # cleanup also finishes. Keep the reader alive until all queued opens finish.
        if reader is None:
            reader = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            await asyncio.wait_for(publishing, timeout=2)
            close_drawer = getattr(controller, "close", None)
            if close_drawer is not None:
                await asyncio.wait_for(asyncio.to_thread(close_drawer), timeout=2)
        finally:
            os.close(reader)


@pytest.mark.parametrize("through_bridge", [False, True])
async def test_t74r1_shutdown_deadline_counts_inflight_and_full_queue_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through_bridge: bool
) -> None:
    """red_at_review_tip: even a stuck regular-file write cannot hold shutdown open."""
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    monkeypatch.setattr(module, "DRAWER_QUEUE_SIZE", 2)
    monkeypatch.setattr(module, "DRAWER_CLOSE_SECONDS", 0.1, raising=False)
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    controller = bridge.debug
    controller.on(PEER)
    entered, release = threading.Event(), threading.Event()

    def stalled(encoded: bytes) -> None:
        entered.set()
        assert release.wait(3), "test must release its simulated stuck filesystem"

    monkeypatch.setattr(controller, "_write_sync", stalled)
    try:
        bridge.events.publish(model.run_started("T-74", RUN, peer_id=PEER))
        assert await asyncio.to_thread(entered.wait, 1)
        bridge.events.publish(model.run_finished("T-74", RUN, peer_id=PEER))
        assert controller._writes.full()
        before = controller.status()["droppedWrites"]
        close = bridge.close() if through_bridge else asyncio.to_thread(controller.close)
        await asyncio.wait_for(close, timeout=0.5)
        assert controller.status()["droppedWrites"] == before + 3
        assert controller.status()["error"] == "DebugDrawerCloseTimeout"
        await asyncio.wait_for(asyncio.to_thread(controller.close), timeout=0.5)
        assert controller.status()["droppedWrites"] == before + 3
        assert controller._writer is not None and controller._writer.is_alive()
    finally:
        release.set()
        if controller._writer is not None:
            await asyncio.to_thread(controller._writer.join, 2)
    assert controller._writer is not None and not controller._writer.is_alive()
    assert controller._queued_bytes == 0


def test_debug_cli_default_and_duration_bounds() -> None:
    args = parser().parse_args(["debug", "on", PEER])
    assert args.seconds == 600
    assert parser().parse_args(["debug", "on", "all", "--for", "60m"]).seconds == 3600
    assert parser().parse_args(["debug", "on", PEER, "--for", "7s"]).seconds == 7
    for value in ("61m", "3601s", "0m", "-1m", "nan", "inf"):
        with pytest.raises(SystemExit):
            parser().parse_args(["debug", "on", "all", "--for", value])
    assert parser().parse_args(["debug", "off"]).scope is None
    assert parser().parse_args(["debug", "status"]).debug_action == "status"


def test_deadline_survives_restart_and_expires(tmp_path: Path) -> None:
    from answerback_bridge.events.debug import DebugController

    clock = Clock()
    debug = DebugController(tmp_path, clock=clock)
    debug.on(PEER)
    assert debug.until(PEER) == clock.value + 600
    for seconds in (3601, 0, -1, float("nan"), True):
        with pytest.raises(ValueError):
            debug.on(PEER, seconds)
    restarted = DebugController(tmp_path, clock=clock)
    assert restarted.until(PEER) == clock.value + 600
    clock.value += 600
    assert restarted.status()["scopes"] == []
    assert restarted.until(PEER) is None
    assert DebugController(tmp_path, clock=clock).status()["scopes"] == []


def test_local_peer_rejects_canonical_remote_address() -> None:
    from answerback_bridge.events.debug import local_peer

    # "hc:<machine>/<terminal>" is the real shape core.py:285 emits for a remote
    # terminal (core.py:22's _ADDRESS regex); "hc:<terminal>" is the local shape.
    assert local_peer("hc:example-host/t61") is False
    assert local_peer("hc:t61") is True


def test_local_peer_keeps_a_local_receipt_target_with_a_path_local() -> None:
    from answerback_bridge.events.debug import local_peer

    # These are the exact receipt targets workers.py:181-183 and
    # native_workers.py:116 reserve for a local Kiro / native CLI run: a path
    # after the alias's ':', not a machine prefix.
    assert local_peer("kiro:work:/tmp/project") is True
    assert local_peer("codex_cli:work:/tmp/project") is True
    assert local_peer("hc:example-host/t61") is False


def test_is_remote_looks_only_at_the_machine_naming_part(tmp_path: Path) -> None:
    """T-56 finding X6: "is this on another machine?" looks only at the part of
    the address that can name a machine: the Core address after the last ':'
    (review round 7, B2 and B3; tests/test_debug_provider_identity.py). The
    target of any other provider is never read as one, no matter what it
    contains: a space, an '@', or a path past 256 characters are not machine
    prefixes. The long path is a string in the form of native_workers.py:116
    with seven directory names of 40 characters; no directory is created.
    """
    from answerback_bridge.events.debug import is_remote

    assert is_remote("hc:example-host/t61") is True
    assert is_remote("hc:t61") is False
    assert is_remote("hc:user@host") is True
    assert is_remote("kiro:work:/tmp/My Project") is False
    assert is_remote("codex_cli:work:/tmp" + ("/" + "a" * 40) * 7) is False
    assert is_remote("no-colon-at-all") is False


def test_all_excludes_machine_addresses_and_can_turn_off_one_peer(tmp_path: Path) -> None:
    from answerback_bridge.events.debug import DebugController

    debug = DebugController(tmp_path)
    debug.on("all")
    assert debug.until(PEER) is not None
    assert debug.until("hc:example-host/t61") is None
    assert debug.until("hc:nuc1/t2") is None
    debug.off(PEER)
    assert debug.until(PEER) is None
    assert debug.until("crew:new") is not None
    assert DebugController(tmp_path).until(PEER) is None
    debug.on(PEER)
    assert debug.until(PEER) is not None
    debug.off()
    assert debug.status()["scopes"] == []


async def test_scope_resolution_uses_receipts_and_refuses_machine_addresses(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task, _ = bridge.store.reserve(
        "hc:default:t61",
        "Task T-40. PRIVATE BRIEF",
        "debug-T40",
        {"kind": "hc", "peer": {"peer_id": PEER}},
    )
    result = await bridge.call("debug_on", {"scope": task["id"]})
    assert result["scopes"][0]["scope"] == PEER
    assert (await bridge.call("debug_status", {}))["scopes"]
    assert not (await bridge.call("debug_off", {"scope": task["id"]}))["scopes"]
    for scope in ("hc:example-host/t61", "hc:nuc1/t2"):
        with pytest.raises(ValueError, match="T-29"):
            await bridge.call("debug_on", {"scope": scope})
    with pytest.raises(ValueError, match="Unknown"):
        await bridge.call("debug_on", {"scope": "missing-task"})


async def test_debug_off_refuses_remote_and_user_host_addresses(tmp_path: Path) -> None:
    """The remote refusal applies to debug off too, not only debug on (T-56 WP-31)."""
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    for scope in ("hc:example-host/t61", "hc:user@host", "hc:user@host/t61"):
        with pytest.raises(ValueError, match="T-29"):
            await bridge.call("debug_off", {"scope": scope})


async def test_debug_on_refuses_caller_whitespace_without_the_remote_message(
    tmp_path: Path,
) -> None:
    """Rework round 1, M-2/R-3: a caller-typed scope that fails ``local_peer()``'s
    input bounds (here, whitespace in the text itself) but is not remote by
    ``is_remote()`` must not be refused with the "remote" message. The refusal
    fallback in ``resolve_scope()`` must classify by ``is_remote()``, the same
    rule the grant path already uses, not by raw ``':'``/``'/'``/``'@'`` marker
    presence in the caller's text -- a receipt-shaped local scope like this one
    has both a ``':'`` and no machine-naming marker, so the old marker check
    mislabelled it "remote" even though ``is_remote()`` says it is not.
    """
    from answerback_bridge.events.debug import is_remote

    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    scope = "kiro:work has space"
    assert is_remote(scope) is False  # no machine-naming marker anywhere in it
    with pytest.raises(ValueError) as excinfo:
        await bridge.call("debug_on", {"scope": scope})
    assert "remote" not in str(excinfo.value).lower()


async def test_debug_on_a_task_id_of_a_local_kiro_receipt_is_granted(tmp_path: Path) -> None:
    """A Kiro/native-CLI receipt target holds a path; local_peer() must keep it local."""
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    # The exact target shape workers.py:181-183 reserves for a local Kiro run,
    # with no stored peer id, as the real metadata has it.
    target = "kiro:work:/tmp/project"
    task, _ = bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-kiro",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/project",
            "completion": "acp_prompt_response",
        },
    )
    result = await bridge.call("debug_on", {"scope": task["id"]})
    assert result["scopes"][0]["scope"] == target
    assert result["scopes"][0]["observed"] is True
    assert "warning" not in result
    await bridge.call("debug_off", {"scope": task["id"]})
    await bridge.call("debug_on", {"scope": "all"})
    assert bridge.debug.until(target) is not None


async def test_debug_on_grants_a_discovered_peer_observed_true_no_warning(
    tmp_path: Path,
) -> None:
    """A discovered peer: granted, observed true, no warning."""
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    script = Script()
    script.scan = {"peers": [peer(ident="hc:t61")], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    assert "hc:t61" in bridge.events.state["peers"]
    result = await bridge.call("debug_on", {"scope": "hc:t61"})
    assert result["scopes"][0]["scope"] == "hc:t61"
    assert result["scopes"][0]["observed"] is True
    assert "warning" not in result


async def test_debug_on_before_any_peer_scan_is_granted_with_observed_false(
    tmp_path: Path,
) -> None:
    """No scan has run yet, and that still grants (T-56 WP-32, decision
    D-20261120-07): no state of the peer scan can cause a refusal any more.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    assert bridge.events.state["peers"] == {}
    result = await bridge.call("debug_on", {"scope": "hc:t999"})
    assert result["scopes"][0]["scope"] == "hc:t999"
    assert result["scopes"][0]["observed"] is False
    assert result["warning"] == (
        "hc:t999 is not in this bridge's current list of discovered peers. The "
        "list is a snapshot and can be incomplete, so this does not show that "
        "the peer is absent. The lease takes effect if a run of this peer appears."
    )


async def test_debug_on_after_a_complete_scan_that_does_not_hold_it_is_granted(
    tmp_path: Path,
) -> None:
    """``debug on hc:t999`` with a complete scan that does not hold it: granted,
    observed false, the exact warning.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    script = Script()
    script.scan = {"peers": [peer(ident="hc:t61")], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    assert "hc:t999" not in bridge.events.state["peers"]
    result = await bridge.call("debug_on", {"scope": "hc:t999"})
    assert result["scopes"][0]["observed"] is False
    assert result["warning"] == (
        "hc:t999 is not in this bridge's current list of discovered peers. The "
        "list is a snapshot and can be incomplete, so this does not show that "
        "the peer is absent. The lease takes effect if a run of this peer appears."
    )


async def test_debug_on_is_granted_after_a_real_peer_scan_failure(tmp_path: Path) -> None:
    """A total peer-scan failure (events/collector.py's _guard swallowing an
    exception into sourceErrors["peers"]) no longer gates anything: debug_on reads
    neither sourceErrors nor any scan-outcome flag at all now (T-56 findings X4, X7).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")

    async def failing_peers() -> dict[str, Any]:
        raise TimeoutError("boom")

    script = Script()
    sources = script.sources(full=True)
    sources.peers = failing_peers
    observer = Collector(bridge.events, sources)
    await observer.tick_slow()
    assert bridge.events.state["bridge"]["sourceErrors"] == {"peers": "TimeoutError"}
    result = await bridge.call("debug_on", {"scope": "hc:t998"})
    assert result["scopes"][0]["observed"] is False


async def test_debug_on_is_granted_while_the_scan_failure_is_still_unpublished(
    tmp_path: Path,
) -> None:
    """X4: the removed refusal read sourceErrors before it was published, so a
    request in that window saw a stale "clean scan" state. debug_on reads neither
    sourceErrors nor any scan-outcome flag now, so this window cannot cause a
    wrong decision, whether or not publication has finished yet.
    """
    import asyncio

    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    release = asyncio.Event()

    async def failing_peers() -> dict[str, Any]:
        raise TimeoutError("boom")

    async def blocked_board(paths: list[str]) -> dict[str, Any]:
        await release.wait()
        return {"decisions": {}, "applyQueue": {}, "heads": {}}

    script = Script()
    sources = script.sources(full=True)
    sources.peers, sources.board = failing_peers, blocked_board
    observer = Collector(bridge.events, sources)
    tick = asyncio.create_task(observer.tick_slow())
    await asyncio.sleep(0)
    result = await bridge.call("debug_on", {"scope": "hc:t998"})
    assert result["scopes"][0]["observed"] is False
    release.set()
    await tick


async def test_debug_on_grants_the_201st_real_peer_past_the_board_display_cap(
    tmp_path: Path,
) -> None:
    """X1: ``peers_section()`` drops every fresh row past ``MAX_PEERS`` (200) from
    the board projection, but that cap is a display limit, not an existence bound
    -- the 201st real peer gets the same lease as the 200 the board can show.
    """
    from answerback_bridge.events.producers import MAX_PEERS

    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    script = Script()
    script.scan = {
        "peers": [peer(ident=f"kiro:worker{i}") for i in range(MAX_PEERS + 1)],
        "complete": True,
        "errors": {},
    }
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    dropped = f"kiro:worker{MAX_PEERS}"
    assert len(bridge.events.state["peers"]) == MAX_PEERS
    assert dropped not in bridge.events.state["peers"]
    result = await bridge.call("debug_on", {"scope": dropped})
    assert result["scopes"][0]["scope"] == dropped
    assert result["scopes"][0]["observed"] is False
    assert dropped in result["warning"]


async def test_debug_on_grants_a_peer_omitted_by_a_real_rex_partial_scan(
    tmp_path: Path,
) -> None:
    """X3: adapters/rex.py's own error-key shape (``rex:<index>``) never matched
    the old provider-name exemption. There is no exemption to miss now, because
    the scan's completeness never gates the grant at all.
    """
    from answerback_bridge.adapters.rex import RexDiscovery

    class _FailingNative:
        async def inventory(self) -> dict[str, Any]:
            return {
                "peers": [],
                "errors": [{"error": "native read failed"}],
                "complete": False,
            }

    rex_config = Config(provider_selection=["rex"])
    page = await RexDiscovery(rex_config, lambda: _FailingNative()).discover()
    # The real shape adapters/rex.py:51 produces: an error keyed by index, not
    # by the bare provider name "rex" the old exemption compared against.
    assert dict(page.errors) == {"rex:0": "native read failed"}
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    script = Script()
    script.scan = page.as_record()
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    missing = "rex:terminal:session-1/block-1"
    assert missing not in bridge.events.state["peers"]
    result = await bridge.call("debug_on", {"scope": missing})
    assert result["scopes"][0]["observed"] is False
    assert missing in result["warning"]


async def test_debug_on_an_absent_peer_becomes_observed_once_it_is_discovered(
    tmp_path: Path,
) -> None:
    """A peer that is absent when the lease is granted and then runs inside the
    lease: the lease already applies (``DebugController.until()`` matches by the
    scope string alone, before any scan), and ``debug status`` then shows
    ``observed`` true once a real scan reports the peer.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    granted = await bridge.call("debug_on", {"scope": "hc:t999"})
    assert granted["scopes"][0]["observed"] is False
    assert bridge.debug.until("hc:t999") is not None  # the lease already applies
    script = Script()
    script.scan = {"peers": [peer(ident="hc:t999")], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    assert bridge.debug.until("hc:t999") is not None  # still applies once observed
    status = await bridge.call("debug_status", {})
    assert status["scopes"][0]["observed"] is True


async def test_debug_on_a_task_id_is_observed_through_its_receipt(tmp_path: Path) -> None:
    """A resolved-from-receipt scope is observed through its receipt, not the
    peer list, so it never carries the unobserved warning; a plain address given
    directly still gets the warning, since nothing refuses it any more.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    assert bridge.events.state["peers"] == {}
    target = "kiro:work:/tmp/project"
    task, _ = bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-unobserved",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/project",
            "completion": "acp_prompt_response",
        },
    )
    result = await bridge.call("debug_on", {"scope": task["id"]})
    assert result["scopes"][0]["scope"] == target
    assert result["scopes"][0]["observed"] is True
    assert "warning" not in result
    result2 = await bridge.call("debug_on", {"scope": "hc:t999"})
    # "hc:t999" sorts before "kiro:work:/tmp/project"; both leases are now active.
    assert result2["scopes"][0]["scope"] == "hc:t999"
    assert result2["scopes"][0]["observed"] is False
    assert "warning" in result2


async def test_debug_status_observed_matches_debug_on_for_a_nested_receipt_peer_id(
    tmp_path: Path,
) -> None:
    """Rework round 1, R-2: a lease resolved via a receipt's nested
    ``metadata.peer.peer_id`` (the normal Core ``hc`` path, not the ``target``
    column) must stay ``observed`` on a later, independent ``debug_status``
    call, not only in the ``debug_on`` result that granted it.
    ``Bridge._observed()`` is the single source of truth both read; it must
    recognise this receipt evidence consistently, not only when
    ``resolve_scope()``'s own ``from_receipt`` flag happens to be at hand
    (spec points 2 and 4: "was resolved from a receipt" is not forgotten the
    moment the grant call returns).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task, _ = bridge.store.reserve(
        "hc:default:t61",
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-nested-peer-id",
        {"kind": "hc", "peer": {"peer_id": PEER}},
    )
    on_result = await bridge.call("debug_on", {"scope": task["id"]})
    on_entry = next(entry for entry in on_result["scopes"] if entry["scope"] == PEER)
    assert on_entry["observed"] is True
    assert "warning" not in on_result
    status_result = await bridge.call("debug_status", {})
    status_entry = next(entry for entry in status_result["scopes"] if entry["scope"] == PEER)
    assert status_entry["observed"] is True


async def test_debug_on_a_kiro_receipt_target_address_given_directly_is_observed(
    tmp_path: Path,
) -> None:
    """``debug status`` echoes a Kiro/native-CLI scope's scope field as its
    receipt target (holding a path), never as the shorter identity the peer scan
    discovers for that same worker (no path, confirmed at adapters/kiro.py:41).
    Pasting that exact address back into ``debug on``, as an operator reading
    debug status would, is observed through ``is_receipt_target()`` even though
    it is given directly rather than through its task ID (T-56 finding T-1).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    target = "kiro:work:/tmp/project"
    bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-address",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/project",
            "completion": "acp_prompt_response",
        },
    )
    # The discovered peer scan identity for this worker has no cwd segment.
    script = Script()
    script.scan = {"peers": [peer(ident="kiro:work")], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    assert target not in bridge.events.state["peers"]
    result = await bridge.call("debug_on", {"scope": target})
    assert result["scopes"][0]["scope"] == target
    assert result["scopes"][0]["observed"] is True
    assert "warning" not in result
    # An address that matches no receipt target at all is unobserved, with a warning.
    other = await bridge.call("debug_on", {"scope": "kiro:nobody:/tmp/elsewhere"})
    assert other["scopes"][0]["observed"] is False
    assert "warning" in other


async def test_debug_on_resolves_an_address_shaped_request_key_before_the_address(
    tmp_path: Path,
) -> None:
    """X5: a request key that looks like an address must resolve through its
    receipt, not be read as an address of its own -- for debug on and debug off,
    since ``resolve_scope()`` checks the receipt before the address branch either
    way.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    target = "kiro:work:/tmp/project"
    bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "kiro:debug1",  # the request key, itself shaped like a kiro:<alias> address
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/project",
            "completion": "acp_prompt_response",
        },
    )
    # A real, unrelated peer scanned under the request key's own literal text.
    script = Script()
    script.scan = {"peers": [peer(ident="kiro:debug1")], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    result = await bridge.call("debug_on", {"scope": "kiro:debug1"})
    # The receipt wins: the lease lands on the receipt's target, not the
    # unrelated peer whose address happens to equal the request key's text.
    assert result["scopes"][0]["scope"] == target
    assert bridge.debug.until("kiro:debug1") is None
    off_result = await bridge.call("debug_off", {"scope": "kiro:debug1"})
    assert off_result["scopes"] == []
    assert bridge.debug.until(target) is None


async def test_debug_on_treats_a_request_key_literally_named_all_as_the_keyword(
    tmp_path: Path,
) -> None:
    """A receipt may legally have the request key ``"all"``; the word still
    means the global keyword, never that receipt's target (T-56 finding X5).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    target = "kiro:work:/tmp/project"
    bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "all",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/project",
            "completion": "acp_prompt_response",
        },
    )
    result = await bridge.call("debug_on", {"scope": "all"})
    assert [entry["scope"] for entry in result["scopes"]] == ["all"]
    assert bridge.debug.until(target) is not None  # covered by "all", not its own lease
    await bridge.call("debug_off", {"scope": "all"})
    assert (await bridge.call("debug_status", {}))["scopes"] == []


async def test_debug_on_a_receipt_target_with_a_space_is_granted_not_remote(
    tmp_path: Path,
) -> None:
    """X6: a local receipt target holding a space in its path (a real, permitted
    absolute working directory) is trusted and never classified as remote, by
    task ID or by request key.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    target = "kiro:work:/tmp/My Project"
    task, _ = bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-space",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": "/tmp/My Project",
            "completion": "acp_prompt_response",
        },
    )
    by_task_id = await bridge.call("debug_on", {"scope": task["id"]})
    assert by_task_id["scopes"][0]["scope"] == target
    await bridge.call("debug_off", {"scope": task["id"]})
    by_request_key = await bridge.call("debug_on", {"scope": "debug-T56-space"})
    assert by_request_key["scopes"][0]["scope"] == target
    assert bridge.debug.until(target) is not None  # not refused as remote


async def test_debug_on_a_long_receipt_target_is_granted_not_remote(tmp_path: Path) -> None:
    """X6: a local receipt target longer than 256 characters is trusted and
    never classified as remote, by task ID or by request key -- only
    ``resolve_scope()``'s own top guard on the literal text the caller typed (the
    task ID / request key itself) is length-bounded.

    The working directory exists, as workers.py:171 demands before it reserves
    a receipt: nine directories of 30 characters with a space and an '@' in
    their names, under ``tmp_path`` (review round 7, N1: the path used to be
    one name of 260 characters, which no file system holds).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    directory = tmp_path.joinpath(*["My Project me@corp " + "a" * 11] * 9)
    directory.mkdir(parents=True)
    long_cwd = str(directory)
    assert directory.is_dir()
    assert all(len(name) <= 255 for name in directory.parts)
    target = f"kiro:work:{long_cwd}"
    assert 256 < len(target) < 1024
    task, _ = bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-long",
        {
            "kind": "kiro",
            "alias": "work",
            "cwd": long_cwd,
            "completion": "acp_prompt_response",
        },
    )
    by_task_id = await bridge.call("debug_on", {"scope": task["id"]})
    assert by_task_id["scopes"][0]["scope"] == target
    await bridge.call("debug_off", {"scope": task["id"]})
    by_request_key = await bridge.call("debug_on", {"scope": "debug-T56-long"})
    assert by_request_key["scopes"][0]["scope"] == target


async def test_a_status_scope_with_a_space_or_over_256_characters_cannot_be_pasted(
    tmp_path: Path,
) -> None:
    """Review round 7, N3: which status scopes can be typed back. Label: pins_existing.

    ``debug status`` echoes the receipt target as the scope. Typed back into
    ``debug on`` or ``debug off`` it is caller text, and the caller rules
    refuse, among other things, whitespace and more than 256 characters (the
    rest of the rule is in the next test). The lease is reached by its receipt
    ID or request key instead. The targets are in the form of workers.py:182,
    and the long working directory exists (workers.py:171).
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    directory = tmp_path.joinpath(*["project-" + "a" * 22] * 9)
    directory.mkdir(parents=True)
    unknown = "^Unknown debug task ID; use its receipt ID or a local provider:peer$"
    too_long = "^Debug scope must be a task ID, local peer address, or all$"
    cases = [("kiro:work:/tmp/My Project", unknown), (f"kiro:work:{directory}", too_long)]
    assert len(cases[1][0]) > 256
    for number, (target, refusal) in enumerate(cases):
        alias, cwd = target.split(":", 2)[1:]
        task, _ = bridge.store.reserve(
            target,
            "Task T-56. PRIVATE BRIEF",
            f"debug-T56-paste-{number}",
            {"kind": "kiro", "alias": alias, "cwd": cwd, "completion": "acp_prompt_response"},
        )
        granted = await bridge.call("debug_on", {"scope": task["id"]})
        echoed = granted["scopes"][0]["scope"]
        assert echoed == target
        for method in ("debug_on", "debug_off"):
            with pytest.raises(ValueError, match=refusal):
                await bridge.call(method, {"scope": echoed})
        assert bridge.debug.until(target) is not None
        removed = await bridge.call("debug_off", {"scope": task["id"]})
        assert removed["scopes"] == []


async def test_a_status_scope_that_is_unprintable_or_of_an_unknown_provider_cannot_be_pasted(
    tmp_path: Path,
) -> None:
    """Review round 8, finding 3: the rest of the rule for typed text.

    Label: pins_existing (passes at ``62090f3`` too). `local_peer()` asks for
    more than no whitespace and at most 256 characters: every character is
    printable, and the provider is one this bridge knows now. U+200B is a
    format character, not whitespace and not printable, and a directory name
    may hold it; the target is in the form of workers.py:182. ``plugin:t1``
    stands for the receipt of a provider that is no longer in the catalogue
    (row T-36); no producer of this tree builds it. Both leases are reached by
    their receipt ID.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    refusal = "^Unknown debug task ID; use its receipt ID or a local provider:peer$"
    unprintable = "kiro:work:/tmp/format\u200bmark"
    assert not any(char.isspace() for char in unprintable) and not unprintable.isprintable()
    assert len(unprintable) == 26
    cases = [
        (unprintable, {"kind": "kiro", "alias": "work", "cwd": unprintable.split(":", 2)[2]}),
        ("plugin:t1", {"kind": "plugin"}),
    ]
    for number, (target, metadata) in enumerate(cases):
        task, _ = bridge.store.reserve(
            target, "Task T-56. PRIVATE BRIEF", f"debug-T56-typed-{number}", metadata
        )
        granted = await bridge.call("debug_on", {"scope": task["id"]})
        echoed = granted["scopes"][0]["scope"]
        assert echoed == target
        for method in ("debug_on", "debug_off"):
            with pytest.raises(ValueError, match=refusal):
                await bridge.call(method, {"scope": echoed})
        assert bridge.debug.until(target) is not None
        removed = await bridge.call("debug_off", {"scope": task["id"]})
        assert removed["scopes"] == []


async def test_the_observed_hint_leaves_the_stream_state_at_the_next_poll(tmp_path: Path) -> None:
    """Review round 7, N2: what a stream consumer can rely on. Label: pins_existing.

    `Bridge.debug_status()` publishes its answer, with ``observed``, to the
    event-stream state. The collector's poll then publishes the controller's
    own status, which has the deadline and no ``observed`` key, and the
    warning of `debug_on()` is never in the stream. A new status call puts the
    hint back until the next poll. ``hc:t999`` is in the form of core.py:285.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    granted = bridge.debug_on("hc:t999")
    assert "warning" in granted
    published = bridge.events.state["bridge"]["debug"]
    assert "warning" not in published
    assert published["scopes"][0]["observed"] is False

    await Collector(bridge.events, Script().sources(full=True)).poll()
    polled = bridge.events.state["bridge"]["debug"]
    assert [entry["scope"] for entry in polled["scopes"]] == ["hc:t999"]
    assert "observed" not in polled["scopes"][0]
    assert polled["scopes"][0]["untilMs"] == granted["scopes"][0]["untilMs"]
    assert "warning" not in polled

    assert bridge.debug_status()["scopes"][0]["observed"] is False
    assert bridge.events.state["bridge"]["debug"]["scopes"][0]["observed"] is False


async def test_tick_slow_no_longer_publishes_peers_scanned_or_partial(tmp_path: Path) -> None:
    """T-56 point 7: the two flags WP-32 added to the collector are removed;
    nothing in ``events/collector.py`` differs from base any more.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    script = Script()
    script.scan = {"peers": [peer()], "complete": True, "errors": {}}
    observer = Collector(bridge.events, script.sources(full=True))
    await observer.tick_slow()
    assert "peersScanned" not in bridge.events.state["bridge"]
    assert "peersPartial" not in bridge.events.state["bridge"]


async def test_debug_status_omits_observed_for_the_all_scope(tmp_path: Path) -> None:
    """The scope ``"all"`` has no single peer to be evidence-backed about, so it
    carries no ``observed`` key; an individual lease still gets one
    (T-56 WP-32, decision D-20261120-07). ``observed`` is computed by
    ``Bridge.debug_status()``, not stored by ``DebugController`` itself.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    await bridge.call("debug_on", {"scope": "all"})
    await bridge.call("debug_on", {"scope": PEER})
    status = await bridge.call("debug_status", {})
    by_scope = {entry["scope"]: entry for entry in status["scopes"]}
    assert "observed" not in by_scope["all"]
    assert "observed" in by_scope[PEER]


async def test_drawer_captures_scoped_raw_and_digest_only_activity(tmp_path: Path) -> None:
    from answerback_bridge.events import model
    from answerback_bridge.events.debug import DebugController

    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer(), peer(ident="hc:other")]}
    script.tails = {PEER: "PRIVATE_SCREEN secret", "hc:other": "UNRELATED"}
    debug = DebugController(tmp_path, clock=clock)
    hub = EventHub(debug=debug)
    observer = Collector(hub, script.sources(full=True), clock=clock)
    debug.on(PEER)
    await observer.tick()
    hub.publish(
        model.raw(
            {
                "type": "task.completed",
                "peer_id": PEER,
                "payload": {"task_id": RUN, "text": "PRIVATE_RAW", "status": "completed"},
                "screen": "PRIVATE_TOP",
            },
            "answerback.bus",
        )
    )
    hub.publish(model.raw({"peer_id": "hc:other", "type": "unrelated"}, "answerback.bus"))
    await flush_drawer(debug)
    records = [
        json.loads(line)
        for file in sorted((tmp_path / "debug").glob("trace*.ndjson"))
        for line in file.read_text().splitlines()
    ]
    text = json.dumps(records)
    for private in ("PRIVATE_SCREEN", "PRIVATE_RAW", "PRIVATE_TOP", "UNRELATED", "hc:other"):
        assert private not in text
    assert "sha256" in text
    assert any(row["event"]["type"] == "RAW" and RUN in row["runIds"] for row in records)
    kinds = [row["event"]["type"] for row in records]
    assert "RUN_STARTED" in kinds and "STEP_STARTED" in kinds and "SUBAGENT_STARTED" in kinds
    assert any(row["event"]["type"] == "STATE_DELTA" for row in records)
    debug.off()
    before = text
    hub.publish(model.run_finished("T-16", RUN, peer_id=PEER))
    assert before == json.dumps(
        [
            json.loads(line)
            for file in sorted((tmp_path / "debug").glob("trace*.ndjson"))
            for line in file.read_text().splitlines()
        ]
    )


async def test_selected_resource_and_tail_intervals(tmp_path: Path) -> None:
    from answerback_bridge.events.debug import DebugController

    script, clock = Script(), Clock()
    script.scan = {"peers": [peer(), peer(ident="hc:other")]}
    calls: list[tuple[float, list[str]]] = []
    tails: list[tuple[float, str]] = []
    sources = script.sources(full=True)

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        calls.append((clock.value, [row["peer_id"] for row in rows]))
        return {}

    async def tail(ident: str, row: dict[str, Any]) -> str:
        tails.append((clock.value, ident))
        return "private"

    sources.resources, sources.tail = resources, tail
    debug = DebugController(tmp_path, clock=clock)
    hub = EventHub(debug=debug)
    observer = Collector(hub, sources, clock=clock, monotonic=clock)
    start = clock.value
    debug.on(PEER, 7)
    for elapsed in range(17):
        clock.value = start + elapsed
        await observer.poll()
    assert [at - start for at, ids in calls if PEER in ids] == [0, 2, 4, 6, 15]
    assert [at - start for at, ids in calls if "hc:other" in ids] == [0, 15]
    assert [at - start for at, ident in tails if ident == PEER] == [0, 5, 15]
    assert [at - start for at, ident in tails if ident == "hc:other"] == [0, 15]
    assert debug.until(PEER) is None


async def test_normal_ps_and_optional_debug_sampler_are_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from answerback_bridge.events.debug import DebugController
    from answerback_bridge.events.resources import ProcessSampler
    from answerback_bridge.events.trace_sources import LiveResources
    from answerback_bridge.events.tracing import TraceSettings

    async def trace_pid(target: str, binding: Any) -> int:
        return 10 if target == "t61" else 20

    bridge = SimpleNamespace(
        debug=DebugController(tmp_path),
        config=SimpleNamespace(provider_enabled=lambda p: True),
        core=SimpleNamespace(trace_pid=trace_pid),
    )
    resources = LiveResources(bridge, TraceSettings())
    calls: list[tuple[ProcessSampler, set[int]]] = []

    async def sample(self: ProcessSampler, roots: set[int]) -> dict[int, Any]:
        calls.append((self, roots))
        return {}

    monkeypatch.setattr(ProcessSampler, "sample", sample)
    await resources([peer(), peer(ident="hc:other")])
    assert calls == [(resources.sampler, {10, 20})]
    calls.clear()
    bridge.debug.on(PEER)
    await resources([peer(), peer(ident="hc:other")])
    assert (resources.sampler, {20}) in calls
    assert (resources.debug_sampler, {10}) in calls
    assert resources.debug_sampler is not resources.sampler


def test_optional_psutil_detection_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from answerback_bridge.events import resources

    fake = SimpleNamespace()
    monkeypatch.setattr(resources.importlib, "import_module", lambda name: fake)
    assert resources.optional_psutil() is fake

    def absent(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(resources.importlib, "import_module", absent)
    assert resources.optional_psutil() is None


async def test_psutil_cpu_tree_uses_cumulative_time_and_no_text() -> None:
    from types import SimpleNamespace

    from answerback_bridge.events.resources import ProcessSampler, read_psutil

    clock, seconds = Clock(), [0.0]
    attributes: list[list[str]] = []

    def processes(attrs: list[str]) -> list[Any]:
        attributes.append(attrs)
        return [
            SimpleNamespace(
                info={
                    "pid": pid,
                    "ppid": parent,
                    "create_time": 50.123,
                    "cpu_times": SimpleNamespace(user=seconds[0], system=0.0),
                    "memory_info": SimpleNamespace(rss=1024),
                }
            )
            for pid, parent in ((10, 1), (11, 10))
        ]

    module = SimpleNamespace(process_iter=processes)
    sampler = ProcessSampler(lambda: read_psutil(module), clock=clock)
    assert (await sampler.sample({10}))[10]["cpuPercent"] is None
    seconds[0] = 1.0
    clock.value += 2
    assert (await sampler.sample({10}))[10] == {
        "cpuPercent": 100.0,
        "rssBytes": 2048,
        "children": 1,
    }
    assert set(attributes[0]) == {"pid", "ppid", "create_time", "cpu_times", "memory_info"}


async def test_debug_command_socket_sampler_both_boards_and_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted end-to-end: the real scheduler and socket, no model or live peer."""
    import asyncio
    import time

    from playwright.async_api import async_playwright, expect
    from test_events_bridge import running_bridge

    from answerback_bridge.bus.web import Dashboard
    from answerback_bridge.cli import dispatch
    from answerback_bridge.events import collector as module
    from answerback_bridge.events.board import Board
    from answerback_bridge.events.sse import RemoteSource

    async with running_bridge() as (service, path):
        service.bridge.event_settings = module.Settings(receipt_seconds=0.2)
        script = Script()
        script.scan = {"peers": [peer()]}
        script.tails = {PEER: "PRIVATE_E2E_SCREEN"}
        sources = script.sources(full=True)
        samples: list[float] = []

        async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
            if any(row["peer_id"] == PEER for row in rows):
                samples.append(time.monotonic())
            return {PEER: {"cpuPercent": len(samples)}}

        sources.resources = resources
        monkeypatch.setattr(module, "bridge_sources", lambda bridge: sources)
        config = ["--config", str(path)]
        await dispatch(parser().parse_args([*config, "debug", "on", PEER, "--for", "7s"]))
        task = asyncio.create_task(service.run_events())
        try:
            with Dashboard(path, source=RemoteSource(path), wait=0.05) as dashboard:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    try:
                        page = await browser.new_page()
                        await page.goto(dashboard.url)
                        row = page.get_by_test_id("board-peers-row")
                        await expect(row).to_contain_text("DEBUG")
                        board = Board()
                        for item in service.bridge.events.read()["events"]:
                            board.apply(item["id"], item["event"])
                        assert "DEBUG" in board.render()
                        await expect(row).not_to_contain_text("DEBUG", timeout=10_000)
                        board = Board()
                        for item in service.bridge.events.read()["events"]:
                            board.apply(item["id"], item["event"])
                        assert "DEBUG" not in board.render()
                    finally:
                        await browser.close()
            intervals = [b - a for a, b in zip(samples, samples[1:], strict=False)]
            assert len(intervals) == 3
            assert all(1.8 <= gap <= 2.7 for gap in intervals), intervals
            assert not (await dispatch(parser().parse_args([*config, "debug", "status"])))["scopes"]
            print(f"T-40 E2E PASS: resource intervals={intervals}; both badges on/off by timer")
        finally:
            service.stopping.set()
            await asyncio.wait_for(task, 5)


def test_drawer_rotates_with_private_permissions_and_bounded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.events import debug as module
    from answerback_bridge.events import model

    monkeypatch.setattr(module, "DRAWER_BYTES", 2048)
    controller = module.DebugController(tmp_path)
    controller.on(PEER)
    hub = EventHub(debug=controller)
    for index in range(50):
        hub.publish(model.run_started("T-40", str(index), peer_id=PEER))
        controller.flush()  # Exercise rotation independently of lossy burst admission.
    paths = list(controller.directory.glob("trace*.ndjson"))
    assert len(paths) == 2
    assert all(path.stat().st_size <= 2048 for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert controller.path.stat().st_mode & 0o777 == 0o600
    assert controller.directory.stat().st_mode & 0o777 == 0o700


async def test_missing_peers_never_keeps_debug_sampling_a_stale_scan(tmp_path: Path) -> None:
    from answerback_bridge.events.debug import DebugController

    script, clock = Script(), Clock()
    script.scan = {"peers": [peer()]}
    sources = script.sources(full=True)
    calls = []

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        calls.append(clock.value)
        return {}

    sources.resources = resources
    controller = DebugController(tmp_path, clock=clock)
    controller.on(PEER)
    observer = Collector(EventHub(debug=controller), sources, clock=clock, monotonic=clock)
    await observer.poll()
    script.scan = {"peers": []}
    clock.value += 15
    await observer.poll()
    clock.value += 2
    await observer.poll()
    assert len(calls) == 1


def test_invalid_saved_deadlines_fail_closed(tmp_path: Path) -> None:
    from answerback_bridge.events.debug import DebugController

    clock = Clock()
    debug = DebugController(tmp_path, clock=clock)
    debug.on(PEER, 3600)
    assert debug.until(PEER) == clock.value + 3600
    for lease in (
        {"enabled": True},
        {"started": clock.value, "until": clock.value + 3601},
        {"started": clock.value, "until": float("inf")},
    ):
        debug.path.write_text(json.dumps({"leases": {PEER: lease}, "excluded": []}))
        restored = DebugController(tmp_path, clock=clock)
        assert not restored.status()["scopes"]
        assert restored.status()["error"]


def test_failed_enable_does_not_leave_an_unpersisted_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.events.debug import DebugController

    debug = DebugController(tmp_path)

    def failed_save() -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(debug, "_save", failed_save)
    with pytest.raises(OSError):
        debug.on(PEER)
    assert debug.until(PEER) is None
