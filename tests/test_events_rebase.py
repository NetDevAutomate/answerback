"""Regression coverage for event tracing on the final T-16 event stream."""

import hashlib
from pathlib import Path

import pytest
from events_helpers import PEER, Script, peer, receipt
from test_tracing import Clock

from answerback_bridge.events import model
from answerback_bridge.events.collector import Collector
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.producers import project_root


@pytest.mark.parametrize("existing_file", [False, True], ids=["missing", "regular-file"])
def test_project_root_requires_an_existing_directory(tmp_path: Path, existing_file: bool) -> None:
    """An invalid worker directory must not select its ancestor's ledger."""
    (tmp_path / ".git").mkdir()
    candidate = tmp_path / "worker"
    if existing_file:
        candidate.write_text("not a working directory")

    assert project_root(str(candidate)) is None


async def test_full_tail_activity_and_bounded_trace_hash_coexist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change outside the trace suffix still updates T-16's activity metadata."""
    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer()]}
    script.tails = {PEER: "PREFIX_PRIVATE_FIRST\n" + "x" * 10000}
    monkeypatch.setattr(model, "now_ms", lambda: int(clock.value * 1000))
    observer = Collector(EventHub(), script.sources(full=True), clock=clock)

    await observer.tick()
    activity = observer.hub.activities[f"tail:{PEER}"][1]
    trace_hash = observer.hub.state["peers"][PEER]["trace"]["activityHash"]
    assert activity["sha256"] == hashlib.sha256(script.tails[PEER].encode()).hexdigest()
    assert activity["chars"] == len(script.tails[PEER]) and activity["lineCount"] == 2
    assert trace_hash == hashlib.sha256(b"x" * 8192).hexdigest()

    clock.value += 15
    await observer.tick()
    assert observer.hub.activities[f"tail:{PEER}"][1] == activity

    script.tails[PEER] = "PREFIX_PRIVATE_SECOND\n" + "x" * 10000
    clock.value += 15
    await observer.tick()
    changed = observer.hub.activities[f"tail:{PEER}"][1]
    assert changed["sha256"] == hashlib.sha256(script.tails[PEER].encode()).hexdigest()
    assert changed["chars"] == len(script.tails[PEER])
    assert changed["changedMs"] > activity["changedMs"]
    assert observer.hub.state["peers"][PEER]["trace"]["activityHash"] == trace_hash
    assert "PREFIX_PRIVATE" not in repr(observer.hub.read())
