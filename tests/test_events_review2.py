"""Regression tests for the T-16 round-2 review (status/handoff/T-16-review.md, 979f28d).

Each test is named for the finding it closes and failed before its fix.
"""

import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from events_helpers import PEER, Script, collector, peer
from test_bus_api import configured

from answerback_bridge.bus.api import BusAPI
from answerback_bridge.events import model
from answerback_bridge.events.board import Board, _clock
from answerback_bridge.events.collector import Collector, bridge_sources, read_receipts
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.producers import RunTracker
from answerback_bridge.tasks import TaskStore

SENTINEL = "SENTINEL_d41d8cd9_PRIVATE"


def live_observer(store: TaskStore, config_path: Path | None = None) -> tuple[Collector, Script]:
    """Scripted peers, tails and board files over the real, read-only receipt store (and
    the real bus ledger reader when `config_path` names one)."""

    class Bridge:
        pass

    bridge: Any = Bridge()
    bridge.store, bridge.config_path = store, config_path
    live = bridge_sources(bridge)
    script = Script()
    observer = collector(script, full=True)
    observer.hub.receipt_path = store.path
    observer.sources.receipts = live.receipts
    observer.sources.retained = live.retained
    if config_path is not None:
        observer.sources.bus = live.bus
    return observer, script


def at(moment: str) -> int:
    return int(datetime.fromisoformat(moment).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
def london(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """This process in Europe/London: UTC+0 in winter, UTC+1 in summer."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_r2n2_a_winter_viewer_reads_a_summer_event_at_its_summer_offset(london: None) -> None:
    winter = datetime(2026, 11, 17, 12, 0, 0)  # Naive, as render() itself takes the time.
    assert winter.astimezone().strftime("%z") == "+0000"
    assert _clock(at("2026-07-01T12:00:00"), winter) == "2026-07-01 13:00:00"
    assert _clock(at("2026-11-17T09:00:00"), winter) == "09:00:00"
    assert _clock(at("2026-07-01T12:00:00")) == "2026-07-01 13:00:00"  # No viewer time given.

    hub, board = EventHub(), Board()
    old = model.run_started("T-16", "summer-run")
    old["timestamp"] = at("2026-07-01T12:00:00")
    hub.publish(old)
    for item in hub.read()["events"]:
        board.apply(item["id"], item["event"])
    drawn = board.render(now=winter)
    assert "2026-07-01 13:00:00  RUN_STARTED" in drawn
    assert "today UTC+00:00" in drawn


def test_r2n2_a_summer_viewer_reads_a_winter_event_at_its_winter_offset(london: None) -> None:
    summer = datetime(2026, 7, 1, 12, 0, 0)
    assert summer.astimezone().strftime("%z") == "+0100"
    assert _clock(at("2026-01-15T12:00:00"), summer) == "2026-01-15 12:00:00"
    assert _clock(at("2026-07-01T08:30:00"), summer) == "09:30:00"


def test_r2n2_a_named_zone_is_converted_per_timestamp_wherever_the_process_runs() -> None:
    viewer = datetime(2026, 11, 17, 12, 0, 0, tzinfo=ZoneInfo("Europe/London"))
    assert _clock(at("2026-07-01T12:00:00"), viewer) == "2026-07-01 13:00:00"
    assert _clock(at("2026-01-15T12:00:00"), viewer) == "2026-01-15 12:00:00"


async def test_f1_no_receipt_detail_result_or_prompt_reaches_any_event_type(
    tmp_path: Path,
) -> None:
    """Every producer path (run, step, error, subagent, state, activity, RAW) is driven with
    one sentinel in the prompt, the result and the detail of every receipt, on the worker's
    screen and in the bus ledger. No emitted event, state or page may carry it."""
    brief = f"Assignment. Task T-16. {SENTINEL}"
    store = TaskStore(tmp_path / "tasks.sqlite")
    config_path = configured(tmp_path)
    observer, script = live_observer(store, config_path)
    old = store.reserve(PEER, brief, "old")[0]["id"]
    store.update(old, "failed", detail=SENTINEL)
    await observer.tick()  # Backfill: what had ended before the bridge started.

    names = ("fails", "done", "stops", "sudden")
    fails, done, stops, sudden = (
        store.reserve(f"hc:{name}", brief, name)[0]["id"] for name in names
    )
    for ident in (fails, done, stops):
        store.update(ident, "submitted", detail=SENTINEL)
    store.update(sudden, "failed", detail=SENTINEL)  # First seen already failed.
    script.scan = {
        "peers": [peer("working", ident=f"hc:{name}") for name in names[:3]],
        "errors": {},
    }
    script.tails = {f"hc:{name}": f"> {SENTINEL}\ncompiling" for name in names[:3]}
    bus = BusAPI(config_path)
    row = bus.submit("kiro:worker", str(tmp_path), SENTINEL, "f1-one")
    await observer.tick()  # Runs, steps and subagents open; the bus cursor finds its end.
    bus.store().claim(row["command_id"])
    bus.store().bind(
        row["command_id"],
        {
            "id": "task-one",
            "status": "failed",
            "result": SENTINEL,
            "detail": SENTINEL,
            "updated": 10,
        },
    )
    script.tails = {f"hc:{name}": f"> {SENTINEL}\ncompiling\n{SENTINEL} more" for name in names[:3]}
    await observer.tick()  # The screens change; the bus rows arrive.
    store.update(fails, "failed", detail=SENTINEL)
    store.update(done, "completed", result=SENTINEL)
    store.update(stops, "cancelled", detail=SENTINEL)
    await observer.tick()

    hub = observer.hub
    connect = hub.read()
    stream = hub.read(f"{hub.epoch}-0", limit=1000)
    assert not stream["resync"]
    emitted = [event for _, event in hub.ring] + [item["event"] for item in connect["events"]]
    assert {event["type"] for event in emitted} >= {
        "RUN_STARTED",
        "RUN_FINISHED",
        "RUN_ERROR",
        "STEP_STARTED",
        "STEP_FINISHED",
        "SUBAGENT_STARTED",
        "SUBAGENT_FINISHED",
        "SUBAGENT_ERROR",
        "STATE_SNAPSHOT",
        "STATE_DELTA",
        "ACTIVITY_SNAPSHOT",
        "ACTIVITY_DELTA",
        "RAW",
    }
    errors = [event for event in emitted if event["type"] in ("RUN_ERROR", "SUBAGENT_ERROR")]
    assert {model.run_id_of(event) for event in errors} >= {fails, stops, sudden}
    assert {(event["type"], event["code"], event["message"]) for event in errors} == {
        ("RUN_ERROR", "failed", "Task failed"),
        ("SUBAGENT_ERROR", "failed", "Run failed"),
        ("SUBAGENT_ERROR", "cancelled", "Run cancelled"),
    }
    assert any(event["metadata"][model.NAMESPACE].get("replayed") for event in errors)
    for view in (emitted, connect, stream, hub.state, hub.activities):
        assert SENTINEL not in repr(view)
    # The reader does not even load the text it must not publish.
    rows = read_receipts(store.path, None)
    assert len(rows) == 5 and not any("detail" in row or "result" in row for row in rows)


async def test_r2n1_overlap_does_not_restart_evicted_receipts(tmp_path: Path) -> None:
    from test_events_review import lifecycle, tied_store

    store = tied_store(tmp_path, 5001)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET status='completed'")
    observer, _ = live_observer(store)
    await observer.tick_fast()
    assert lifecycle(observer) == []
    assert observer.tracker.omitted == 1
    for _ in range(3):
        await observer.tick_fast()
        assert lifecycle(observer) == []
        assert observer.tracker.omitted == 1
        assert len(observer.tracker.runs) == 5000


def test_r2n1_evicted_terminal_identity_survives_receipt_corrections() -> None:
    from events_helpers import receipt

    tracker = RunTracker(limit=1)
    tracker.observe([receipt("failed", 1, run="old"), receipt("failed", 2, run="new")])
    assert "old" not in tracker.runs
    assert tracker.observe([receipt("completed", 3, run="old")]) == []
    assert tracker.omitted == 1
    tracker.retain(set())
    assert tracker.omitted == 0


@pytest.mark.parametrize("historical", [False, True])
def test_f9_known_run_with_no_ring_events_is_incomplete(historical: bool) -> None:
    from urllib.error import HTTPError

    from test_events_stream import sse

    from answerback_bridge.events.cli import follow
    from answerback_bridge.events.sse import EventServer, LocalSource

    hub = EventHub(history=16)
    if not historical:
        hub.publish(model.run_started("T-16", "known"))
    hub.set_section("runs", {"known": {"status": "completed" if historical else "submitted"}})
    for index in range(20):
        hub.set_section("bridge", {"tick": index})
    assert not any(model.run_id_of(event) == "known" for _, event in hub.ring)
    page = hub.read(run="known")
    assert page.get("incomplete") is True
    assert page["events"] == [] and page["cursor"] is None
    with pytest.raises(ValueError, match="no longer in the retained event history"):
        list(follow(LocalSource(hub), run="known", timeout=0.1))
    with EventServer(LocalSource(hub), wait=0.1) as server:
        with pytest.raises(HTTPError) as gone:
            sse(server.url + "?run=known", server.token)
        assert gone.value.code == 410
    # An unknown future run can still be followed, and starts normally.
    future = hub.read(run="future")
    hub.publish(model.run_started("T-16", "future"))
    assert hub.read(future["cursor"], run="future")["events"][0]["event"]["type"] == "RUN_STARTED"


@pytest.mark.parametrize("status", ["completed", "failed", "pending"])
async def test_f2_store_pages_survive_cache_cap_and_retention(tmp_path: Path, status: str) -> None:
    from test_events_review import tied_store

    from answerback_bridge.events.sse import LocalSource

    store = tied_store(tmp_path, 5001)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE tasks SET status=?, message=?, detail=?, result=?",
            (status, SENTINEL, SENTINEL, SENTINEL),
        )
    observer, _ = live_observer(store)
    await observer.tick_fast()
    assert len(observer.tracker.runs) == len(observer.hub.state["runs"]) == 5000
    assert observer.tracker.omitted == 1
    board = Board()
    for item in observer.hub.read()["events"]:
        board.apply(item["id"], item["event"])
    assert "truncated, 1 more" in board.render()
    source = LocalSource(observer.hub)
    seen: set[str] = set()
    offset = 0
    while True:
        page = source.runs(offset, 100)
        assert page["total"] == 5001 and len(page["runs"]) <= 100
        assert SENTINEL not in repr(page)
        assert seen.isdisjoint(page["runs"])
        seen.update(page["runs"])
        if page["nextOffset"] is None:
            break
        offset = page["nextOffset"]
    assert seen == {f"{i:04d}" for i in range(5001)}
    # Store retention leaves only the cache-omitted receipt, outside the polling overlap.
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET updated=200 WHERE id!='0000'")
    await observer.tick_fast()
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM tasks WHERE id!='0000'")
    observer.next_sweep = 0
    await observer.tick_fast()
    assert set(observer.hub.state["runs"]) == {"0000"}
    assert observer.tracker.omitted == 0
    assert set(source.runs(0, 100)["runs"]) == {"0000"}


async def test_f2_pages_use_read_only_rpc_and_known_omitted_runs_are_incomplete() -> None:
    import asyncio

    from test_events_bridge import running_bridge

    from answerback_bridge.events.sse import RemoteSource
    from answerback_bridge.transport import READ_METHODS, RemoteBridge

    async with running_bridge() as (service, config_path):
        store = service.bridge.store
        record = store.reserve("hc:x", "Task T-16.", "page")[0]
        store.update(record["id"], "failed", detail=SENTINEL)
        source = RemoteSource(config_path)
        page = await asyncio.to_thread(source.runs, 0, 100)
        assert set(page["runs"]) == {record["id"]} and page["total"] == 1
        assert SENTINEL not in repr(page)
        assert "events_runs" in READ_METHODS
        # Nothing for this retained receipt is in the hub cache or its ring.
        run = await RemoteBridge(config_path).call("events_read", {"run": record["id"]})
        assert run["incomplete"] and run["cursor"] is None


async def test_f2_retention_does_not_require_a_full_id_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_events_review import tied_store

    from answerback_bridge.events import collector as collection

    store = tied_store(tmp_path, 7)
    observer, _ = live_observer(store)
    observer.tracker = RunTracker(limit=2)
    monkeypatch.setattr(collection, "MAX_RECEIPTS", 1)
    await observer.tick_fast()
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM tasks WHERE id>'0002'")
    observer.next_sweep = 0
    await observer.tick_fast()
    assert set(observer.hub.state["runs"]) <= {"0000", "0001", "0002"}
    assert len(observer.hub.state["runs"]) == 2 and observer.tracker.omitted == 1
    assert not observer.errors


async def test_f2_only_omitted_receipt_survives_retention(tmp_path: Path) -> None:
    from test_events_review import tied_store

    store = tied_store(tmp_path, 5)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET status='failed', updated=200 WHERE id!='0000'")
        db.execute("UPDATE tasks SET status='failed' WHERE id='0000'")
    observer, _ = live_observer(store)
    observer.tracker = RunTracker(limit=4)
    await observer.tick_fast()
    assert "0000" not in observer.hub.state["runs"]
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM tasks WHERE id!='0000'")
    observer.next_sweep = 0
    await observer.tick_fast()
    assert set(observer.hub.state["runs"]) == {"0000"}
    assert observer.hub.state["bridge"]["runsOmitted"] == 0


def test_f2_both_boards_reach_history_beyond_the_live_working_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from playwright.sync_api import expect, sync_playwright
    from test_events_review import tied_store

    from answerback_bridge.bus.cli import monitor_board
    from answerback_bridge.bus.cli import parser as bus_parser
    from answerback_bridge.bus.web import Dashboard
    from answerback_bridge.events.sse import LocalSource

    store = tied_store(tmp_path, 202)
    observer, _ = live_observer(store)
    observer.tracker = RunTracker(limit=4)
    asyncio.run(observer.tick_fast())
    source = LocalSource(observer.hub)
    args = bus_parser().parse_args(
        ["monitor", "--board", "--once", "--all", "--page", "3", "--rows", "0"]
    )
    monitor_board(args, source)
    terminal = capsys.readouterr().out.split("PEERS")[0]
    assert "0000" in terminal and "0001" in terminal and "0201" not in terminal
    assert "offset 200 of 202" in terminal

    with Dashboard(configured(tmp_path), source=source, wait=0.1) as dashboard:
        base = f"http://127.0.0.1:{dashboard.server.server_port}"
        auth = {"Authorization": f"Bearer {dashboard.server.token}"}
        for suffix, headers, code in (
            ("/api/runs", {}, 401),
            ("/api/runs?limit=101", auth, 400),
            ("/api/runs?offset=-1", auth, 400),
            ("/api/runs?limit=1&limit=2", auth, 400),
            ("/api/runs?command=submit", auth, 400),
            ("/api/runs", {**auth, "Origin": "https://elsewhere.example"}, 403),
        ):
            with pytest.raises(HTTPError) as refused:
                urlopen(Request(base + suffix, headers=headers), timeout=2)
            assert refused.value.code == code
        with urlopen(Request(base + "/api/runs?offset=200", headers=auth), timeout=2) as response:
            assert set(json.load(response)["runs"]) == {"0000", "0001"}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(dashboard.url)
                expect(page.get_by_test_id("board-status")).to_have_text("LIVE")
                expect(page.get_by_test_id("board-runs-row")).to_have_count(4)
                expect(page.get_by_test_id("board-runs-note")).to_contain_text(
                    "truncated, 198 more"
                )
                page.get_by_test_id("board-all").click()
                expect(page.get_by_test_id("board-runs-row")).to_have_count(100)
                expect(page.get_by_test_id("board-page")).to_have_text("Page 1 of 3")
                page.get_by_test_id("board-older").click()
                expect(page.get_by_test_id("board-page")).to_have_text("Page 2 of 3")
                expect(page.get_by_test_id("board-runs-row")).to_have_count(100)
                page.get_by_test_id("board-older").click()
                expect(page.get_by_test_id("board-runs-row")).to_have_count(2)
                expect(page.locator("#board-runs")).to_contain_text("0000")
                page.get_by_test_id("board-newer").click()
                expect(page.get_by_test_id("board-runs-row")).to_have_count(100)
                page.get_by_test_id("board-window").click()
                expect(page.get_by_test_id("board-runs-row")).to_have_count(4)
                # Returning to live mode releases the history page; All starts at page one.
                page.get_by_test_id("board-all").click()
                expect(page.get_by_test_id("board-page")).to_have_text("Page 1 of 3")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
