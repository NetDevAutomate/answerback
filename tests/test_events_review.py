"""Regressions for the independent review of T-16 (status/handoff/T-16-review.md).

Each test is the reviewer's reproduction turned round: it asserted the defect, this asserts
the requirement. Test names carry the finding they close.
"""

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from events_helpers import PEER, Script, collector, peer, receipt, types

from answerback_bridge.adapters.builtins import Owners, builtin_plugins
from answerback_bridge.config import Config
from answerback_bridge.events import model
from answerback_bridge.events.board import Board, _clock, in_window
from answerback_bridge.events.collector import PAGE, Collector, bridge_sources, read_receipts
from answerback_bridge.events.hub import MAX_ACTIVITIES, EventHub
from answerback_bridge.events.producers import RunTracker
from answerback_bridge.events.sse import EventServer, LocalSource
from answerback_bridge.process import BridgeError
from answerback_bridge.tasks import TaskStore


@pytest.mark.parametrize("text", ["x" * 65536, "界" * 12000])
def test_t74_ring_bounds_serialized_bytes_for_custom_events(text: str) -> None:
    """red_at_base: count and bytes both bound the retained ring, including Unicode."""
    hub = EventHub(history=16)
    for index in range(20):
        hub.publish(model.custom("answerback.anomaly", {"message": text, "index": index}))
    assert len(hub.ring) == 16
    assert all(len(json.dumps(event).encode()) <= 65536 for _, event in hub.ring)
    assert all(event["name"] == "answerback.oversized" for _, event in hub.ring)


def test_t74_ring_detaches_input_before_retaining_it() -> None:
    """red_at_base: a producer cannot grow an already admitted event by mutating it."""
    hub = EventHub()
    event = model.custom("answerback.example", {"message": "small"})
    hub.publish(event)
    event["value"]["message"] = "x" * 65536
    assert hub.ring[-1][1]["value"] == {"message": "small"}


def test_t74_event_at_the_byte_limit_is_unchanged() -> None:
    """pins_existing: ordinary JSON events, including the exact ceiling, keep their fields."""
    event = model.custom("answerback.example", {"message": ""})
    event["value"]["message"] = "x" * (65536 - len(json.dumps(event).encode()))
    hub = EventHub()
    hub.publish(event)
    assert hub.ring[-1][1] == event


def test_t74_oversized_metadata_preserves_the_decision_outcome() -> None:
    """pins_existing: a small interrupt still identifies the actual pending decision."""
    outcome = model.interrupt_outcome("D-74", "decision_overdue", "A decision is overdue")
    event = model.run_finished("T-74", "run-74", outcome=outcome, peer_id="x" * 65536)
    hub = EventHub()
    hub.publish(event)
    assert hub.ring[-1][1]["outcome"] == outcome


@pytest.mark.parametrize("kind", sorted(model.LIFECYCLE))
def test_t74_oversized_lifecycle_keeps_schema_and_run_correlation(kind: str) -> None:
    """red_at_base: bounded markers still form a schema-valid run-filtered lifecycle."""
    from importlib import resources

    from jsonschema import Draft202012Validator

    ids = {"thread": "T-74", "run": "run-74"}
    events = {
        "RUN_STARTED": model.run_started("T-74", "run-74", peer_id=PEER),
        "RUN_FINISHED": model.run_finished("T-74", "run-74", outcome=dict(model.CANCELLED)),
        "RUN_ERROR": model.run_error("Failed", "failed", **ids),
        "STEP_STARTED": model.step_started("working", **ids),
        "STEP_FINISHED": model.step_finished("working", **ids),
        "SUBAGENT_STARTED": model.subagent_started(
            "sub-74", "worker", description=None, peer_id=PEER, **ids
        ),
        "SUBAGENT_FINISHED": model.subagent_finished("sub-74", peer_id=PEER, **ids),
        "SUBAGENT_ERROR": model.subagent_error("sub-74", "Failed", "failed", peer_id=PEER, **ids),
    }
    event = events[kind]
    event["rawEvent"] = "x" * 65536
    event["metadata"]["answerback"]["peerId"] = "herdr:" + "x" * 65536
    hub = EventHub()
    if kind != "RUN_STARTED":
        hub.publish(events["RUN_STARTED"])
    hub.publish(event)
    retained = hub.read(run="run-74")["events"][-1]["event"]
    assert len(json.dumps(retained).encode()) <= 65536
    assert retained["type"] == kind
    assert retained["metadata"]["answerback"]["runId"] == "run-74"
    assert retained["metadata"]["answerback"]["threadId"] == "T-74"
    assert retained["metadata"]["answerback"]["oversized"] is True
    if kind == "RUN_FINISHED":
        assert retained["outcome"] == model.CANCELLED
    schema = resources.files("answerback_bridge.events") / "schema/ag-ui-1.0.schema.json"
    Draft202012Validator(json.loads(schema.read_text())).validate(retained)


@pytest.mark.parametrize("activity", [False, True])
def test_t74_oversized_deltas_resynchronise_connected_boards(activity: bool) -> None:
    """red_at_base: dropping a delta must not leave consumers on the wrong base."""
    hub, board = EventHub(), Board()
    initial = hub.read()
    for item in initial["events"]:
        board.apply(item["id"], item["event"])
    if activity:
        hub.set_activity("tail:test", "answerback.tail", {"message": "x" * 65536})
    else:
        hub.set_section("bridge", {"large": "x" * 65536})
    assert all(len(json.dumps(event).encode()) <= 65536 for _, event in hub.ring)
    page = hub.read(initial["cursor"])
    assert page["resync"] is True
    for item in page["events"]:
        board.apply(item["id"], item["event"])
    assert board.state == hub.state
    if activity:
        assert "tail:test" not in board.activities
    hub.set_section("bridge", {"small": True})
    assert not hub.read(page["cursor"])["resync"]


def test_t74_board_sanitises_source_error_keys() -> None:
    """red_at_base: dictionary keys receive the same terminal filtering as values."""
    hub, board = EventHub(), Board()
    key = "tail:\x1b[31mpeer\x1b[0m\x07\r\n\t\u202e" + "x" * 300
    hub.set_section("bridge", {"sourceErrors": {key: "Timeout\x1bError"}})
    for item in hub.read()["events"]:
        board.apply(item["id"], item["event"])
    rendered = board.render()
    assert "SOURCES WITH ERRORS · " + model.bounded(key, 200) + ": Timeout Error" in rendered
    assert all(char.isprintable() or char == "\n" for char in rendered)


def store_observer(store: TaskStore) -> Collector:
    """A collector reading a real receipt store through the live, read-only sources."""

    class Bridge:
        pass

    bridge: Any = Bridge()
    bridge.store = store
    live = bridge_sources(bridge)
    observer = collector(Script())
    observer.sources.receipts = live.receipts
    observer.sources.retained = live.retained
    return observer


@pytest.mark.parametrize("field", ["peer_id", "target"])
async def test_t74r1_receipt_to_connect_page_bounds_peer_identity(
    tmp_path: Path, field: str
) -> None:
    """red_at_review_tip: the real receipt reader, tracker and tick cannot leak a huge ID."""
    from answerback_bridge.events.history import read_runs

    store = TaskStore(tmp_path / "tasks.sqlite")
    ident = "herdr:" + "x" * 65536
    target = ident if field == "target" else PEER
    metadata = {"peer": {"peer_id": ident}} if field == "peer_id" else {}
    row, _ = store.reserve(target, "Task T-74.", "review-T-74", metadata=metadata)
    observer = store_observer(store)
    await observer.tick_fast()
    assert observer.tracker.runs[row["id"]].peer_id == ident
    for projected in (
        observer.tracker.section()[row["id"]],
        observer.hub.state["runs"][row["id"]],
        observer.hub.read()["events"][0]["event"]["snapshot"]["runs"][row["id"]],
        read_runs(store.path)["runs"][row["id"]],
    ):
        assert projected["peerId"] == ""
        assert projected["peerIdOmitted"] is True
    assert ident not in json.dumps(observer.hub.read())


@pytest.mark.parametrize("value", ["x" * 70000, float("nan")])
def test_t74r2_identical_refused_patches_are_quiet(value: Any) -> None:
    """red_at_review_tip: one last refusal is remembered across unrelated accepted patches."""
    hub = EventHub(history=16)
    changes = [
        {"op": "add", "path": "/bridge/sibling", "value": "must remain absent"},
        {"op": "add", "path": "/bridge/large", "value": value},
    ]
    cursor = hub.read()["cursor"]
    assert hub.patch_state(changes) == []
    assert hub.state["bridge"] == {}
    first = hub.read(cursor)
    assert first["resync"] is True
    assert hub.omitted_updates == 1
    for index in range(20):
        hub.set_section("peers", {"ok": {"tick": index}})
    cursor = hub.read()["cursor"]
    seq, resync_seq = hub.seq, hub.resync_seq
    for _ in range(4):
        assert hub.patch_state([dict(op) for op in changes]) == []
        assert hub.omitted_updates == 1
        assert (hub.seq, hub.resync_seq) == (seq, resync_seq)
        assert hub.read(cursor)["resync"] is False
    changes[-1]["path"] = "/bridge/different"
    assert hub.patch_state(changes) == []
    assert hub.omitted_updates == 2
    assert hub.read(cursor)["resync"] is True
    assert hub.state["bridge"] == {}
    changes[-1]["path"] = "/bridge/large"
    hub.patch_state(changes)
    assert hub.omitted_updates == 3  # Only the last refusal is remembered.


def test_t74r1_refused_metrics_patch_keeps_state_and_reports_loss_after_ring_eviction() -> None:
    """red_at_review_tip: refused values never reach fresh or reconnecting readers."""
    hub = EventHub(history=16)
    hub.set_section("peers", {"test": {"metrics": {"tokens": 1}}})
    initial = hub.read()
    changes = [
        {"op": "replace", "path": "/peers/test/metrics/tokens", "value": 2},
        {"op": "add", "path": "/peers/test/metrics/large", "value": "x" * 65536},
    ]
    applied = hub.patch_state(changes)
    assert hub.state["peers"]["test"]["metrics"] == {"tokens": 1}
    assert applied == []
    page = hub.read(initial["cursor"])
    assert page["resync"]
    assert page["events"][0]["event"]["metadata"]["answerback"]["omittedUpdates"] == 1
    assert "x" * 65536 not in json.dumps(page)
    assert any(item["event"].get("name") == "answerback.oversized" for item in page["events"])
    for index in range(20):
        hub.publish(model.custom("answerback.example", {"n": index}))
    assert not any(event.get("name") == "answerback.oversized" for _, event in hub.ring)
    snapshot = hub.read()["events"][0]["event"]
    assert snapshot["metadata"]["answerback"]["omittedUpdates"] == 1
    assert snapshot["snapshot"]["peers"]["test"]["metrics"] == {"tokens": 1}


def test_t74r1_large_honest_state_is_delivered_in_bounded_deltas() -> None:
    """red_at_review_tip: batching cannot turn a large valid inventory into refused data."""
    hub, board = EventHub(), Board()
    page = hub.read()
    for item in page["events"]:
        board.apply(item["id"], item["event"])
    tracker = RunTracker()
    tracker.observe([receipt("completed", 101, run=f"run-{n}") for n in range(5000)])
    wanted = tracker.section()
    hub.set_section("runs", wanted)
    updates = hub.read(page["cursor"])
    assert not updates["resync"]
    assert len(updates["events"]) > 1
    for item in updates["events"]:
        assert item["event"]["type"] == "STATE_DELTA"
        assert len(json.dumps(item["event"]).encode()) <= 65536
        board.apply(item["id"], item["event"])
    assert board.state["runs"] == wanted
    snapshot = hub.read()["events"][0]["event"]
    assert len(json.dumps(snapshot).encode()) > 65536
    assert snapshot["snapshot"]["runs"] == wanted


@pytest.mark.parametrize("existing", [False, True])
def test_t74r1_refused_activity_does_not_reappear_on_connect(existing: bool) -> None:
    """red_at_review_tip: activity storage also admits before retaining reconnect content."""
    hub = EventHub()
    if existing:
        hub.set_activity("tail:test", "answerback.tail", {"message": "small"})
    hub.set_activity("tail:test", "answerback.tail", {"message": "x" * 65536})
    page = hub.read()
    assert "x" * 65536 not in json.dumps(page)
    assert page["events"][0]["event"]["metadata"]["answerback"]["omittedUpdates"] == 1
    if existing:
        assert hub.activities["tail:test"][1] == {"message": "small"}
    else:
        assert not hub.activities


def lifecycle(observer: Collector) -> list[tuple[str, str | None]]:
    return [
        (event["type"], model.run_id_of(event))
        for _, event in observer.hub.ring
        if event["type"] in model.LIFECYCLE
    ]


async def test_f2_receipts_that_ended_before_start_are_on_the_board(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    done, failed, opened = (
        store.reserve(name, "Task T-16.", name)[0]["id"] for name in ("done", "failed", "open")
    )
    store.update(done, "completed", result="ok")
    store.update(failed, "failed", detail="needs attention")
    assert {row["status"] for row in read_receipts(store.path, None)} == {
        "completed",
        "failed",
        "pending",
    }

    observer = store_observer(store)
    await observer.tick_fast()
    runs = observer.hub.state["runs"]
    assert {runs[done]["status"], runs[failed]["status"], runs[opened]["status"]} == {
        "completed",
        "failed",
        "pending",
    }
    assert [runs[ident]["attention"] for ident in (done, failed, opened)] == [False, True, False]
    # History fills the board state; only the receipt that is still open is announced.
    assert lifecycle(observer) == [("RUN_STARTED", opened), ("STEP_STARTED", opened)]
    # What ends from now on is announced as before.
    store.update(opened, "completed", result="ok")
    await observer.tick_fast()
    assert lifecycle(observer)[-1] == ("RUN_FINISHED", opened)


def test_f2_a_failed_run_outlives_any_number_of_later_completions() -> None:
    tracker = RunTracker()
    tracker.observe([receipt("failed", 1.0, run="attention")])
    tracker.observe([receipt("completed", index + 2.0, run=str(index)) for index in range(200)])
    assert "attention" in tracker.runs and len(tracker.runs) == 201 and tracker.omitted == 0


def test_f2_the_memory_bound_leaves_out_old_finished_runs_first_and_says_so() -> None:
    tracker = RunTracker(limit=4)
    tracker.observe([receipt("failed", 1.0, run="failed"), receipt("running", 2.0, run="open")])
    tracker.observe([receipt("completed", index + 3.0, run=f"done-{index}") for index in range(6)])
    assert sorted(tracker.runs) == ["done-4", "done-5", "failed", "open"]
    assert tracker.omitted == 4


async def test_f2_a_run_leaves_the_board_when_retention_deletes_its_receipt(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    kept, gone = (store.reserve(name, "Task T-16.", name)[0]["id"] for name in ("kept", "gone"))
    store.update(gone, "completed", result="ok")
    observer = store_observer(store)
    await observer.tick_fast()
    assert set(observer.hub.state["runs"]) == {kept, gone}
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM tasks WHERE id=?", (gone,))
    observer.next_sweep = 0.0
    await observer.tick_fast()
    assert set(observer.hub.state["runs"]) == {kept}
    assert observer.hub.state["bridge"]["runsOmitted"] == 0


def tied_store(folder: Path, count: int) -> TaskStore:
    """A store whose receipts all share one `updated` value, as a bulk recovery writes."""
    store = TaskStore(folder / "tasks.sqlite")
    with sqlite3.connect(store.path) as db:
        db.executemany(
            "INSERT INTO tasks(id,request_key,target,message,status,created,updated,metadata)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [
                (f"{i:04d}", f"{i:04d}", "hc:x", "Task T-16.", "submitted", 100, 100, "{}")
                for i in range(count)
            ],
        )
    return store


def test_f8_a_tie_spanning_the_page_boundary_is_drained(tmp_path: Path) -> None:
    store = tied_store(tmp_path, PAGE + 1)
    first = read_receipts(store.path, None)
    assert len(first) == PAGE
    second = read_receipts(store.path, (first[-1]["updated"], first[-1]["id"]))
    assert len(second) == 1
    assert len({row["id"] for row in first + second}) == PAGE + 1
    # Nothing follows the last row, and a later update is seen from the same position.
    position = (second[-1]["updated"], second[-1]["id"])
    assert read_receipts(store.path, position) == []
    store.update("0003", "completed", result="ok")
    assert [row["id"] for row in read_receipts(store.path, position)] == ["0003"]


async def test_f8_the_collector_reaches_every_row_of_a_tie_and_a_later_update(
    tmp_path: Path,
) -> None:
    store = tied_store(tmp_path, PAGE + 1)
    observer = store_observer(store)
    await observer.tick_fast()
    assert len(observer.hub.state["runs"]) == PAGE + 1
    store.update("0500", "completed", result="ok")
    await observer.tick_fast()
    assert observer.hub.state["runs"]["0500"]["status"] == "completed"
    # An update stamped just before the newest row already read (a slow commit) is not lost.
    with sqlite3.connect(store.path) as db:
        newest = db.execute("SELECT max(updated) FROM tasks").fetchone()[0]
        db.execute("UPDATE tasks SET status='failed', updated=? WHERE id='0001'", (newest - 1,))
    await observer.tick_fast()
    assert observer.hub.state["runs"]["0001"]["status"] == "failed"


async def test_f1_no_prompt_result_or_screen_text_reaches_the_stream() -> None:
    private = ("PROMPT_PRIVATE", "RESULT_PRIVATE", "SCREEN_PRIVATE")

    class Bridge:
        config = Config(provider_selection=["hc", "crew"], crew_enabled=True)

        def __init__(self) -> None:
            # The tails come from the plugins the service composes, as in `Bridge`.
            self.plugins = builtin_plugins(Owners.of(self))

        async def crew_read(self, target: str) -> dict[str, Any]:
            return {
                "messages": [
                    {"role": "user", "content": "PROMPT_PRIVATE"},
                    {"role": "assistant", "content": "RESULT_PRIVATE"},
                ]
            }

        async def hc_read(self, target: str, lines: int) -> dict[str, Any]:
            return {"text": "> PROMPT_PRIVATE\nSCREEN_PRIVATE compiling\nRESULT_PRIVATE\n"}

    live = bridge_sources(Bridge())
    # The sources do read private text; the test is that none of it leaves the bridge.
    assert "PROMPT_PRIVATE" in str(await live.tail("crew:chat-1", {}))
    assert "SCREEN_PRIVATE" in str(await live.tail(PEER, {}))
    script = Script()
    observer = collector(script, full=True)
    observer.sources.tail = live.tail
    script.rows = [
        receipt("submitted", 101.0),
        receipt("submitted", 101.0, run="crew-run", peer_id="crew:chat-1"),
    ]
    script.scan = {"peers": [peer("working"), peer("working", ident="crew:chat-1")], "errors": {}}
    await observer.tick()
    await observer.tick()

    hub = observer.hub
    assert set(hub.activities) == {f"tail:{PEER}", "tail:crew:chat-1"}
    everything = repr([hub.read(), list(hub.ring), hub.state, hub.activities])
    assert not [word for word in private if word in everything]
    for _, content in hub.activities.values():
        assert set(content) == {"peerId", "runId", "sha256", "lineCount", "chars", "changedMs"}
        assert len(content["sha256"]) == 64 and content["chars"] > 0
    assert "activity_lines" not in observer.settings.__dataclass_fields__


def follow(hub: EventHub, board: Board) -> None:
    for item in hub.read(board.cursor)["events"]:
        board.apply(item["id"], item["event"])


def test_f6_a_reader_holds_no_more_activities_than_the_hub() -> None:
    hub, board = EventHub(), Board()
    follow(hub, board)
    for index in range(100):
        hub.set_activity(f"tail:{index}", "answerback.tail", {"peerId": str(index), "n": 1})
        follow(hub, board)
        assert set(board.activities) == set(hub.activities)
        hub.drop_activity(f"tail:{index}")
        follow(hub, board)
    assert not hub.activities and not board.activities and board.skipped == 0

    # The hub's own bound evicts the oldest; the reader is told and lets it go too.
    for index in range(MAX_ACTIVITIES + 5):
        hub.set_activity(f"tail:{index}", "answerback.tail", {"peerId": str(index), "n": 1})
        follow(hub, board)
    assert len(hub.activities) == MAX_ACTIVITIES
    assert set(board.activities) == set(hub.activities) == set(hub.state["activities"])
    assert board.state == hub.state

    # A fresh snapshot restarts the set: nothing from before it survives.
    stale = Board()
    stale.state = {"activities": {"tail:gone": {"activityType": "answerback.tail"}}}
    stale.activities = {"tail:gone": {"peerId": "gone"}}
    for item in hub.read()["events"]:
        stale.apply(item["id"], item["event"])
    assert set(stale.activities) == set(hub.activities)
    # Content for an activity the state does not list is not kept.
    stale.apply("x", model.activity_snapshot("tail:unlisted", "answerback.tail", {"n": 1}))
    assert "tail:unlisted" not in stale.activities


async def test_f6_an_activity_is_shown_only_against_its_own_run() -> None:
    script = Script()
    observer = collector(script, full=True)
    script.tails = {PEER: "first run output\n"}
    script.rows = [receipt("submitted", 101.0, run="run-one")]
    script.scan = {"peers": [peer("working")], "errors": {}}
    await observer.tick()
    board = Board()
    follow(observer.hub, board)
    changed = board.activities[f"tail:{PEER}"]["changedMs"]

    # The peer's activity still names run-one; a second run on that peer shows nothing yet.
    observer.hub.set_section(
        "runs",
        {**observer.hub.state["runs"], "run-two": {**observer.hub.state["runs"]["run-one"]}},
    )
    follow(observer.hub, board)
    rows = [line for line in board.render().splitlines() if line.startswith("T-16")]
    cells = {line.split()[1]: line.split()[-1] for line in rows}
    assert changed and cells["run-two"] == "—" and cells["run-one"] != "—"


async def test_f5_a_reader_that_drops_inside_the_connect_page_gets_all_of_it_again() -> None:
    script = Script()
    observer = collector(script, full=True)
    script.tails = {PEER: "compiling\n"}
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer("working")], "errors": {}}
    await observer.tick()
    hub = observer.hub
    page = hub.read()
    kinds = types(page)
    assert kinds[:2] == ["STATE_SNAPSHOT", "ACTIVITY_SNAPSHOT"] and len(kinds) == 5

    # Whichever event the connection drops after, short of the last, the page comes again.
    for seen in range(1, len(kinds)):
        board = Board()
        for item in page["events"][:seen]:
            board.apply(item["id"], item["event"])
        assert board.cursor != page["cursor"]
        again = hub.read(board.cursor)
        assert again["resync"] and types(again) == kinds
        for item in again["events"]:
            board.apply(item["id"], item["event"])
        assert board.state == hub.state and set(board.activities) == set(hub.activities)
        assert len(board.recent) == 3  # Restated once, not once per attempt.
        assert board.cursor == page["cursor"]

    # A reader that saw the whole page resumes after it: nothing is sent twice.
    assert hub.read(page["cursor"]) == {"events": [], "cursor": page["cursor"], "resync": False}
    # A page of one event is complete with that event.
    alone = EventHub().read()
    assert [item["id"] for item in alone["events"]] == [alone["cursor"]]


async def test_f5_the_sse_endpoint_resends_the_connect_page_for_a_part_id() -> None:
    from test_events_stream import read_frames, sse

    script = Script()
    observer = collector(script, full=True)
    script.tails = {PEER: "compiling\n"}
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer("working")], "errors": {}}
    await observer.tick()
    with EventServer(LocalSource(observer.hub), wait=0.2) as server:
        with sse(server.url, server.token) as response:
            ((first_id, snapshot),) = read_frames(response, 1)  # ...and the reader is gone.
        assert snapshot["type"] == "STATE_SNAPSHOT" and "." in first_id
        with sse(server.url, server.token, last=first_id) as response:
            frames = read_frames(response, 5)
        assert [event["type"] for _, event in frames[:2]] == [
            "STATE_SNAPSHOT",
            "ACTIVITY_SNAPSHOT",
        ]
        assert frames[-1][0] == observer.hub.ident(observer.hub.seq)


def test_f9_a_single_run_view_never_starts_after_run_started() -> None:
    from test_events_stream import sse

    from answerback_bridge.events.cli import follow as follow_cli

    hub = EventHub(history=16)
    hub.publish(model.run_started("T-16", "r"))
    whole = hub.read(run="r")
    assert types(whole) == ["RUN_STARTED"] and "incomplete" not in whole
    for index in range(20):
        hub.set_section("runs", {"r": {"n": index}})
    hub.publish(model.run_finished("T-16", "r"))

    # RUN_STARTED has left the history: the view is refused, not served from RUN_FINISHED.
    page = hub.read(run="r")
    assert page["events"] == [] and page["incomplete"] is True and not page["resync"]
    # A reader that already has the start resumes from its position as before.
    resumed = hub.read(hub.ident(hub.seq - 1), run="r")
    assert types(resumed) == ["RUN_FINISHED"] and "incomplete" not in resumed
    # A run nobody has seen yet is an empty, complete view: it may still start.
    assert hub.read(run="later") == {"events": [], "cursor": hub.ident(hub.seq), "resync": False}

    with pytest.raises(ValueError, match="no longer in the retained event history"):
        list(follow_cli(LocalSource(hub), run="r", timeout=0.2))
    with EventServer(LocalSource(hub), wait=0.1) as server:
        with pytest.raises(HTTPError) as gone:
            sse(server.url + "?run=r", server.token)
        assert gone.value.code == 410


async def settles(reached: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not reached():
        assert time.monotonic() < deadline, "The expected state was not reached in time"
        await asyncio.sleep(0.01)


async def test_f7_a_reader_that_closes_its_socket_frees_its_capacity_at_once() -> None:
    from test_events_bridge import running_bridge

    from answerback_bridge.transport import RPC_VERSION, RemoteBridge, encode

    async with running_bridge() as (service, path):
        after = service.bridge.events.read()["cursor"]
        request = {
            "version": RPC_VERSION,
            "config_path": str(path),
            "method": "events_read",
            "params": {"after": after, "wait": 20},
        }
        writers = []
        for _ in range(8):
            _, writer = await asyncio.open_unix_connection(str(service.path))
            writer.write(encode(request))
            await writer.drain()
            writers.append(writer)
        await settles(lambda: service.event_readers == 8, 5)
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        await settles(lambda: service.event_readers == 0, 2)  # Well inside the 20 s poll.
        assert (await RemoteBridge(path).call("events_read", {}))["resync"]
        # A reader that stays is still answered when something happens.
        waiting = asyncio.create_task(
            RemoteBridge(path).call("events_read", {"after": after, "wait": 10})
        )
        await asyncio.sleep(0.2)
        service.bridge.events.set_section("runs", {"r": {"status": "pending"}})
        assert types(await asyncio.wait_for(waiting, 5)) == ["STATE_DELTA"]
        # Stopping the service ends a poll too.
        latest = service.bridge.events.ident(service.bridge.events.seq)
        held = asyncio.create_task(
            RemoteBridge(path).call("events_read", {"after": latest, "wait": 20})
        )
        await asyncio.sleep(0.2)
        assert service.event_readers == 1
        service.stopping.set()
        with pytest.raises(BridgeError):
            await asyncio.wait_for(held, 5)
        assert service.event_readers == 0


def test_f7_a_browser_that_leaves_frees_its_stream_slot_within_a_slice() -> None:
    from test_events_stream import read_frames, sse

    with EventServer(LocalSource(EventHub()), wait=30) as server:
        opened = [sse(server.url, server.token) for _ in range(4)]
        for response in opened:
            assert read_frames(response, 1)[0][1]["type"] == "STATE_SNAPSHOT"
            response.close()
        deadline = time.monotonic() + 4  # Far less than the 30-second wait.
        while True:
            try:
                with sse(server.url, server.token) as response:
                    assert read_frames(response, 1)
                break
            except HTTPError as busy:
                assert busy.code == 503 and time.monotonic() < deadline
                time.sleep(0.1)


TIME = re.compile(r"\d\d:\d\d:\d\d")
DATED = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def aged_runs(now: float) -> RunTracker:
    """The reviewer's board: one recent run and three from two days ago."""
    tracker = RunTracker()
    tracker.observe(
        [
            receipt(status, stamp, run=name, peer_id="hc:" + name, created=stamp)
            for name, status, stamp in (
                ("recent", "completed", now),
                ("old-done", "completed", now - 172800),
                ("old-failed", "failed", now - 172800),
                ("old-open", "pending", now - 172800),
            )
        ]
    )
    return tracker


def test_f3_the_terminal_board_dates_anything_not_from_today() -> None:
    zone = timezone(timedelta(hours=13))
    now = datetime(2026, 11, 17, 0, 30, 0, tzinfo=zone)
    at = lambda **back: int((now - timedelta(**back)).timestamp() * 1000)  # noqa: E731
    assert _clock(at(minutes=10), now) == "00:20:00"
    # Forty minutes ago is yesterday for this viewer, though less than a day old.
    assert _clock(at(minutes=40), now) == "2026-11-16 23:50:00"
    assert _clock(at(days=2), now) == "2026-11-15 00:30:00"
    assert _clock(None, now) == "—" and len(_clock(at(hours=24))) == 19

    hub, board = EventHub(), Board()
    hub.set_section("runs", aged_runs(now.timestamp()).section())
    old = model.run_started("T-16", "old-open")
    old["timestamp"] = at(days=2)
    hub.publish(old)
    for item in hub.read()["events"]:
        board.apply(item["id"], item["event"])
    drawn = board.render(now=now)
    rows = {line.split()[1]: line for line in drawn.splitlines() if line.startswith("T-16")}
    assert "2026-11-15 00:30:00" in rows["old-open"] and "2026-11-15" in rows["old-fail"]
    assert "00:30:00" in rows["recent"] and "2026-" not in rows["recent"]
    recent = drawn[drawn.index("RECENT") :]
    assert "2026-11-15 00:30:00  RUN_STARTED" in recent
    assert "Times are local (UTC+13:00); a date is shown for any other day." in drawn


def test_f3_the_web_board_and_the_bus_timeline_date_anything_not_from_today(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import expect, sync_playwright
    from test_bus_api import configured
    from test_bus_monitor import populate

    from answerback_bridge.bus.web import Dashboard

    path = configured(tmp_path)
    api = populate(path)
    with sqlite3.connect(api.store().path) as db:
        db.execute(
            "UPDATE events SET created=created-172800 WHERE seq=(SELECT min(seq) FROM events)"
        )
    hub = EventHub()
    hub.set_section("runs", aged_runs(time.time()).section())
    for run, age in (("old-open", 172800), ("recent", 0)):
        event = model.run_started("T-16", run)
        event["timestamp"] = int((time.time() - age) * 1000)
        hub.publish(event)
    with Dashboard(path, source=LocalSource(hub), wait=0.1) as dashboard:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(dashboard.url)
                expect(page.get_by_test_id("board-status")).to_have_text("LIVE")
                cell = lambda run: page.locator(  # noqa: E731
                    "#board-runs tr", has_text="hc:" + run
                ).locator("td:nth-child(5)")
                expect(cell("old-open")).to_have_text(DATED)
                expect(cell("old-failed")).to_have_text(DATED)
                expect(cell("recent")).to_have_text(TIME)
                assert TIME.fullmatch(cell("recent").inner_text())
                stream = page.locator("#board-events tr td:first-child")
                expect(stream).to_have_count(2)
                assert [bool(DATED.fullmatch(text)) for text in stream.all_inner_texts()] == [
                    False,
                    True,
                ]
                expect(page.get_by_test_id("board-zone")).to_have_text(
                    re.compile(r"Times are local \(UTC[+-]\d\d:\d\d\)")
                )
                # The original bus timeline follows the same rule.
                legacy = page.locator("#events .time-cell")
                expect(legacy).to_have_count(3)
                shown = legacy.all_inner_texts()
                assert sum(bool(DATED.fullmatch(text)) for text in shown) == 1
                assert sum(bool(TIME.fullmatch(text)) for text in shown) == 2
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()


def test_f4_the_terminal_board_defaults_to_24_hours_plus_open_and_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from answerback_bridge.bus.cli import monitor_board, run_cli
    from answerback_bridge.bus.cli import parser as bus_parser

    now_ms = 1_000 * 86_400_000
    run = lambda status, age, **more: {  # noqa: E731
        "status": status,
        "updatedMs": now_ms - age,
        "attention": status == "failed",
        **more,
    }
    day = 86_400_000
    assert in_window(run("completed", day), now_ms)
    assert not in_window(run("completed", day + 1), now_ms)
    assert not in_window(run("cancelled", 2 * day), now_ms)
    assert in_window(run("failed", 90 * day), now_ms)
    assert in_window(run("pending", 90 * day), now_ms)
    assert in_window(run("uncertain", 90 * day), now_ms)

    tracker = aged_runs(time.time())
    tracker.observe(
        [
            receipt("completed", time.time() - 200_000 - index, run=f"bulk-{index:02d}")
            for index in range(30)
        ]
    )
    hub = EventHub()
    hub.set_section("runs", tracker.section())
    source = LocalSource(hub)

    def board(*options: str) -> str:
        monitor_board(bus_parser().parse_args(["monitor", "--board", "--once", *options]), source)
        return capsys.readouterr().out.split("PEERS")[0]

    default = board()
    assert "RUNS · 3 · last 24 h, unfinished and failed; 31 older in --all" in default
    assert all(name in default for name in ("recent", "old-open", "old-fail"))
    assert "old-done" not in default and "bulk-" not in default

    everything = board("--all")
    assert "RUNS · 34 (+22 more; --rows 0 shows every row) · all retained history" in everything
    every_row = board("--all", "--rows", "0")
    assert "more" not in every_row and "old-done" in every_row
    assert sum(f"bulk-{index:02d}" in every_row for index in range(30)) == 30

    for options in (["--all"], ["--rows", "5"], ["--web", "--all"], ["--board", "--rows", "-1"]):
        with pytest.raises(SystemExit) as refused:
            run_cli(bus_parser().parse_args(["monitor", *options]))
        assert refused.value.code == 1
        capsys.readouterr()


def test_f4_the_web_board_defaults_to_24_hours_and_all_pages_through_history(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import expect, sync_playwright
    from test_bus_api import configured

    from answerback_bridge.bus.web import Dashboard

    now = time.time()
    tracker = aged_runs(now)
    tracker.observe(
        [
            receipt("completed", now - 200_000 - index, run=f"bulk-{index:03d}")
            for index in range(230)
        ]
    )
    hub = EventHub()
    hub.set_section("runs", tracker.section())
    with Dashboard(configured(tmp_path), source=LocalSource(hub), wait=0.1) as dashboard:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(dashboard.url)
                expect(page.get_by_test_id("board-status")).to_have_text("LIVE")
                rows = page.get_by_test_id("board-runs-row")
                table = page.locator("#board-runs")
                window, everything = (
                    page.get_by_test_id("board-window"),
                    page.get_by_test_id("board-all"),
                )

                # Default: the recent run, the old unfinished one and the old failed one.
                expect(rows).to_have_count(3)
                for shown in ("hc:recent", "hc:old-open", "hc:old-failed"):
                    expect(table).to_contain_text(shown)
                expect(table).not_to_contain_text("hc:old-done")
                expect(window).to_have_attribute("aria-pressed", "true")
                expect(everything).to_have_attribute("aria-pressed", "false")
                expect(page.locator("#board-runs-count")).to_have_text("3 of 234")
                expect(page.get_by_test_id("board-runs-note")).to_contain_text("231 older in All")
                expect(page.locator("#board-pager")).to_be_hidden()

                # All: every retained run, a page at a time, down to the oldest.
                everything.click()
                expect(everything).to_have_attribute("aria-pressed", "true")
                expect(rows).to_have_count(100)
                expect(page.locator("#board-runs-count")).to_have_text("234 of 234")
                expect(page.get_by_test_id("board-page")).to_have_text("Page 1 of 3")
                expect(page.get_by_test_id("board-newer")).to_be_disabled()
                seen = set(page.locator("#board-runs tr td:nth-child(2)").all_inner_texts())
                page.get_by_test_id("board-older").click()
                expect(page.get_by_test_id("board-page")).to_have_text("Page 2 of 3")
                expect(rows).to_have_count(100)
                page.get_by_test_id("board-older").click()
                expect(rows).to_have_count(34)
                expect(page.get_by_test_id("board-older")).to_be_disabled()
                expect(table).to_contain_text("bulk-229"[:8])
                assert "old-done" in seen and len(seen) > 3

                # Back to the default view, from any page.
                window.click()
                expect(rows).to_have_count(3)
                expect(page.locator("#board-pager")).to_be_hidden()
                assert not errors
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
