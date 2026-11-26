"""Tests for the ``hc events`` follower (T-85). A fake child stands in for ``hc``."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.providers.herdr.core_events import (
    EventFollower,
    FollowerStale,
    GapError,
    ProtocolError,
    StatusRecord,
    TerminalRecord,
    TerminalView,
    ViewLimitError,
    parse_line,
)

# ---------------------------------------------------------------- fixtures


def terminal(address: str = "t1", **changes: Any) -> dict[str, Any]:
    """A synthetic Terminal row shaped like ``hc events --json`` output."""
    return {
        "address": address,
        "argv": ["/bin/zsh"],
        "cols": 80,
        "rows": 24,
        "created_ms": 1794570000000,
        "cwd": "/work",
        "foreground": {"argv": ["claude"], "cwd": "/work/repo", "name": "claude", "pid": 101},
        "identity": "ghostty",
        "keep": False,
        "labels": {"where": "Desk"},
        "name": None,
        "parent": None,
        "pid": 100,
        "reason": None,
        "screen": "exact",
        "state": "live",
        "status": None,
        "status_ms": None,
        "title": "",
        "turn": 0,
        **changes,
    }


def snapshot_msg(seq: int, *rows: dict[str, Any]) -> dict[str, Any]:
    """The first line ``hc events --json`` prints: the bare subscribe result."""
    return {"channel": None, "seq": seq, "terminals": list(rows)}


def event_msg(seq: int, kind: str, address: str, data: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"seq": seq, "type": kind, "terminal": address, "data": data},
    }


def heartbeat_msg(seq: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "heartbeat", "params": {"seq": seq}}


def missed_msg(seq: int, *rows: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "missed", "params": snapshot_msg(seq, *rows)}


FAKE_HC = """\
import os, pathlib, sys, time
d = pathlib.Path({directory!r})
if sys.argv[1:] != ["events", "--json"]:
    sys.exit(64)
n = len(list(d.glob("started-*")))
(d / f"started-{{n}}").write_text(
    f"{{os.getpid()}} {{int('HC_TERMINAL_ID' in os.environ)}}"
)
runs = sorted(d.glob("run*.jsonl"), key=lambda p: int(p.stem[3:]))
script = runs[min(n, len(runs) - 1)]
for raw in script.read_bytes().split(b"\\n"):
    if raw.startswith(b"#exit "):
        sys.exit(int(raw[6:]))
    if raw.startswith(b"#sleep "):
        time.sleep(float(raw[7:]))
        continue
    if raw:
        sys.stdout.buffer.write(raw + b"\\n")
        sys.stdout.flush()
time.sleep(3600)
"""


class FakeHc:
    """A fake ``hc`` executable that replays one scripted run per start."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.binary = directory / "hc"
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE_HC.format(directory=str(directory)))
        self.binary.chmod(0o755)

    def run(self, index: int, *lines: dict[str, Any] | bytes | str) -> None:
        out = bytearray()
        for line in lines:
            if isinstance(line, dict):
                out += json.dumps(line).encode()
            elif isinstance(line, str):
                out += line.encode()
            else:
                out += line
            out += b"\n"
        (self.directory / f"run{index}.jsonl").write_bytes(bytes(out))

    def starts(self) -> list[tuple[int, bool]]:
        result = []
        for path in sorted(self.directory.glob("started-*"), key=lambda p: int(p.name[8:])):
            pid, env_flag = path.read_text().split()
            result.append((int(pid), env_flag == "1"))
        return result


@pytest.fixture
def fake_hc(tmp_path: Path) -> FakeHc:
    return FakeHc(tmp_path)


async def eventually(check: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def follower(fake: FakeHc, **options: Any) -> EventFollower:
    options.setdefault("backoff_initial", 0.01)
    options.setdefault("backoff_max", 0.05)
    return EventFollower(str(fake.binary), **options)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------- TerminalView


def test_snapshot_then_events_in_order() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(10, terminal("t1"), terminal("t2", title="two")))
    assert view.seq == 10
    assert set(view.terminals) == {"t1", "t2"}
    record = view.terminals["t1"]
    assert isinstance(record, TerminalRecord)
    assert record.foreground is not None and record.foreground.argv == ("claude",)

    view.apply(event_msg(11, "status", "t1", {"state": "working", "app": "claude"}))
    view.apply(event_msg(12, "title", "t1", "busy"))
    view.apply(event_msg(13, "turn", "t1", 1))
    view.apply(event_msg(14, "labels", "t1", {"where": "Lab"}))
    view.apply(event_msg(15, "created", "t3", terminal("t3")))
    view.apply(event_msg(16, "removed", "t2"))
    record = view.terminals["t1"]
    assert record.status is not None and record.status.state == "working"
    assert record.status.app == "claude"
    assert record.title == "busy"
    assert record.turn == 1
    assert dict(record.labels) == {"where": "Lab"}
    assert set(view.terminals) == {"t1", "t3"}
    assert view.seq == 16


def test_finished_event_patches_record() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    view.apply(event_msg(2, "finished", "t1", {"exit_code": 3, "reason": "exited"}))
    record = view.terminals["t1"]
    assert record.state == "finished"
    assert record.exit_code == 3
    assert record.reason == "exited"
    assert record.foreground is None


def test_lost_and_finished_without_data() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1"), terminal("t2")))
    view.apply(event_msg(2, "lost", "t1"))
    view.apply(event_msg(3, "finished", "t2"))
    assert view.terminals["t1"] == TerminalView.parse_record(terminal("t1"))
    assert view.terminals["t2"].state == "finished"
    with pytest.raises(GapError):
        view.apply(event_msg(4, "title", "t404", "unknown terminal"))


HOSTILE_IDENTITY = {
    "address": "t1",
    "state": "live",
    "cwd": "/evil",
    "labels": {"where": "Elsewhere"},
    "turn": 99,
    "pid": 4242,
    "foreground": {"argv": ["sh"], "cwd": "/", "name": "sh", "pid": 1},
    "argv": ["sh"],
}


def identity(record: TerminalRecord) -> tuple[Any, ...]:
    return (
        record.address,
        record.cwd,
        dict(record.labels),
        record.turn,
        record.pid,
        record.foreground,
        record.argv,
    )


def test_title_event_with_full_record_shape_only_patches_title() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1", turn=3)))
    before = identity(view.terminals["t1"])
    view.apply(event_msg(2, "title", "t1", {**HOSTILE_IDENTITY, "title": "new"}))
    record = view.terminals["t1"]
    assert record.title == "new"
    assert identity(record) == before


def test_status_event_with_full_record_shape_only_patches_status() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1", turn=3)))
    before = identity(view.terminals["t1"])
    data = {**HOSTILE_IDENTITY, "status": {"state": "working"}, "status_ms": 5}
    view.apply(event_msg(2, "status", "t1", data))
    record = view.terminals["t1"]
    assert record.status is not None and record.status.state == "working"
    assert record.status_ms == 5
    assert identity(record) == before
    assert record.state == "live"


def test_unknown_event_with_full_record_shape_changes_nothing() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    before = view.terminals["t1"]
    view.apply(event_msg(2, "teleported", "t1", {**HOSTILE_IDENTITY, "title": "x"}))
    view.apply(event_msg(3, "teleported", "t9", terminal("t9")))
    # The record's fields are untouched; it is only marked unverified (T-89 fix 1).
    assert view.terminals["t1"] == replace(before, unverified=True)
    assert set(view.terminals) == {"t1"}
    assert view.seq == 3


def test_finished_forced_fields_win_over_stream() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1", status={"state": "working"})))
    data = {
        "state": "running",
        "foreground": {"argv": ["sh"], "cwd": "/", "name": "sh", "pid": 1},
        "status": {"state": "working"},
        "exit_code": 0,
        "cwd": "/evil",
    }
    view.apply(event_msg(2, "finished", "t1", data))
    record = view.terminals["t1"]
    assert record.state == "finished"
    assert record.foreground is None
    assert record.status is None
    assert record.exit_code == 0
    assert record.cwd == "/work"


def test_observed_title_event_shape() -> None:
    # Real output (work Mac sample): title events carry data {title}.
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    view.apply(event_msg(2, "title", "t1", {"title": "✳ Claude Code"}))
    assert view.terminals["t1"].title == "✳ Claude Code"


def test_observed_status_event_shape() -> None:
    # Real output (work Mac sample): status events carry data {records, reported, status}.
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    data = {
        "records": {},
        "reported": True,
        "status": {"app": "claude", "state": "idle", "msg": "", "title": ""},
    }
    view.apply(event_msg(2, "status", "t1", data))
    record = view.terminals["t1"]
    assert record.status == StatusRecord(state="idle", app="claude", msg="", title="")
    view.apply(event_msg(3, "status", "t1", {"records": {}, "reported": True, "status": None}))
    assert view.terminals["t1"].status is None


def test_gap_raises_typed_error() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1")))
    with pytest.raises(GapError):
        view.apply(event_msg(7, "title", "t1", "x"))
    assert view.terminals["t1"].title == ""


def test_old_events_after_missed_are_ignored() -> None:
    # Schema: "After a missed snapshot, ignore events whose seq is not above the snapshot's."
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1")))
    view.apply(missed_msg(9, terminal("t1", title="fresh")))
    view.apply(event_msg(8, "title", "t1", "stale"))
    assert view.terminals["t1"].title == "fresh"
    assert view.seq == 9


def test_missed_replaces_state() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1"), terminal("t2")))
    view.apply(missed_msg(40, terminal("t9")))
    assert set(view.terminals) == {"t9"}
    assert view.seq == 40
    view.apply(event_msg(41, "title", "t9", "ok"))
    assert view.terminals["t9"].title == "ok"


def test_missed_without_state_requires_resync() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1")))
    with pytest.raises(GapError):
        view.apply({"jsonrpc": "2.0", "method": "missed", "params": {"seq": 9}})


def test_unknown_event_type_advances_seq() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1")))
    view.apply(event_msg(6, "teleported", "t1", {"anything": True}))
    view.apply(event_msg(7, "doc", "t1", {"name": "x"}))
    assert view.seq == 7
    expected = replace(TerminalView.parse_record(terminal("t1")), unverified=True)
    assert view.terminals["t1"] == expected


# T-89 fix 1: the view trusts a record only for event types it models.


def test_unknown_event_type_marks_only_its_terminal_unverified() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1"), terminal("t2")))
    view.apply(event_msg(2, "teleported", "t2", {"cwd": "/elsewhere"}))
    assert view.terminals["t2"].unverified is True
    assert view.terminals["t2"].cwd == "/work"
    assert view.terminals["t1"].unverified is False


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("doc", {"name": "x"}),
        ("status", {"status": {"state": "idle"}}),
        ("turn", 4),
        ("title", "new"),
        ("labels", {"where": "Desk"}),
        ("size", {"cols": 100, "rows": 30}),
        ("lost", None),
    ],
)
def test_modeled_event_types_do_not_mark_a_record_unverified(kind: str, data: Any) -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    view.apply(event_msg(2, kind, "t1", data))
    assert view.terminals["t1"].unverified is False


def test_patch_events_keep_the_unverified_flag() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1")))
    view.apply(event_msg(2, "teleported", "t1"))
    view.apply(event_msg(3, "status", "t1", {"status": {"state": "idle"}}))
    view.apply(event_msg(4, "title", "t1", "x"))
    assert view.terminals["t1"].unverified is True


@pytest.mark.parametrize("resync", ["created", "restarted", "snapshot", "missed"])
def test_a_full_record_clears_the_unverified_flag(resync: str) -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t1"), terminal("t2")))
    view.apply(event_msg(2, "teleported", "t2"))
    message = {
        "created": event_msg(3, "created", "t2", terminal("t2")),
        "restarted": event_msg(3, "restarted", "t2", terminal("t2")),
        "snapshot": snapshot_msg(3, terminal("t1"), terminal("t2")),
        "missed": missed_msg(3, terminal("t1"), terminal("t2")),
    }[resync]
    view.apply(message)
    assert view.terminals["t2"].unverified is False


def test_many_unknown_events_flag_a_record_once() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(1, terminal("t2")))
    view.apply(event_msg(2, "teleported", "t2"))
    flagged = view.terminals["t2"]
    for seq in range(3, 10_003):
        view.apply(event_msg(seq, "teleported", "t2"))
    assert view.terminals["t2"] is flagged
    assert view.seq == 10_002


def test_heartbeat_ahead_of_seq_is_a_gap() -> None:
    view = TerminalView()
    view.apply(snapshot_msg(5, terminal("t1")))
    view.apply(heartbeat_msg(5))
    with pytest.raises(GapError):
        view.apply(heartbeat_msg(6))


def test_event_before_snapshot_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        TerminalView().apply(event_msg(1, "title", "t1", "x"))


def test_more_terminals_than_cap_is_rejected() -> None:
    view = TerminalView(max_terminals=2)
    with pytest.raises(ViewLimitError):
        view.apply(snapshot_msg(1, terminal("t1"), terminal("t2"), terminal("t3")))
    view.apply(snapshot_msg(1, terminal("t1"), terminal("t2")))
    with pytest.raises(ViewLimitError):
        view.apply(event_msg(2, "created", "t3", terminal("t3")))
    assert set(view.terminals) == {"t1", "t2"}


def test_oversized_fields_are_truncated_and_flagged() -> None:
    view = TerminalView(max_text=8, max_argv=2, max_labels=1)
    row = terminal(
        "t1",
        title="x" * 100,
        argv=["a", "b", "c"],
        labels={"k1": "v", "k2": "v"},
    )
    view.apply(snapshot_msg(1, row))
    record = view.terminals["t1"]
    assert record.title == "x" * 8
    assert record.argv == ("a", "b")
    assert len(record.labels) == 1
    assert record.truncated is True
    assert view.terminals is not None


def test_invalid_records_are_protocol_errors() -> None:
    view = TerminalView()
    for bad in (
        snapshot_msg(1, terminal("not an address")),
        snapshot_msg(1, terminal("t1", turn="many")),
        snapshot_msg(1, terminal("t1", turn=True)),
        snapshot_msg(1, terminal("t1"), terminal("t1")),
        {"seq": -1, "terminals": []},
        {"something": "else"},
    ):
        with pytest.raises(ProtocolError):
            view.apply(bad)


def test_parse_line_rejects_malformed_and_oversized() -> None:
    assert parse_line(b'{"seq": 1, "terminals": []}\n', max_bytes=100) == {
        "seq": 1,
        "terminals": [],
    }
    for bad in (b"{not json\n", b"[1, 2]\n", b'{"a": 1, "a": 2}\n', b'{"a": NaN}\n', b"\xff\n"):
        with pytest.raises(ProtocolError):
            parse_line(bad, max_bytes=100)
    with pytest.raises(ProtocolError):
        parse_line(b'{"seq": 1, "terminals": []}\n', max_bytes=10)
    with pytest.raises(ProtocolError):
        parse_line(b"[" * 100_000 + b"]" * 100_000 + b"\n", max_bytes=1_000_000)


# ---------------------------------------------------------------- EventFollower


async def test_follower_applies_stream_and_scrubs_env(
    fake_hc: FakeHc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HC_TERMINAL_ID", "t42")
    fake_hc.run(0, snapshot_msg(1, terminal("t1")), event_msg(2, "title", "t1", "hello"))
    async with follower(fake_hc) as events:
        await eventually(
            lambda: (
                (snap := events.snapshot(max_age=10)) is not None and snap["t1"].title == "hello"
            )
        )
    assert fake_hc.starts()[0][1] is False


async def test_gap_makes_follower_resync(fake_hc: FakeHc) -> None:
    fake_hc.run(
        0,
        snapshot_msg(5, terminal("t1")),
        event_msg(6, "title", "t1", "a"),
        event_msg(9, "title", "t1", "skipped"),
    )
    fake_hc.run(1, snapshot_msg(20, terminal("t2", title="resynced")))
    async with follower(fake_hc) as events:
        await eventually(lambda: (snap := events.snapshot(max_age=10)) is not None and "t2" in snap)
        assert events.restarts >= 1
        assert "gap" in (events.last_error or "")
    first_pid = fake_hc.starts()[0][0]
    assert not alive(first_pid)


@pytest.mark.parametrize(
    "bad_line",
    [b"{this is not json", b'{"seq": 1, "terminals": [' + b"1," * 600 + b"1]}"],
    ids=["malformed", "oversized"],
)
async def test_bad_line_marks_stale_and_restarts(fake_hc: FakeHc, bad_line: bytes) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")), bad_line)
    fake_hc.run(1, snapshot_msg(3, terminal("t1", title="after")))
    async with follower(fake_hc, max_line_bytes=1024) as events:
        await eventually(lambda: len(fake_hc.starts()) >= 2)
        await eventually(
            lambda: (
                (snap := events.snapshot(max_age=10)) is not None and snap["t1"].title == "after"
            )
        )
        assert events.restarts >= 1


async def test_child_exit_marks_stale_and_restarts(fake_hc: FakeHc) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")), "#sleep 0.2", "#exit 3")
    fake_hc.run(1, "#sleep 3600")
    async with follower(fake_hc) as events:
        await eventually(lambda: events.snapshot(max_age=10) is not None)
        await eventually(lambda: len(fake_hc.starts()) >= 2)
        assert events.snapshot(max_age=10) is None
        assert "exit 3" in (events.last_error or "")


async def test_backoff_is_exponential_and_capped(fake_hc: FakeHc) -> None:
    fake_hc.run(0, "#exit 1")
    delays: list[float] = []
    enough = asyncio.Event()

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 7:
            enough.set()
        await asyncio.sleep(0)

    events = EventFollower(str(fake_hc.binary), backoff_initial=0.1, backoff_max=1.0, sleep=record)
    await events.start()
    await asyncio.wait_for(enough.wait(), 10)
    await events.stop()
    assert delays[:7] == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0]


async def test_cancel_leaves_no_child_running(fake_hc: FakeHc) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")))
    events = follower(fake_hc)
    task = asyncio.create_task(events.run())
    await eventually(lambda: events.snapshot(max_age=10) is not None)
    ((pid, _),) = fake_hc.starts()
    assert alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(pid)
    assert events.snapshot(max_age=10) is None


async def test_stop_kills_and_reaps_child(fake_hc: FakeHc) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")))
    events = follower(fake_hc)
    await events.start()
    await eventually(lambda: events.snapshot(max_age=10) is not None)
    ((pid, _),) = fake_hc.starts()
    await events.stop()
    assert not alive(pid)


async def test_snapshot_none_when_stale_or_old(fake_hc: FakeHc) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")))
    now = [100.0]
    events = follower(fake_hc, clock=lambda: now[0])
    assert events.snapshot(max_age=10) is None
    await events.start()
    try:
        await eventually(lambda: events.snapshot(max_age=10) is not None)
        now[0] += 11
        assert events.snapshot(max_age=10) is None
        assert events.snapshot(max_age=60) is not None
        with pytest.raises(ValueError):
            events.snapshot(max_age=0)
    finally:
        await events.stop()
    assert events.snapshot(max_age=60) is None


async def test_wait_for_resolves_on_matching_status(fake_hc: FakeHc) -> None:
    fake_hc.run(
        0,
        snapshot_msg(1, terminal("t1"), terminal("t2")),
        "#sleep 0.3",
        event_msg(2, "status", "t2", {"state": "idle"}),
        event_msg(3, "status", "t1", {"state": "working"}),
        event_msg(4, "turn", "t1", 1),
        event_msg(5, "status", "t1", {"state": "idle"}),
    )
    async with follower(fake_hc) as events:
        await eventually(lambda: events.snapshot(max_age=10) is not None)

        def idle(record: TerminalRecord) -> bool:
            return record.status is not None and record.status.state == "idle"

        record = await events.wait_for("t1", idle, timeout=5)
        assert record.address == "t1"
        assert record.turn == 1
        with pytest.raises(TimeoutError):
            await events.wait_for("t1", lambda r: r.turn > 5, timeout=0.1)


async def test_wait_for_fails_when_follower_goes_stale(fake_hc: FakeHc) -> None:
    fake_hc.run(0, snapshot_msg(1, terminal("t1")), "#sleep 0.3", "#exit 2")
    fake_hc.run(1, "#sleep 3600")
    async with follower(fake_hc) as events:
        await eventually(lambda: events.snapshot(max_age=10) is not None)
        with pytest.raises(FollowerStale):
            await events.wait_for("t1", lambda r: False, timeout=5)
        with pytest.raises(FollowerStale):
            await events.wait_for("t1", lambda r: False, timeout=5)


async def test_wait_for_rejects_bad_address(fake_hc: FakeHc) -> None:
    events = follower(fake_hc)
    with pytest.raises(ValueError):
        await events.wait_for("rm -rf", lambda r: True, timeout=1)


async def test_missing_binary_retries_with_backoff(tmp_path: Path) -> None:
    delays: list[float] = []
    enough = asyncio.Event()

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 3:
            enough.set()
        await asyncio.sleep(0)

    events = EventFollower(str(tmp_path / "missing-hc"), backoff_initial=0.5, sleep=record)
    await events.start()
    await asyncio.wait_for(enough.wait(), 5)
    await events.stop()
    assert delays[:3] == [0.5, 1.0, 2.0]
    assert events.snapshot(max_age=10) is None


async def test_waiters_are_indexed_capped_and_released(
    fake_hc: FakeHc, monkeypatch: pytest.MonkeyPatch
) -> None:
    import answerback_bridge.providers.herdr.core_events as module

    monkeypatch.setattr(module, "MAX_WAITERS", 2)
    fake_hc.run(0, snapshot_msg(1, terminal("t1"), terminal("t2")))
    async with follower(fake_hc) as events:
        await eventually(lambda: events.snapshot(max_age=10) is not None)
        first = asyncio.create_task(events.wait_for("t1", lambda r: False, timeout=5))
        second = asyncio.create_task(events.wait_for("t2", lambda r: False, timeout=5))
        await asyncio.sleep(0.01)
        with pytest.raises(module.EventStreamError):
            await events.wait_for("t2", lambda r: False, timeout=1)
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        # Released slots are reusable and no empty per-address group lingers.
        with pytest.raises(TimeoutError):
            await events.wait_for("t1", lambda r: False, timeout=0.05)
        assert events._waiters == {}
        assert events._waiter_count == 0
