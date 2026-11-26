"""Stream behaviour: ordering, snapshot-then-delta, reconnect replay, and the SSE endpoint."""

import json
import socket
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from events_helpers import (
    PEER,
    RUN,
    Script,
    acceptance_run,
    collector,
    in_order,
    peer,
    receipt,
    types,
)

from answerback_bridge.events import model
from answerback_bridge.events.board import Board
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.sse import EventServer, LocalSource

ACCEPTANCE = ["RUN_STARTED", "SUBAGENT_STARTED", "STATE_DELTA", "RUN_FINISHED"]


async def test_scripted_receipt_and_peer_produce_the_lifecycle_in_order() -> None:
    script = Script()
    observer = collector(script)
    cursor = observer.hub.read()["cursor"]
    await acceptance_run(script, observer)

    page = observer.hub.read(cursor)
    assert in_order(types(page), ACCEPTANCE)
    lifecycle = [
        item["event"] for item in page["events"] if item["event"]["type"] in model.LIFECYCLE
    ]
    assert [event["type"] for event in lifecycle] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "SUBAGENT_STARTED",
        "STEP_FINISHED",
        "STEP_STARTED",
        "STEP_FINISHED",
        "SUBAGENT_FINISHED",
        "RUN_FINISHED",
    ]
    started, finished = lifecycle[0], lifecycle[-1]
    assert (started["threadId"], started["runId"]) == ("T-16", RUN)
    assert started["protocolVersion"] == "1.0"
    assert [event["stepName"] for event in lifecycle if "stepName" in event] == [
        "pending",
        "pending",
        "submitted",
        "submitted",
    ]
    assert lifecycle[2]["subagentRunId"] == lifecycle[-2]["subagentRunId"] == f"{PEER}#{RUN}"
    # The result body never enters the stream, only its size.
    assert finished["result"] == {"status": "completed", "resultChars": 42}
    assert all(model.run_id_of(event) == RUN for event in lifecycle)
    state = observer.hub.state
    assert state["runs"][RUN]["status"] == "completed"
    assert state["peers"][PEER]["metrics"] == {
        "tokens": None,
        "cpuPercent": None,
        "rssBytes": None,
        "children": None,
    }


async def test_snapshot_then_deltas_reconstruct_the_same_state() -> None:
    script = Script()
    observer = collector(script, full=True)
    script.tails = {PEER: "compiling\n"}
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer("working")], "complete": True, "errors": {}}
    await observer.tick()

    board = Board()
    page = observer.hub.read()
    assert types(page)[0] == "STATE_SNAPSHOT" and page["resync"]
    # A reader that has just connected is also told what recently happened, marked as such.
    # Only the last event of the page carries the stream position (review F5).
    assert types(page)[1:] == [
        "ACTIVITY_SNAPSHOT",
        "RUN_STARTED",
        "STEP_STARTED",
        "SUBAGENT_STARTED",
    ]
    assert [item["id"] for item in page["events"]] == [
        *(f"{page['cursor']}.{part}" for part in range(4)),
        page["cursor"],
    ]
    assert all(
        item["event"]["metadata"]["answerback"]["replayed"] is True for item in page["events"][2:]
    )
    assert "replayed" not in observer.hub.ring[0][1].get("metadata", {}).get("answerback", {})
    for item in page["events"]:
        board.apply(item["id"], item["event"])
    assert board.state == observer.hub.state

    script.tails = {PEER: "compiling\nrunning 2068 tests\n"}
    script.scan = {
        "peers": [peer("working"), peer("idle", ident="crew:chat-9", name="a/b ~ worker")],
        "complete": True,
        "errors": {},
    }
    script.board = {
        "decisions": {},
        "applyQueue": {"/work/answer~back": {"pending": ["T-16"], "done": []}},
        "heads": {"/work/answer~back": {"/tmp/wt/a": {"branch": "task/x", "sha": "abc"}}},
    }
    await observer.tick()
    script.rows = [receipt("completed", 105.0)]
    script.scan = {"peers": [peer("idle")], "complete": True, "errors": {}}
    await observer.tick()

    page = observer.hub.read(board.cursor)
    assert not page["resync"] and "STATE_DELTA" in types(page) and "ACTIVITY_DELTA" in types(page)
    for item in page["events"]:
        board.apply(item["id"], item["event"])
    assert board.state == observer.hub.state
    assert board.skipped == 0
    assert "crew:chat-9" not in board.state["peers"]
    assert board.state["heads"]["/work/answer~back"]["/tmp/wt/a"]["sha"] == "abc"
    # The run closed, so its tail is no longer tracked for new readers.
    assert observer.hub.activities == {}
    # The scripted receipt is dated 1970: finished, it is history, shown by the All view.
    assert "T-16" not in board.render().split("PEERS")[0]
    assert "T-16" in board.render(history=True) and "completed" in board.render(history=True)


async def test_reconnect_replays_from_the_last_event_id() -> None:
    script = Script()
    observer = collector(script)
    hub = observer.hub
    first = hub.read()
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer("working")], "complete": True, "errors": {}}
    await observer.tick()
    seen = hub.read(first["cursor"], limit=2)
    assert len(seen["events"]) == 2
    last_id = seen["events"][-1]["id"]

    # The connection drops here. More happens, then the client returns with its last ID.
    script.rows = [receipt("completed", 102.0)]
    await observer.tick()
    replay = hub.read(last_id)
    assert not replay["resync"] and "STATE_SNAPSHOT" not in types(replay)
    everything = hub.read(first["cursor"])
    assert [item["id"] for item in seen["events"] + replay["events"]] == [
        item["id"] for item in everything["events"]
    ]
    assert in_order(types(replay), ["SUBAGENT_FINISHED", "RUN_FINISHED", "STATE_DELTA"])
    # Nothing new: the cursor holds and the page is empty.
    assert hub.read(replay["cursor"]) == {"events": [], "cursor": replay["cursor"], "resync": False}


def test_a_cursor_outside_the_history_or_from_another_bridge_is_resynchronised() -> None:
    hub = EventHub(history=16)
    start = hub.read()["cursor"]
    for index in range(40):
        hub.set_section("runs", {"r": {"n": index}})
    fallen_behind = hub.read(start)
    assert fallen_behind["resync"] and types(fallen_behind) == ["STATE_SNAPSHOT"]
    assert fallen_behind["events"][0]["event"]["snapshot"]["runs"] == {"r": {"n": 39}}
    assert len(hub.ring) == 16

    restarted = EventHub()
    other = restarted.read(fallen_behind["cursor"])
    assert other["resync"] and other["cursor"].startswith(restarted.epoch)
    for bad in ("0", "zzzzzzzz-1", f"{hub.epoch}-x", 7):
        with pytest.raises(ValueError):
            hub.read(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hub.read(limit=0)


async def test_run_filter_is_one_conformant_single_run_stream() -> None:
    script = Script()
    observer = collector(script)
    script.rows = [receipt("submitted", 101.0), receipt("running", 101.5, run="other-run")]
    script.scan = {"peers": [peer("working")], "complete": True, "errors": {}}
    await observer.tick()
    script.rows = [receipt("failed", 103.0, detail="Worker exited 1")]
    await observer.tick()

    events = [item["event"] for item in observer.hub.read(run=RUN)["events"]]
    assert events[0]["type"] == "RUN_STARTED" and events[-1]["type"] == "RUN_ERROR"
    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "SUBAGENT_STARTED",
        "STEP_FINISHED",
        "SUBAGENT_ERROR",
        "RUN_ERROR",
    ]
    # The receipt's detail is free text: the stream says only that the task failed.
    assert events[-1]["message"] == "Task failed" and events[-1]["code"] == "failed"
    # A later correction of a closed receipt updates state but emits nothing after RUN_ERROR.
    script.rows = [receipt("completed", 104.0)]
    await observer.tick()
    assert len(observer.hub.read(run=RUN)["events"]) == len(events)
    assert observer.hub.state["runs"][RUN]["status"] == "completed"


def sse(url: str, token: str | None, *, last: str | None = None, **headers: str) -> Any:
    values = dict(headers)
    if token is not None:
        values["Authorization"] = f"Bearer {token}"
    if last is not None:
        values["Last-Event-ID"] = last
    return urlopen(Request(url, headers=values), timeout=5)


def read_frames(response: Any, count: int) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    ident = ""
    while len(frames) < count:
        line = response.readline().decode().rstrip("\n")
        if line.startswith("id: "):
            ident = line[4:]
        elif line.startswith("data: "):
            frames.append((ident, json.loads(line[6:])))
    return frames


async def test_sse_endpoint_shows_the_acceptance_events_in_order_and_resumes() -> None:
    script = Script()
    observer = collector(script)
    with EventServer(LocalSource(observer.hub), wait=0.2) as server:
        assert server.url.startswith("http://127.0.0.1:") and server.token not in server.url
        with sse(server.url, server.token) as response:
            assert response.headers["Content-Type"].startswith("text/event-stream")
            assert response.headers["Cache-Control"] == "no-store"
            ((first_id, snapshot),) = read_frames(response, 1)
            assert snapshot["type"] == "STATE_SNAPSHOT"
            await acceptance_run(script, observer)
            total = len(observer.hub.ring) - int(first_id.split("-")[1])
            frames = read_frames(response, total)
        kinds = [event["type"] for _, event in frames]
        assert in_order(kinds, ACCEPTANCE)
        assert frames[-1][0] == observer.hub.ident(observer.hub.seq)

        # Reconnect with the ID of the third event: exactly the rest is replayed, no snapshot.
        with sse(server.url, server.token, last=frames[2][0]) as response:
            replay = read_frames(response, len(frames) - 3)
        assert replay == frames[3:]

        with sse(server.url + f"?run={RUN}", server.token) as response:
            only = [event["type"] for _, event in read_frames(response, 8)]
        assert only[0] == "RUN_STARTED" and only[-1] == "RUN_FINISHED"


def test_sse_endpoint_is_local_authenticated_and_read_only() -> None:
    with EventServer(LocalSource(EventHub()), wait=0.1) as server:
        for token, headers, code in (
            (None, {}, 401),
            ("wrong", {}, 401),
            (server.token, {"Host": "attacker.example"}, 403),
            (server.token, {"Origin": "https://attacker.example"}, 403),
        ):
            with pytest.raises(HTTPError) as denied:
                sse(server.url, token, **headers)
            assert denied.value.code == code
        with pytest.raises(HTTPError) as unauthenticated:
            sse(server.url, None)
        assert unauthenticated.value.headers["WWW-Authenticate"] == "Bearer"
        base = server.url.removesuffix("/events")
        for method, path, code in (
            ("POST", "/events", 405),
            ("PUT", "/events", 405),
            ("DELETE", "/events", 405),
            ("GET", "/", 404),
            ("GET", "/events?after=bogus", 400),
            ("GET", "/events?token=" + server.token, 400),
        ):
            request = Request(
                base + path, headers={"Authorization": f"Bearer {server.token}"}, method=method
            )
            with pytest.raises(HTTPError) as refused:
                urlopen(request, timeout=5)
            assert refused.value.code == code
        assert server.server.server_address[0] == "127.0.0.1"


def test_t191_slow_headers_expire_before_exhausting_handler_slots() -> None:
    """red_at_76db6d7a: trickled pre-auth bytes hold all eight handler slots."""
    with EventServer(LocalSource(EventHub()), wait=0.1) as server:
        clients: list[socket.socket] = []
        try:
            for _ in range(8):
                client = socket.create_connection(server.server.server_address, timeout=2)
                client.settimeout(2)
                client.sendall(b"G")
                clients.append(client)
            time.sleep(1)
            for client in clients:
                client.sendall(b"E")
            with socket.create_connection(server.server.server_address, timeout=2) as busy:
                busy.settimeout(2)
                busy.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                assert busy.recv(1024).startswith(b"HTTP/1.0 503 Busy")
            time.sleep(2.5)
            with sse(server.url, server.token) as response:
                assert response.status == 200
        finally:
            for client in clients:
                client.close()


def test_only_a_bounded_number_of_streams_are_served() -> None:
    with EventServer(LocalSource(EventHub()), wait=0.1) as server:
        opened = [sse(server.url, server.token) for _ in range(4)]
        try:
            with pytest.raises(HTTPError) as busy:
                sse(server.url, server.token)
            assert busy.value.code == 503
        finally:
            for response in opened:
                response.close()
