"""fleet: the writer's cycles with a fake clock, a fake runner and a fake service."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from test_fleet_fakes import FakeClock, Fleet, Service, Sink, darwin, linux, writer

from answerback_bridge.fleet import loop, probes

WORKERS = ("worker-a", "worker-b", "worker-c")


def _fleet(**outputs: str) -> Fleet:
    base = {"local": linux(), **{n: linux() for n in WORKERS}}
    base.update(outputs)
    return Fleet(base)


async def _serve(w: loop.Writer, clock: FakeClock, until: float) -> list[float]:
    """Run serve until the fake clock passes ``until``; return the cycle start times."""
    starts: list[float] = []
    real = w.cycle

    async def cycle() -> None:
        starts.append(clock.now_mono)
        await real()

    w.cycle = cycle  # type: ignore[method-assign]
    stop = asyncio.Event()
    clock.stop, clock.until = stop, until
    await w.serve(stop)
    return starts


# ---------------------------------------------------------------- 4. cadence


@pytest.mark.parametrize(
    ("coordinator", "gap"),
    [
        (darwin(), 15),
        (darwin(compressor=300_000), 30),
        (darwin(compressor=450_000), 45),
    ],
)
async def test_cadence_with_fake_clock_15s_30s_memory_busy_45s_memory_full(
    coordinator: str, gap: float
) -> None:
    w, clock, _ = writer(_fleet(local=coordinator))
    starts = await _serve(w, clock, 3 * gap + 1)
    assert starts[:4] == [0, gap, 2 * gap, 3 * gap]


async def test_cadence_council_case_45s_cycles_with_worker_probes() -> None:
    # Swap 900 of 1024 MB in use: on macOS swap is reported but never a stop.
    council = darwin(pressure=2, compressor=520_000, swap_mb=900)
    fleet = _fleet(local=council)
    w, clock, _ = writer(fleet)
    starts = await _serve(w, clock, 91)
    assert starts[:3] == [0, 45, 90]
    assert fleet.probed().count("worker-a") == 3
    assert w.cadence.mode == "memory_full" and w.cadence.pause is None


async def test_pressure_level_4_on_macos_starts_no_worker_probe_but_publishes() -> None:
    fleet = _fleet(local=darwin(pressure=4))
    w, clock, sink = writer(fleet)
    service = w.call
    assert await w.once()
    assert fleet.probed() == []
    doc = sink.docs[-1]
    assert doc["capacity"]["paused"]["reason"] == "pressure_critical"
    assert doc["capacity"]["paused"]["since"] == clock.now_wall
    assert doc["machines"][0]["reading"]["taken_at"] is not None
    assert [m["reading"]["error"] for m in doc["machines"][1:]] == ["probes paused"] * 3
    assert "status" in service.methods  # type: ignore[attr-defined]
    # Probing starts again at the next cycle once the emergency ends.
    fleet.outputs["local"] = darwin(pressure=2)
    clock.move(45)
    await w.once()
    assert fleet.probed() == list(WORKERS)
    assert sink.docs[-1]["capacity"]["paused"] is None


async def test_swap_at_95_percent_on_linux_starts_no_worker_probe() -> None:
    fleet = _fleet(local=linux(swapfree=50))
    w, _, sink = writer(fleet)
    await w.once()
    assert fleet.probed() == []
    assert sink.docs[-1]["capacity"]["paused"]["reason"] == "swap_exhausted"
    assert sink.docs[-1]["writer"]["mode"] == "paused"


async def test_high_cpu_load_alone_never_pauses() -> None:
    fleet = _fleet(local=linux(load=400))
    w, _, sink = writer(fleet)
    await w.once()
    assert fleet.probed() == list(WORKERS)
    assert sink.docs[-1]["capacity"]["paused"] is None
    assert sink.docs[-1]["machines"][0]["reading"]["state"] == "full"


# ---------------------------------------------------------------- 5. writes


async def test_least_gap_between_two_writes_is_5s() -> None:
    w, clock, sink = writer(_fleet())
    await w.once()
    w.error("test", "a change")
    clock.move(4.9)
    assert not w.offer() and w.pending
    clock.move(0.1)
    assert w.offer()
    assert sink.at == [0, 5.0]


async def test_unchanged_content_is_not_written_before_the_heartbeat_at_30s() -> None:
    w, clock, sink = writer(_fleet())
    await w.once()
    clock.move(29.9)
    assert not w.offer() and not w.pending
    clock.move(0.1)
    assert w.offer()
    assert sink.at == [0, 30.0]


async def test_a_heartbeat_does_not_change_any_taken_at() -> None:
    w, clock, sink = writer(_fleet(local=darwin(compressor=450_000)))  # 45 s cycles
    await _serve(w, clock, 44)
    assert sink.at[:2] == [0, 30]
    first, beat = sink.docs[0], sink.docs[1]
    assert beat["written_at"] == first["written_at"] + 30
    assert beat["capacity"]["taken_at"] == first["capacity"]["taken_at"]
    assert [m["reading"]["taken_at"] for m in beat["machines"]] == [
        m["reading"]["taken_at"] for m in first["machines"]
    ]


# ---------------------------------------------------------------- 6. expiry


async def test_reading_older_than_60s_is_unknown_with_was_and_no_advice_from_it() -> None:
    fleet = _fleet()
    w, clock, sink = writer(fleet)
    await w.once()
    first = sink.docs[-1]
    assert first["capacity"]["new_work"]["machine"] in WORKERS
    fleet.fail.update(WORKERS)
    for _ in range(4):
        clock.move(15.1)
        await w.once()
    doc = sink.docs[-1]
    for m in doc["machines"][1:]:
        assert (m["reading"]["state"], m["reading"]["was"]) == ("unknown", "ok")
        assert m["reading"]["taken_at"] == first["machines"][1]["reading"]["taken_at"]
    assert doc["capacity"]["new_work"]["machine"] is None
    assert doc["capacity"]["ranked_for_heavy_work"] == []


async def test_heartbeat_rewrite_expires_old_readings_itself() -> None:
    w, clock, sink = writer(_fleet(local=darwin(compressor=450_000)))
    await w.once()
    w.cadence = w.cadence  # nothing new is measured from here
    clock.move(61)
    w.offer()
    assert all(m["reading"]["state"] == "unknown" for m in sink.docs[-1]["machines"])


# ---------------------------------------------------------------- 7. deadlines


async def test_a_worker_that_never_answers_is_ended_and_the_others_are_published() -> None:
    fleet = _fleet()
    fleet.hang.add("worker-b")
    w, _, sink = writer(fleet, timings=loop.Timings(worker=0.2, cycle=2.0, reserve=0.1))
    begun = time.monotonic()
    await w.once()
    assert time.monotonic() - begun < 1.5
    assert fleet.cancelled == ["worker-b"]
    states = {m["name"]: m["reading"] for m in sink.docs[-1]["machines"]}
    assert states["worker-a"]["state"] == "ok" and states["worker-c"]["state"] == "ok"
    assert states["worker-b"]["state"] == "unknown"
    assert states["worker-b"]["error"] == "probe timed out"


async def test_the_cycle_ends_at_its_deadline_and_publishes_what_is_ready() -> None:
    fleet = _fleet()
    fleet.hang.update(WORKERS)
    w, _, sink = writer(fleet, timings=loop.Timings(worker=30, cycle=0.3, reserve=0.05))
    begun = time.monotonic()
    await w.once()
    assert time.monotonic() - begun < 1.5
    assert sorted(fleet.cancelled) == sorted(WORKERS)
    doc = sink.docs[-1]
    assert doc["machines"][0]["reading"]["state"] == "ok"
    assert {m["reading"]["error"] for m in doc["machines"][1:]} == {"probe timed out"}


def test_default_deadlines_are_3_5_8_2_and_12_seconds_with_2_kept_for_ending() -> None:
    t = loop.Timings()
    assert (t.hc_ls, t.coordinator, t.worker, t.service, t.cycle, t.reserve) == (
        3,
        5,
        8,
        2,
        12,
        2,
    )


async def test_a_real_child_past_its_deadline_is_ended_and_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    argv = ["/bin/sh", "-c", f'echo $$ > "{pid_file}"; exec sleep 30']
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await probes.run_bounded(argv, env=dict(os.environ))
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_child_output_is_bounded() -> None:
    status, output = await probes.run_bounded(
        ["/bin/sh", "-c", "yes x"], env=dict(os.environ), limit=1000
    )
    assert (status, output) == (1, b"")


async def test_a_tick_during_a_cycle_is_skipped_and_not_caught_up() -> None:
    fleet = _fleet()
    w, clock, _ = writer(fleet)
    slow = {"done": False}

    def on_call(argv: list[str]) -> None:
        # The first cycle takes 20 s of the fake clock: the tick at 15 s falls inside it.
        if argv[0] == "/bin/sh" and not slow["done"]:
            slow["done"] = True
            clock.move(20)

    fleet.on_call = on_call
    starts = await _serve(w, clock, 31)
    assert starts[:2] == [0, 30]
    assert w.skipped == 1


# ---------------------------------------------------------------- 8, 9. one machine at a time


async def test_a_probe_answer_that_cannot_be_understood_makes_only_that_machine_unknown() -> None:
    fleet = _fleet(**{"worker-b": "os=Linux\n\x1b[2Jgarbage"})
    w, _, sink = writer(fleet)
    await w.once()
    states = {m["name"]: m["reading"] for m in sink.docs[-1]["machines"]}
    assert states["worker-b"]["state"] == "unknown"
    assert states["worker-b"]["error"] == "probe answer not understood"
    assert states["worker-a"]["state"] == "ok" and states["worker-c"]["state"] == "ok"


async def test_a_machine_whose_link_is_not_connected_gets_no_probe_and_keeps_its_link_word() -> (
    None
):
    fleet = _fleet()
    fleet.links["worker-c"] = "unreachable"
    w, _, sink = writer(fleet)
    await w.once()
    assert "worker-c" not in fleet.probed()
    c = sink.docs[-1]["machines"][3]
    assert c["link"]["state"] == "unreachable" and c["reading"]["error"] == "no link"


async def test_hc_ls_failure_leaves_links_unknown_and_starts_no_probe() -> None:
    fleet = _fleet()
    fleet.ls_fails = True
    w, _, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    assert fleet.probed() == []
    doc = sink.docs[-1]
    assert doc["machines"][1]["link"]["state"] == "unknown"
    assert any(e["where"] == "hc" for e in doc["errors"])


async def test_machine_option_limits_and_orders_workers_as_hc_lists_them() -> None:
    fleet = _fleet()
    w, _, sink = writer(fleet, machines=["worker-c", "worker-a", "worker-z"])
    await w.once()
    assert sink.docs[-1]["capacity"]["node_order"] == ["worker-a", "worker-c", "worker-z"]
    assert sorted(fleet.probed()) == ["worker-a", "worker-c"]


async def test_a_bad_machine_name_from_hc_is_refused() -> None:
    fleet = _fleet()
    fleet.links["../evil;rm"] = "connected"
    fleet.links["local"] = "connected"
    w, _, sink = writer(fleet)
    await w.once()
    names = [m["name"] for m in sink.docs[-1]["machines"]]
    assert names == [None, *WORKERS]
    assert any("refused" in (e["text"] or "") for e in sink.docs[-1]["errors"])


async def test_launch_on_a_worker_keeps_it_out_of_advice_for_60s() -> None:
    fleet = _fleet()
    w, clock, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    assert sink.docs[-1]["capacity"]["new_work"]["machine"] == "worker-a"
    fleet.terminals.append({"address": "worker-a/t9", "state": "live", "status": "working"})
    clock.move(15)
    await w.once()
    doc = sink.docs[-1]
    assert doc["capacity"]["new_work"]["machine"] is None
    assert doc["capacity"]["recent_launches"] == [
        {"machine": "worker-a", "seen_at": clock.now_wall, "count": 1}
    ]
    assert doc["machines"][1]["reading"]["agents"]["active"] == 1
    clock.move(61)
    await w.once()
    assert sink.docs[-1]["capacity"]["new_work"]["machine"] == "worker-a"


# ---------------------------------------------------------------- 11. service


async def test_service_down_keeps_last_good_receipts_stale_with_old_read_at() -> None:
    service = Service()
    w, clock, sink = writer(_fleet(), service)
    await w.once()
    first = sink.docs[-1]
    assert first["health"]["service"]["state"] == "unreachable"
    assert first["receipts"]["counts"] is None
    assert first["receipts"]["needs_person_complete"] is False
    assert first["machines"][1]["reading"]["state"] == "ok"  # capacity is still published
    service.up = True
    clock.move(15)
    await w.once()
    good = sink.docs[-1]["receipts"]
    assert good["stale"] is False and good["counts"]["uncertain"] == 1
    assert good["source"] == "status+task_list" and len(good["needs_person"]) == 1
    assert good["needs_person_complete"] is True
    assert service.methods[-3:] == ["status", "fleet_snapshot", "task_list"]
    service.up = False
    clock.move(15)
    await w.once()
    doc = sink.docs[-1]
    assert doc["health"]["service"]["state"] == "unreachable"
    stale = doc["receipts"]
    assert stale["stale"] is True and stale["error"]
    assert stale["read_at"] == good["read_at"] and stale["needs_person"] == good["needs_person"]


async def test_fleet_snapshot_is_used_when_the_service_knows_it() -> None:
    service = Service()
    service.up = True
    original = service.__call__

    async def call(method: str, params: dict) -> dict:
        if method == "fleet_snapshot":
            return {"tasks": service.tasks}
        return await original(method, params)

    w, _, sink = writer(_fleet(), call)  # type: ignore[arg-type]
    await w.once()
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"


# ---------------------------------------------------------------- 3, 14. failures and clock jumps


async def test_failed_publication_keeps_going_and_the_next_document_carries_an_error() -> None:
    w, clock, sink = writer(_fleet())
    sink.fail = True
    assert not await w.once()
    sink.fail = False
    clock.move(15)
    assert await w.once()
    assert any(e["where"] == "publish" for e in sink.docs[-1]["errors"])


async def test_a_clock_jump_of_more_than_5s_expires_every_reading_and_adds_one_error() -> None:
    fleet = _fleet(local=darwin())
    w, clock, sink = writer(fleet)
    await w.once()
    first = sink.docs[-1]
    assert all(m["reading"]["state"] == "ok" for m in first["machines"][1:])
    # Probes pause in the next cycle, so the workers' good readings are not refreshed:
    # only the clock jump can expire them.
    fleet.outputs["local"] = darwin(pressure=4)
    fleet.calls.clear()
    clock.move(15, wall=115)  # the wall clock went 100 s further than the monotonic one
    await w.once()
    assert fleet.probed() == []
    doc = sink.docs[-1]
    jumps = [e for e in doc["errors"] if e["where"] == "clock"]
    assert len(jumps) == 1 and "100.0 s" in jumps[0]["text"]
    for before, after in zip(first["machines"][1:], doc["machines"][1:], strict=True):
        assert (after["reading"]["state"], after["reading"]["was"]) == ("unknown", "ok")
        assert after["reading"]["taken_at"] == before["reading"]["taken_at"]
        assert after["reading"]["error"] == "reading expired"
    assert doc["machines"][0]["reading"]["state"] == "full"  # measured after the jump


async def test_sigterm_during_a_cycle_ends_its_children_and_returns() -> None:
    fleet = _fleet()
    fleet.hang.add("worker-a")
    w, _, _ = writer(fleet)
    stop = asyncio.Event()
    task = asyncio.create_task(w.serve(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert fleet.cancelled == ["worker-a"]


# ---------------------------------------------------------------- 17. the reader's sample


async def test_reader_sample_gives_new_work_on_first_ok_worker_and_new_suite_null() -> None:
    # Four workers; the coordinator full on CPU; one worker busy on memory at 32% compressor;
    # three ok workers that each run a suite.
    fleet = Fleet(
        {
            "local": darwin(load=18.56, cores=16, compressor=190_000),
            "worker-a": darwin(load=2.75, cores=16, suites=1),
            "worker-b": darwin(load=2.8, cores=14, compressor=320_000),
            "worker-c": linux(load=1.64, suites=1),
            "worker-d": linux(load=5.4, suites=1),
        }
    )
    w, _, sink = writer(fleet)
    await w.once()
    doc = sink.docs[-1]
    coordinator = doc["machines"][0]
    assert coordinator["reading"]["state"] == "full"
    assert coordinator["reading"]["pressure"]["signal"] == "cpu"
    busy = doc["machines"][2]
    assert busy["reading"]["state"] == "busy"
    assert busy["reading"]["pressure"] == {"signal": "memory", "fraction": 0.84, "text": "32%"}
    cap = doc["capacity"]
    assert cap["ranked_for_heavy_work"] == ["worker-a", "worker-c", "worker-d"]
    assert cap["new_work"]["machine"] == "worker-a"
    assert cap["new_suite"]["machine"] is None and cap["new_suite"]["why"]


# ---------------------------------------------------------------- 13. hostile texts


async def test_hostile_texts_from_hc_probe_and_service_are_cut_and_cleaned() -> None:
    hostile = "\x1b]0;owned\x07\u202e" + "\u4e00" * 5000
    fleet = _fleet(
        **{"worker-a": linux().replace("largest_name=python", "largest_name=" + hostile)}
    )
    fleet.links["worker-b"] = hostile
    fleet.terminals = [{"address": hostile, "state": "live", "status": "working"}] * 50
    service = Service()
    service.up = True
    service.status["version"] = hostile
    service.status["bus"] = {"connected": True, "last_error": hostile}
    service.status["clients"] = {"clients": [{"client": hostile}] * 100}
    service.tasks = [
        {
            "id": f"id-{n}",
            "status": "blocked",
            "target": hostile,
            "request_key": hostile,
            "title": hostile,
            "created": float(n),
        }
        for n in range(100)
    ]
    w, _, sink = writer(fleet, service)
    await w.once()
    doc = sink.docs[-1]
    text = json.dumps(doc, ensure_ascii=False)
    assert len(text.encode()) <= 256 * 1024
    assert not any(ord(c) < 32 or c == "\u202e" for c in text)
    b = doc["machines"][2]
    assert len(b["link"]["state"]) <= 64 and b["reading"]["error"] == "no link"
    assert len(doc["health"]["service"]["version"]) <= 64
    assert len(doc["health"]["clients"]) == 32
    for r in doc["receipts"]["needs_person"] + doc["receipts"]["recent"]:
        assert len(r["title"]) <= 120 and len(r["recipient"]["text"]) <= 200
    kept = len(doc["receipts"]["needs_person"])
    assert doc["receipts"]["needs_person_omitted"] == 100 - kept


# ---------------------------------------------------------------- S1. no loop without a sleep


class FlakySink(Sink):
    """Publishes once, then fails until ``healthy`` is set; records each attempt's time."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.attempts: list[float] = []
        self.healthy = False

    def publish(self, data: bytes) -> None:
        from answerback_bridge.fleet.publish import PublishError

        self.attempts.append(self.clock.now_mono if self.clock else 0.0)
        if len(self.attempts) > 200:
            raise SystemExit("the writer publishes without a pause")
        if len(self.attempts) > 1 and not self.healthy:
            raise PublishError("publication failed: test")
        super().publish(data)


async def test_failed_publications_are_attempted_at_least_5s_apart_and_never_spin() -> None:
    clock = FakeClock()
    sink = FlakySink(clock)
    w, clock, sink = writer(_fleet(), clock=clock, sink=sink)
    await _serve(w, clock, 60)
    attempts = sink.attempts
    assert len(attempts) >= 3
    assert len(set(attempts)) == len(attempts)
    gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
    assert all(gap >= 5.0 for gap in gaps), gaps
    assert all(seconds > 0 for seconds in clock.sleeps)


async def test_after_a_failed_publication_the_next_attempt_is_5s_later_or_at_the_tick() -> None:
    clock = FakeClock()
    sink = FlakySink(clock)
    w, clock, sink = writer(_fleet(), clock=clock, sink=sink)
    await _serve(w, clock, 31)
    # 0 published; 15 (tick) failed; 20 and 25 retries; 30 the next tick.
    assert sink.attempts[:5] == [0, 15, 20, 25, 30]


async def test_the_next_document_after_the_sink_works_again_carries_the_error() -> None:
    clock = FakeClock()
    sink = FlakySink(clock)
    w, clock, sink = writer(_fleet(), clock=clock, sink=sink)
    await _serve(w, clock, 21)
    sink.healthy = True
    await _serve(w, clock, 40)
    assert len(sink.docs) >= 2
    assert any(e["where"] == "publish" for e in sink.docs[1]["errors"])


async def test_the_serve_loop_never_runs_its_inner_loop_twice_without_a_sleep() -> None:
    clock = FakeClock()
    sink = FlakySink(clock)
    w, clock, sink = writer(_fleet(), clock=clock, sink=sink)
    await _serve(w, clock, 120)
    assert w.steps > 0
    assert w.steps <= len(clock.sleeps), (w.steps, len(clock.sleeps))


# ---------------------------------------------------------------- S2. fleet_snapshot once


async def _cycles(w: loop.Writer, clock: FakeClock, n: int) -> None:
    for _ in range(n):
        await w.once()
        clock.move(15)


async def test_fleet_snapshot_is_asked_once_while_the_service_refuses_it() -> None:
    service = Service()
    service.up = True
    w, clock, sink = writer(_fleet(), service)
    await _cycles(w, clock, 5)
    assert service.methods.count("fleet_snapshot") == 1
    assert service.methods.count("task_list") == 5
    assert sink.docs[-1]["receipts"]["source"] == "status+task_list"


async def test_a_new_service_instance_is_asked_for_fleet_snapshot_once_more() -> None:
    service = Service()
    service.up = True
    w, clock, _ = writer(_fleet(), service)
    await _cycles(w, clock, 2)
    service.status["instance_id"] = "svc-2"
    await _cycles(w, clock, 3)
    assert service.methods.count("fleet_snapshot") == 2


async def test_a_status_without_instance_id_is_asked_for_fleet_snapshot_once() -> None:
    service = Service()
    service.up = True
    del service.status["instance_id"]
    w, clock, _ = writer(_fleet(), service)
    await _cycles(w, clock, 4)
    assert service.methods.count("fleet_snapshot") == 1


async def test_a_service_that_knows_fleet_snapshot_is_asked_every_cycle() -> None:
    service = Service()
    service.up = True
    original = service.__call__

    async def call(method: str, params: dict) -> dict:
        if method == "fleet_snapshot":
            service.methods.append(method)
            return {"tasks": service.tasks}
        return await original(method, params)

    w, clock, sink = writer(_fleet(), call)  # type: ignore[arg-type]
    await _cycles(w, clock, 3)
    assert service.methods.count("fleet_snapshot") == 3
    assert "task_list" not in service.methods
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"
    assert sink.docs[-1]["receipts"]["needs_person"][0]["id"] == "r-1"


# ---------------------------------------------------------------- S3. agents and lanes


def _live(address: str, status: str | None, agent: str) -> dict:
    row = {"address": address, "state": "live", "agent": agent, "turn": 0}
    if status is not None:
        row["status"] = status
    return row


LANE_MIX = [
    _live("worker-a/t1", "working", "claude-code"),
    _live("worker-a/t2", "blocked", "claude-code"),
    _live("worker-a/t3", "idle", "codex"),
    _live("worker-a/t4", "done", "kiro-cli"),
    _live("worker-a/t5", None, "zsh"),
    _live("worker-a/t6", None, "node"),
    _live("worker-a/t7", None, "sh"),
    _live("worker-a/t8", None, "sh"),
]


async def test_agents_count_working_programs_and_labelled_lanes_through_the_listings() -> None:
    fleet = _fleet()
    fleet.terminals = list(LANE_MIX)
    fleet.labelled = [LANE_MIX[6], LANE_MIX[7]]
    w, _, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    a = sink.docs[-1]["machines"][1]["reading"]["agents"]
    assert a == {"active": 4, "ceiling": None, "lanes": 2}


async def test_a_labelled_idle_terminal_is_not_active_and_not_a_lane() -> None:
    fleet = _fleet()
    idle = _live("worker-a/t3", "idle", "sh")
    fleet.terminals = [idle]
    fleet.labelled = [idle]
    w, _, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    assert sink.docs[-1]["machines"][1]["reading"]["agents"]["active"] == 0
    assert sink.docs[-1]["machines"][1]["reading"]["agents"]["lanes"] == 0


async def test_an_address_in_both_listings_counts_once_in_the_document() -> None:
    fleet = _fleet()
    lane = _live("worker-a/t7", None, "sh")
    fleet.terminals = [lane]
    fleet.labelled = [lane]
    w, _, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    assert sink.docs[-1]["machines"][1]["reading"]["agents"]["active"] == 1


async def test_the_coordinators_agents_follow_the_same_rule() -> None:
    fleet = _fleet()
    fleet.terminals = [
        _live("t1", "working", "claude-code"),
        _live("t2", "blocked", "claude-code"),
        _live("t3", None, "zsh"),
        _live("t4", None, "sh"),
    ]
    fleet.labelled = [_live("t4", None, "sh")]
    w, _, sink = writer(fleet)
    await w.once()
    assert sink.docs[-1]["machines"][0]["reading"]["agents"] == {
        "active": 2,
        "ceiling": None,
        "lanes": 1,
    }


@pytest.mark.parametrize("failure", ["fails", "hangs"])
async def test_a_failed_labelled_listing_gives_null_lanes_and_one_error_not_repeated(
    failure: str,
) -> None:
    fleet = _fleet()
    fleet.terminals = list(LANE_MIX)
    fleet.labelled = [LANE_MIX[6], LANE_MIX[7]]
    setattr(fleet, f"label_{failure}", True)
    w, clock, sink = writer(fleet, machines=["worker-a"], timings=loop.Timings(hc_ls=0.2))
    await w.once()
    clock.move(15)
    await w.once()  # the service is down: its error entry comes between the two cycles
    doc = sink.docs[-1]
    assert doc["machines"][1]["reading"]["agents"] == {"active": 2, "ceiling": None, "lanes": None}
    assert len([e for e in doc["errors"] if e["where"] == "hc-label"]) == 1
    assert doc["machines"][1]["reading"]["state"] == "ok"


async def test_the_lane_label_reaches_hc_as_one_argument() -> None:
    fleet = _fleet()
    w, _, _ = writer(fleet, lane_label="team.lane=x-1")
    await w.once()
    labelled = [argv for argv in fleet.calls if "--label" in argv]
    assert labelled == [["hc", "ls", "--json", "--label", "team.lane=x-1"]]


async def test_the_agents_signal_with_a_ceiling_uses_the_same_count() -> None:
    fleet = _fleet()
    fleet.terminals = [_live("worker-a/t7", None, "sh"), _live("worker-a/t8", None, "sh")]
    fleet.labelled = list(fleet.terminals)
    w, _, sink = writer(fleet, machines=["worker-a"], ceilings={"worker-a": 2})
    await w.once()
    r = sink.docs[-1]["machines"][1]["reading"]
    assert r["state"] == "full" and r["pressure"]["signal"] == "agents"
    assert sink.docs[-1]["capacity"]["new_work"]["machine"] is None


async def test_harness_processes_come_from_the_probe() -> None:
    fleet = _fleet(**{"worker-a": linux().replace("procs=12\n", "procs=12\nharness_procs=3\n")})
    w, _, sink = writer(fleet, machines=["worker-a"])
    await w.once()
    assert sink.docs[-1]["machines"][1]["reading"]["harness_processes"] == 3
    assert sink.docs[-1]["machines"][0]["reading"]["harness_processes"] is None


# ---------------------------------------------------------------- S4. unmeasured


@pytest.mark.parametrize(
    "failure",
    ["timeout", "not understood", "incomplete"],
)
async def test_a_coordinator_that_cannot_be_measured_gives_mode_unmeasured(failure: str) -> None:
    fleet = _fleet()
    if failure == "timeout":
        fleet.hang.add("local")
    elif failure == "not understood":
        fleet.outputs["local"] = "garbage"
    else:
        fleet.outputs["local"] = linux().replace("os=Linux", "os=Plan9")
    service = Service()
    w, _, sink = writer(fleet, service, timings=loop.Timings(coordinator=0.1))
    assert await w.once()
    doc = sink.docs[-1]
    assert (doc["writer"]["mode"], doc["writer"]["interval_s"]) == ("unmeasured", 45)
    assert doc["capacity"]["paused"] is None
    assert fleet.probed() == list(WORKERS)
    assert "status" in service.methods
    assert doc["machines"][0]["reading"]["state"] == "unknown"


async def test_unmeasured_only_after_the_last_good_coordinator_reading_expires() -> None:
    fleet = _fleet(local=darwin(compressor=450_000))
    w, clock, sink = writer(fleet)
    await w.once()
    assert w.cadence.mode == "memory_full"
    fleet.outputs["local"] = "garbage"
    clock.move(45)
    await w.once()
    assert sink.docs[-1]["writer"]["mode"] == "memory_full"  # 45 s old: still current
    clock.move(45)
    await w.once()
    assert sink.docs[-1]["writer"]["mode"] == "unmeasured"
    assert sink.docs[-1]["writer"]["interval_s"] == 45
    fleet.outputs["local"] = darwin()
    clock.move(45)
    await w.once()
    assert sink.docs[-1]["writer"]["mode"] == "normal"


# ---------------------------------------------------------------- S6, S7


async def test_a_listing_that_does_not_answer_says_in_time_and_no_fixed_seconds() -> None:
    fleet = _fleet()

    async def hang(argv: list[str]) -> tuple[int, str]:
        if argv[1:] == ["ls", "--json"]:
            await asyncio.Event().wait()
        return await Fleet.__call__(fleet, argv)

    w, _, sink = writer(fleet, timings=loop.Timings(hc_ls=0.1))
    w.runner = hang
    await w.once()
    texts = [e["text"] for e in sink.docs[-1]["errors"] if e["where"] == "hc"]
    assert texts == ["hc ls --json did not answer in time"]


async def test_a_clock_jump_before_a_rewrite_between_cycles_expires_every_reading() -> None:
    w, clock, sink = writer(_fleet())
    await w.once()
    assert all(m["reading"]["state"] == "ok" for m in sink.docs[-1]["machines"])
    clock.move(20, wall=600)  # the machine slept: wall 600 s, monotonic 20 s
    assert w.offer()
    doc = sink.docs[-1]
    for m in doc["machines"]:
        assert (m["reading"]["state"], m["reading"]["was"]) == ("unknown", "ok")
        assert m["reading"]["taken_at"] == sink.docs[0]["machines"][0]["reading"]["taken_at"]
    assert len([e for e in doc["errors"] if e["where"] == "clock"]) == 1
    clock.move(10)
    w.offer()
    assert len([e for e in w.errors if e["where"] == "clock"]) == 1


# ---------------------------------------------------------------- F2. unusable service answers


def _bad_counts(service: Service) -> None:
    service.status["tasks"] = {"uncertain": "unavailable"}


def _no_counts(service: Service) -> None:
    del service.status["tasks"]


def _null_receipt(service: Service) -> None:
    service.tasks = [None]


def _receipt_without_id(service: Service) -> None:
    service.tasks = [{"status": "uncertain", "target": "hc:t8"}]


@pytest.mark.parametrize("spoil", [_bad_counts, _no_counts, _null_receipt, _receipt_without_id])
async def test_an_unusable_service_answer_keeps_the_last_good_receipts_stale(spoil: Any) -> None:
    service = Service()
    service.up = True
    w, clock, sink = writer(_fleet(), service)
    await w.once()
    good = sink.docs[-1]["receipts"]
    assert good["stale"] is False and good["needs_person_complete"] is True
    spoil(service)
    clock.move(15)
    await w.once()
    doc = sink.docs[-1]
    rc = doc["receipts"]
    assert rc["stale"] is True and rc["error"]
    assert rc["read_at"] == good["read_at"]
    assert rc["counts"] == good["counts"] and rc["needs_person"] == good["needs_person"]
    assert rc["needs_person_complete"] is False
    assert doc["machines"][1]["reading"]["state"] == "ok"


async def test_an_unusable_answer_before_any_good_read_never_claims_nothing_waits() -> None:
    service = Service()
    service.up = True
    _bad_counts(service)
    w, _, sink = writer(_fleet(), service)
    await w.once()
    rc = sink.docs[-1]["receipts"]
    assert rc["counts"] is None and rc["needs_person_complete"] is False and rc["stale"]
    assert sink.docs[-1]["health"]["service"]["state"] == "unreachable"


async def test_a_very_large_integer_from_the_service_is_null_and_capacity_is_published() -> None:
    service = Service()
    service.up = True
    service.status["started"] = 10**400
    service.tasks[0]["created"] = 10**400
    service.tasks[0]["updated"] = 10**400
    w, _, sink = writer(_fleet(), service)
    assert await w.once()
    doc = sink.docs[-1]
    assert doc["health"]["service"]["started"] is None
    assert doc["receipts"]["needs_person"][0]["created"] is None
    assert doc["receipts"]["needs_person"][0]["updated"] is None
    assert doc["capacity"]["new_work"]["machine"] in WORKERS


# ---------------------------------------------------------------- F4. the budget


async def test_no_step_starts_after_second_10_of_the_cycle_and_the_document_is_published() -> None:
    # Scaled: a 0.12 s cycle with 0.02 s kept for ending children, so steps stop at 0.10 s.
    # The coordinator's probe times out at 0.05 s and its child takes 0.1 s to end.
    fleet = _fleet()
    fleet.hang.add("local")
    fleet.slow_end["local"] = 0.1
    service = Service()
    timings = loop.Timings(coordinator=0.05, cycle=0.12, reserve=0.02)
    w, _, sink = writer(fleet, service, timings=timings)
    assert await w.once()
    assert [argv for argv in fleet.calls if argv[0] != "/bin/sh"] == []
    assert service.methods == []
    doc = sink.docs[-1]
    budget = [e for e in doc["errors"] if e["text"] == "no time left in the cycle"]
    assert len(budget) == 1
    # Steps that were not started count as failed.
    assert all(m["link"]["state"] == "unknown" for m in doc["machines"][1:])
    assert all(m["reading"]["state"] == "unknown" for m in doc["machines"][1:])
    assert doc["health"]["service"]["state"] == "unreachable"
    assert doc["health"]["error"] == "no time left in the cycle"


async def test_workers_and_service_are_not_started_when_the_listing_used_the_budget() -> None:
    fleet = _fleet()

    async def slow(argv: list[str]) -> tuple[int, str]:
        if argv[1:2] == ["ls"]:
            await asyncio.sleep(0.15)
        return await Fleet.__call__(fleet, argv)

    service = Service()
    w, _, sink = writer(fleet, service, timings=loop.Timings(hc_ls=5, cycle=0.12, reserve=0.02))
    w.runner = slow
    await w.once()
    assert fleet.probed() == [] and service.methods == []
    assert [e["text"] for e in sink.docs[-1]["errors"]].count("no time left in the cycle") == 1


# ---------------------------------------------------------------- F5. an unusable listing


@pytest.mark.parametrize(
    "raw",
    [
        '{"machines": [{"name": "worker-a", "state": "connected"}], "terminals": "unavailable"}',
        '{"machines": [{"name": "worker-a", "state": "connected"}]}',
        '{"terminals": []}',
    ],
)
async def test_a_listing_without_machines_or_terminals_is_no_listing(raw: str) -> None:
    fleet = _fleet()
    fleet.terminals = [_live("worker-a/t1", "working", "claude-code")]
    w, clock, sink = writer(fleet, machines=["worker-a"], ceilings={"worker-a": 1})
    await w.once()
    assert sink.docs[-1]["machines"][1]["reading"]["state"] == "full"
    fleet.raw_listing = raw
    fleet.calls.clear()
    clock.move(15)
    await w.once()
    doc = sink.docs[-1]
    assert fleet.probed() == []
    assert doc["capacity"]["new_work"]["machine"] is None
    assert doc["capacity"]["ranked_for_heavy_work"] == []
    assert doc["machines"][1]["link"]["state"] == "unknown"
    assert doc["machines"][1]["reading"]["agents"]["active"] is None
    errors = [e for e in doc["errors"] if e["where"] == "hc"]
    assert len(errors) == 1 and "no" in errors[0]["text"]


# ---------------------------------------------------------------- F9. two deciding signals


async def test_pressure_is_the_deciding_signal_with_the_highest_fraction() -> None:
    # The coordinator is full on CPU (load 12 on 10 cores: 1.20) and on memory
    # (compressor 55%: 1.30). The document names memory, the higher fraction.
    fleet = _fleet(local=darwin(load=12, compressor=550_000))
    w, _, sink = writer(fleet)
    await w.once()
    r = sink.docs[-1]["machines"][0]["reading"]
    assert r["state"] == "full"
    assert sorted(d["signal"] for d in r["decided_by"]) == ["cpu", "memory"]
    assert r["pressure"] == {"signal": "memory", "fraction": 1.3, "text": "55%"}


# ---------------------------------------------------------------- F14. only the receipts fail


async def test_when_only_the_receipts_fail_health_is_what_status_said() -> None:
    service = Service()
    service.up = True
    w, clock, sink = writer(_fleet(), service)
    await w.once()
    good = sink.docs[-1]["receipts"]
    service.tasks = "unavailable"  # type: ignore[assignment]
    clock.move(15)
    await w.once()
    doc = sink.docs[-1]
    assert doc["health"]["service"]["state"] == "running"
    assert doc["health"]["stale"] is False and doc["health"]["read_at"] == clock.now_wall
    rc = doc["receipts"]
    assert rc["stale"] is True and rc["error"] and rc["read_at"] == good["read_at"]
    assert rc["needs_person"] == good["needs_person"]


# ---------------------------------------------------------------- an unexpected error


async def test_an_unexpected_error_in_one_probe_gives_one_error_with_its_type_and_goes_on() -> None:
    fleet = _fleet()
    raised = {"done": False}

    async def runner(argv: list[str]) -> tuple[int, str]:
        if argv[1:4] == ["exec", "--on", "worker-a"] and not raised["done"]:
            raised["done"] = True
            raise KeyError("surprise")
        return await Fleet.__call__(fleet, argv)

    w, clock, sink = writer(fleet)
    w.runner = runner
    starts = await _serve(w, clock, 16)
    assert starts[:2] == [0, 15]
    first = sink.docs[0]
    assert [e for e in first["errors"] if "KeyError" in (e["text"] or "")] != []
    assert first["machines"][1]["reading"]["state"] == "unknown"
    assert first["machines"][2]["reading"]["state"] == "ok"
    later = sink.docs[-1]
    assert len([e for e in later["errors"] if "KeyError" in (e["text"] or "")]) == 1
    assert later["machines"][1]["reading"]["state"] == "ok"


async def test_an_unexpected_error_that_ends_a_cycle_is_recorded_and_the_writer_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, clock, sink = writer(_fleet())
    real = w._take_listing
    calls = {"n": 0}

    def broken(*args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("surprise")
        real(*args)

    monkeypatch.setattr(w, "_take_listing", broken)
    starts = await _serve(w, clock, 16)
    assert starts[:2] == [0, 15]
    errors = [e for e in sink.docs[-1]["errors"] if e["where"] == "cycle"]
    assert len(errors) == 1 and "KeyError" in errors[0]["text"]
    assert sink.docs[-1]["machines"][1]["reading"]["state"] == "ok"


class _Unusual(BaseException):
    """An exception of none of the standard classes: it derives from ``BaseException`` alone."""


class FailsAfterOnce(Sink):
    """Publishes once, then raises the given exception; counts how often it was asked."""

    def __init__(self, clock: FakeClock, error: BaseException) -> None:
        super().__init__(clock)
        self.error = error
        self.asked = 0

    def publish(self, data: bytes) -> None:
        self.asked += 1
        if self.asked > 1:
            raise self.error
        super().publish(data)


def _after_publication(w: loop.Writer) -> tuple[Any, ...]:
    return (w.published, w.last_key, w.last_write, w.pending, len(w.errors))


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt("injected"),
        SystemExit("injected"),
        RuntimeError("injected"),
        _Unusual("injected"),
        asyncio.CancelledError("injected"),
    ],
    ids=["interrupt", "exit", "runtime", "own", "cancelled"],
)
async def test_a_sink_error_that_is_no_publish_error_leaves_offer_as_it_is(
    error: BaseException,
) -> None:
    clock = FakeClock()
    w, clock, sink = writer(_fleet(), clock=clock, sink=FailsAfterOnce(clock, error))
    assert await w.once()
    before = _after_publication(w)
    clock.move(15)
    with pytest.raises(BaseException) as failed:
        w.offer(force=True)
    assert failed.value is error
    assert sink.asked == 2 and len(sink.docs) == 1
    # No error entry; what the first publication set is as it was.
    assert _after_publication(w) == before


async def test_a_sink_error_that_is_no_publish_error_ends_serve_and_is_not_published() -> None:
    words = "words that must not be published"
    error = RuntimeError(words)
    clock = FakeClock()
    w, clock, sink = writer(_fleet(), clock=clock, sink=FailsAfterOnce(clock, error))
    stop = asyncio.Event()
    clock.stop, clock.until = stop, 120.0
    with pytest.raises(RuntimeError) as failed:
        await w.serve(stop)
    assert failed.value is error
    assert sink.asked == 2 and len(sink.docs) == 1  # asked once after its publication, not again
    assert not any(words in str(entry) for entry in w.errors)
    assert not stop.is_set()
