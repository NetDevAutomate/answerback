"""Worker health through the collector's scripted input and event-stream boundary."""

from dataclasses import replace
from typing import Any

import pytest
from events_helpers import PEER, RUN, Script, peer, receipt

from answerback_bridge.events.collector import Collector, Settings
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.tracing import TraceSettings


class Clock:
    value = 702.0

    def __call__(self) -> float:
        return self.value


def anomalies(hub: EventHub) -> list[dict[str, Any]]:
    return [event["value"] for _, event in hub.ring if event.get("name") == "answerback.anomaly"]


async def test_stuck_receipt_alerts_within_one_interval_and_names_the_guard() -> None:
    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer()], "complete": True}
    script.tails = {PEER: "unchanged"}
    observer = Collector(EventHub(), script.sources(full=True), clock=clock)
    await observer.tick()
    assert anomalies(observer.hub) == []
    clock.value += 15
    await observer.tick()
    (event,) = anomalies(observer.hub)
    assert (event["reason"], event["runId"], event["peerId"]) == ("stuck", RUN, PEER)
    assert event["configKey"] == "tracing.stuck_seconds" and event["threshold"] == 600
    assert "600" in event["message"] and "tracing.stuck_seconds" in event["message"]
    await observer.tick()
    assert len(anomalies(observer.hub)) == 1


@pytest.mark.parametrize(
    ("reason", "status", "state", "age", "key", "threshold"),
    [
        ("hung", "submitted", "blocked", 3601, "hung_seconds", 3600),
        ("undelivered", "pending", "done", 40, "undelivered_seconds", 30),
        ("undelivered", "submitted", "idle", 40, "undelivered_seconds", 30),
        ("orphan", None, "working", 40, "orphan_seconds", 30),
    ],
)
async def test_receipt_state_detectors_require_repeated_evidence(
    reason: str, status: str | None, state: str, age: float, key: str, threshold: float
) -> None:
    script, clock = Script(), Clock()
    clock.value = 101 + age
    if status:
        script.rows = [receipt(status, 101)]
    script.scan = {"peers": [peer(state)], "complete": True}
    observer = Collector(EventHub(), script.sources(), clock=clock)
    await observer.tick()
    assert not anomalies(observer.hub)
    for _ in range(3):
        clock.value += 15
        await observer.tick()
    (event,) = anomalies(observer.hub)
    assert event["reason"] == reason and event["threshold"] == threshold
    assert event["configKey"] == f"tracing.{key}"
    assert observer.hub.state["peers"][PEER]["trace"]["anomalies"] == [reason]


async def test_t191_decision_file_alias_never_clears_a_stuck_guard() -> None:
    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer()], "complete": True}
    script.tails = {PEER: "one"}
    settings = Settings(tracing=TraceSettings(human_aliases=("owner",)))
    observer = Collector(EventHub(), script.sources(full=True), settings, clock=clock)
    await observer.tick()
    clock.value += 15
    script.tails[PEER] = "two"  # The model talks; guard age keeps advancing.
    await observer.tick()
    for _ in range(2):
        clock.value += 15
        await observer.tick()
    assert [e["reason"] for e in anomalies(observer.hub)] == ["stuck"]

    answer = {
        "task": "T-16",
        "class": "reserved",
        "overdue": False,
        "status": "answered",
        "answered": True,
        "answeredBy": "owner",
        "answeredMs": int(clock.value * 1000),
        "recommendationOnly": False,
        "project": "/wrong/project",
    }
    script.board["decisions"] = {"D-1": answer}
    clock.value += 15
    await observer.tick()
    assert observer.hub.state["peers"][PEER]["trace"]["anomalies"] == ["stuck"]
    answer["project"] = "/work/answerback"
    answer["recommendationOnly"] = True
    clock.value += 15
    await observer.tick()
    assert observer.hub.state["peers"][PEER]["trace"]["anomalies"] == ["stuck"]
    answer["recommendationOnly"] = False
    clock.value += 15
    await observer.tick()
    assert observer.hub.state["peers"][PEER]["trace"]["anomalies"] == ["stuck"]
    assert len(anomalies(observer.hub)) == 1
    for _ in range(41):
        clock.value += 15
        await observer.tick()
    assert len(anomalies(observer.hub)) == 1


async def test_a_missing_sample_breaks_confirmation_and_never_proves_recovery() -> None:
    script, clock = Script(), Clock()
    script.rows = [receipt("pending", 101)]
    script.scan = {"peers": [peer("done")], "complete": True}
    observer = Collector(EventHub(), script.sources(), clock=clock)
    await observer.tick()
    script.scan = {"peers": [], "errors": {"hc": "offline"}}
    clock.value += 15
    await observer.tick()
    assert observer.hub.state["peers"][PEER]["trace"]["fresh"] is False
    script.scan = {"peers": [peer("done")]}
    clock.value += 15
    await observer.tick()
    assert not anomalies(observer.hub)
    clock.value += 15
    await observer.tick()
    assert len(anomalies(observer.hub)) == 1


def test_tracing_settings_load_all_thresholds_and_reject_invalid_numbers(tmp_path: Any) -> None:
    import json

    values = {"stuck_seconds": 5, "hung_seconds": 10, "cpu_percent": 90}
    (tmp_path / "events.json").write_text(json.dumps({"tracing": values}))
    settings = Settings.load(tmp_path / "config.json")
    assert settings.tracing.stuck_seconds == 5 and settings.tracing.cpu_percent == 90
    for changes in ({"stuck_seconds": float("nan")}, {"confirm_samples": 1}, {"tail_bytes": True}):
        with pytest.raises(ValueError):
            replace(settings.tracing, **changes)


@pytest.mark.parametrize(
    ("metric", "value", "key", "passes"),
    [
        ("cpuPercent", 90, "cpu_percent", 21),
        ("rssBytes", 5 * 1024**3, "rss_bytes", 2),
        ("children", 21, "children", 2),
    ],
)
async def test_hot_worker_metrics_use_sustained_samples(
    metric: str, value: float, key: str, passes: int
) -> None:
    script, clock = Script(), Clock()
    script.scan = {"peers": [peer("idle")]}
    sources = script.sources()

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {PEER: {"cpuPercent": 1, "rssBytes": 1024, "children": 0, metric: value}}

    sources.resources = resources
    observer = Collector(EventHub(), sources, clock=clock)
    for index in range(passes):
        await observer.tick()
        if index == 0:
            assert not anomalies(observer.hub)
        clock.value += 15
    (event,) = anomalies(observer.hub)
    assert event["reason"] == "hot" and event["configKey"] == f"tracing.{key}"
    metrics = observer.hub.state["peers"][PEER]["metrics"]
    assert metrics[metric] == value and metrics["tokens"] is None
    assert not observer.hub.state["bridge"]["sourceErrors"]


async def test_ps_sampler_attributes_a_busy_child_to_only_its_foreground_tree() -> None:
    from answerback_bridge.events.resources import ProcessSampler

    clock = Clock()
    text = (
        "10 1 00:01.00 100 Tue Nov 17 00:00:00 2026\n"
        "11 10 00:01.00 200 Tue Nov 17 00:00:00 2026\n"
        "12 1 10:00.00 9000 Tue Nov 17 00:00:00 2026\n"
    )

    async def read() -> str:
        return text

    sampler = ProcessSampler(read, clock=clock)
    first = await sampler.sample({10})
    assert first[10] == {"cpuPercent": None, "rssBytes": 307200, "children": 1}
    clock.value += 15
    text = text.replace("11 10 00:01.00", "11 10 00:14.50")
    second = await sampler.sample({10})
    assert second[10] == {"cpuPercent": 90.0, "rssBytes": 307200, "children": 1}
    # Reused pid is a new measurement baseline, never a negative/spurious rate.
    clock.value += 15
    text = text.replace("00:14.50", "00:00.10").replace("00:00:00 2026", "00:01:00 2026")
    assert (await sampler.sample({10}))[10]["cpuPercent"] is None


async def test_ps_sampler_keeps_cpu_delta_when_a_new_descendant_appears() -> None:
    from answerback_bridge.events.resources import ProcessSampler

    clock = Clock()
    text = (
        "10 1 00:01.00 100 Tue Nov 17 00:00:00 2026\n11 10 00:01.00 200 Tue Nov 17 00:00:00 2026\n"
    )

    async def read() -> str:
        return text

    sampler = ProcessSampler(read, clock=clock)
    assert (await sampler.sample({10}))[10]["cpuPercent"] is None
    clock.value += 15
    text = (
        "10 1 00:02.50 100 Tue Nov 17 00:00:00 2026\n"
        "11 10 00:14.50 200 Tue Nov 17 00:00:00 2026\n"
        "12 11 00:09.00 300 Tue Nov 17 00:00:15 2026\n"
    )
    second = (await sampler.sample({10}))[10]
    assert isinstance(second["cpuPercent"], float) and second["cpuPercent"] > 0
    assert second == {"cpuPercent": 100.0, "rssBytes": 614400, "children": 2}


async def test_all_workers_get_tail_samples_and_a_failed_tail_is_unknown() -> None:
    script, clock = Script(), Clock()
    identifiers = [f"hc:t{index}" for index in range(10)]
    script.rows = [receipt("submitted", 101, run=i, peer_id=i) for i in identifiers]
    script.scan = {"peers": [peer(ident=i) for i in identifiers]}
    script.tails = {i: "unchanged" for i in identifiers}
    sources = script.sources(full=True)

    async def tail(ident: str, record: dict[str, Any]) -> str:
        if ident == "hc:t0":
            raise OSError("gone")
        return script.tails[ident]

    sources.tail = tail
    observer = Collector(EventHub(), sources, clock=clock)
    await observer.tick()
    clock.value += 15
    await observer.tick()
    assert {e["peerId"] for e in anomalies(observer.hub)} == set(identifiers[1:])
    assert observer.hub.state["peers"]["hc:t0"]["trace"]["activityHash"] is None


async def test_hung_receipt_is_detected_even_when_its_peer_is_gone() -> None:
    script, clock = Script(), Clock()
    clock.value = 3702
    script.rows = [receipt("submitted", 101)]
    observer = Collector(EventHub(), script.sources(), clock=clock)
    await observer.tick()
    clock.value += 15
    await observer.tick()
    (event,) = anomalies(observer.hub)
    assert event["reason"] == "hung" and event["runId"] == RUN


async def test_collector_loop_observes_without_readers_and_stops_cleanly() -> None:
    import asyncio

    script = Script()
    stopping = asyncio.Event()
    sources = script.sources()

    async def peers() -> dict[str, Any]:
        stopping.set()
        return {"peers": [peer("idle")]}

    sources.peers = peers
    observer = Collector(EventHub(), sources)
    async with asyncio.timeout(1):
        await observer.run(stopping)
    assert observer.hub.state["peers"][PEER]["trace"]["fresh"]


async def test_changed_activity_does_not_reset_guard_before_first_confirmation() -> None:
    # Continuous model output cannot reset the age, but a repeated sample is still
    # required before stuck fires.
    script, clock = Script(), Clock()
    clock.value = 102
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer()]}
    script.tails = {PEER: "first"}
    observer = Collector(EventHub(), script.sources(full=True), clock=clock)
    for index in range(41):
        script.tails[PEER] = str(index)
        await observer.tick()
        clock.value += 15
    assert not anomalies(observer.hub)
    await observer.tick()
    clock.value += 15
    await observer.tick()
    assert [e["reason"] for e in anomalies(observer.hub)] == ["stuck"]


async def test_orphan_has_a_bounded_activity_hash_and_state_timeline() -> None:
    script, clock = Script(), Clock()
    script.scan = {"peers": [peer()]}
    script.tails = {PEER: "prefix" + "x" * 10000}
    observer = Collector(
        EventHub(),
        script.sources(full=True),
        Settings(tracing=TraceSettings(timeline_limit=2)),
        clock=clock,
    )
    await observer.tick()
    trace = observer.hub.state["peers"][PEER]["trace"]
    assert trace["activityHash"] is not None
    script.tails[PEER] = "different-prefix" + "x" * 10000
    clock.value += 15
    await observer.tick()
    assert observer.hub.state["peers"][PEER]["trace"]["activityHash"] == trace["activityHash"]
    for state in ("idle", "working"):
        script.scan = {"peers": [peer(state)]}
        clock.value += 15
        await observer.tick()
    assert [v["state"] for v in observer.hub.state["peers"][PEER]["trace"]["timeline"]] == [
        "idle",
        "working",
    ]


async def test_receipt_completion_during_slow_reads_prevents_undelivered_alert() -> None:
    script, clock = Script(), Clock()
    script.rows = [receipt("pending", 101)]
    script.scan = {"peers": [peer("done")]}
    sources = script.sources()
    completed = False

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if completed:
            script.rows = [receipt("completed", clock.value)]
        return {}

    sources.resources = resources
    observer = Collector(EventHub(), sources, clock=clock)
    await observer.tick()
    completed = True
    clock.value += 15
    await observer.tick()
    assert not anomalies(observer.hub)
