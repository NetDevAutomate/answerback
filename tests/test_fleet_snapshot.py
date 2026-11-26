"""fleet: what the writer remembers of fleet_snapshot, and the counts it takes from it."""

from typing import Any

from test_fleet_fakes import FakeClock, Fleet, Service, linux, writer

from answerback_bridge.fleet import loop, service


def _up() -> Service:
    s = Service()
    s.up = True
    return s


def _writer(s: Service, **settings: Any) -> tuple[loop.Writer, FakeClock, Any]:
    return writer(Fleet({"local": linux(), "worker-a": linux()}), s, **settings)


async def _cycle(w: loop.Writer, clock: FakeClock) -> None:
    await w.once()
    clock.move(15)


async def test_a_fleet_snapshot_that_times_out_is_asked_again_and_its_answer_used() -> None:
    s = _up()
    s.snapshot = "slow"
    w, clock, sink = _writer(s, timings=loop.Timings(service=0.1))
    await _cycle(w, clock)
    assert sink.docs[-1]["receipts"]["source"] == "status+task_list"
    assert sink.docs[-1]["receipts"]["stale"] is False
    s.snapshot = "answer"
    await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 2
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"
    assert w.snapshots.refused is None


async def test_a_refusal_of_fleet_snapshot_is_still_asked_once() -> None:
    s = _up()
    w, clock, _ = _writer(s)
    for _ in range(4):
        await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 1
    assert w.snapshots.refused == service.Refusal("svc")


async def test_an_unusable_fleet_snapshot_is_remembered_like_a_refusal() -> None:
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {"tasks": "unavailable"}
    w, clock, sink = _writer(s)
    for _ in range(3):
        await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 1
    assert sink.docs[-1]["receipts"]["source"] == "status+task_list"


async def test_only_the_current_instance_is_remembered() -> None:
    s = _up()
    s.status["instance_id"] = "svc-a"
    w, clock, _ = _writer(s)
    await _cycle(w, clock)
    await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 1
    s.status["instance_id"] = "svc-b"
    await _cycle(w, clock)
    assert w.snapshots.refused == service.Refusal("svc-b")
    # The refusal of svc-a was forgotten: when it comes back, it is asked once more.
    s.status["instance_id"] = "svc-a"
    await _cycle(w, clock)
    await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 3
    assert w.snapshots.refused == service.Refusal("svc-a")


async def test_counts_of_the_snapshot_win_over_the_counts_of_status() -> None:
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {"tasks": s.tasks, "counts": {"uncertain": 3, "failed": 2}}
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    rc = sink.docs[-1]["receipts"]
    assert rc["source"] == "fleet_snapshot"
    assert rc["counts"]["uncertain"] == 3 and rc["counts"]["failed"] == 2
    assert rc["counts"]["completed"] == 0  # status said 3; the snapshot's counts are used
    assert rc["counts"]["total"] == 5
    assert rc["needs_person_complete"] is False and rc["needs_person_omitted"] == 2


async def test_without_counts_in_the_snapshot_the_counts_of_status_are_used() -> None:
    s = _up()
    s.snapshot = "answer"
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    rc = sink.docs[-1]["receipts"]
    assert rc["source"] == "fleet_snapshot"
    assert rc["counts"]["uncertain"] == 1 and rc["counts"]["completed"] == 3
    assert rc["needs_person_complete"] is True


async def test_unusable_counts_make_the_snapshot_unusable_and_the_instance_refused() -> None:
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {"tasks": s.tasks, "counts": {"uncertain": "three"}}
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    await _cycle(w, clock)
    rc = sink.docs[-1]["receipts"]
    assert rc["source"] == "status+task_list"
    assert rc["counts"]["uncertain"] == 1
    assert s.methods.count("fleet_snapshot") == 1 and s.methods.count("task_list") == 2
    assert w.snapshots.refused == service.Refusal("svc")


# ---------------------------------------------------------------- v4: which failure is remembered


async def test_operation_unknown_is_asked_once_and_not_again_for_the_same_instance() -> None:
    s = _up()  # the fake refuses with PublicBridgeError("operation_unknown")
    w, clock, sink = _writer(s)
    for _ in range(25):  # 375 s: longer than the pause of another error
        await _cycle(w, clock)
    assert s.methods.count("fleet_snapshot") == 1
    assert sink.docs[-1]["receipts"]["source"] == "status+task_list"


async def _at(w: loop.Writer, clock: FakeClock, mono: float) -> None:
    clock.move(mono - clock.now_mono)
    await w.once()


async def test_another_error_answer_pauses_fleet_snapshot_for_300_seconds() -> None:
    s = _up()
    s.snapshot = "fail"
    w, clock, sink = _writer(s)
    await _at(w, clock, 0)
    assert s.methods.count("fleet_snapshot") == 1
    assert sink.docs[-1]["receipts"]["source"] == "status+task_list"
    assert sink.docs[-1]["receipts"]["stale"] is False  # task_list in the cycle of the failure
    s.snapshot = "answer"
    for mono in (15, 150, 299):
        await _at(w, clock, mono)
        assert s.methods.count("fleet_snapshot") == 1, mono
    await _at(w, clock, 300)
    assert s.methods.count("fleet_snapshot") == 2
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"
    assert service.SNAPSHOT_PAUSE_S == 300


async def test_a_new_instance_during_the_pause_is_asked_at_once() -> None:
    s = _up()
    s.snapshot = "fail"
    w, clock, sink = _writer(s)
    await _at(w, clock, 0)
    s.snapshot = "answer"
    s.status["instance_id"] = "svc-new"
    await _at(w, clock, 15)
    assert s.methods.count("fleet_snapshot") == 2
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"


def test_the_memory_takes_the_time_as_an_argument() -> None:
    memory = service.SnapshotMemory()
    memory.pause("svc", now=100.0)
    assert not memory.asks("svc", now=399.9)
    assert memory.asks("svc", now=400.0)
    assert memory.asks("other", now=101.0)
    memory.refuse("svc")
    assert not memory.asks("svc", now=10**9)


# ---------------------------------------------------------------- v5: the complete table


def _no_answers() -> list[tuple[str, BaseException]]:
    from answerback_bridge.transport import TransportUnavailable, reply_error

    wire_timeout = reply_error({"kind": "timeout"}, "fleet_snapshot")
    assert isinstance(wire_timeout, TransportUnavailable)
    return [
        ("transport", TransportUnavailable("Bridge disconnected; delivery may be uncertain.")),
        ("wire timeout", wire_timeout),
        ("connection", ConnectionResetError("reset by peer")),
    ]


async def test_no_answer_of_fleet_snapshot_is_not_remembered_and_asked_again() -> None:
    for what, error in _no_answers():
        s = _up()
        s.snapshot_error = error
        w, clock, sink = _writer(s)
        await _cycle(w, clock)
        assert s.methods == ["status", "fleet_snapshot", "task_list"], what
        assert sink.docs[-1]["receipts"]["source"] == "status+task_list", what
        await _cycle(w, clock)
        assert s.methods[3:5] == ["status", "fleet_snapshot"], what
        assert w.snapshots.refused is None, what


async def test_an_unexpected_error_of_fleet_snapshot_ends_the_service_step() -> None:
    import json

    s = _up()
    w, clock, sink = _writer(s)
    # a good read first: the refusal of a service without the method, then task_list
    await _cycle(w, clock)
    s.snapshot = "answer"
    s.status["instance_id"] = "svc-2"
    s.snapshot_error = KeyError("private-marker-91c")
    good = sink.docs[-1]["receipts"]
    before = len(s.methods)
    await _cycle(w, clock)
    doc = sink.docs[-1]
    assert s.methods[before:] == ["status", "fleet_snapshot"]  # no task_list in this cycle
    rc = doc["receipts"]
    assert rc["stale"] is True and rc["read_at"] == good["read_at"]
    assert rc["error"] == "service answer cannot be used: unexpected KeyError"
    assert [e["text"] for e in doc["errors"]].count(rc["error"]) == 1
    assert "private-marker-91c" not in json.dumps(doc)
    assert w.snapshots.refused is None
    s.snapshot_error = None
    await _cycle(w, clock)
    assert s.methods[-2:] == ["status", "fleet_snapshot"]
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"


async def test_an_unexpected_error_of_the_client_build_names_its_type_only() -> None:
    from answerback_bridge.fleet import service as svc

    def build() -> Any:
        raise KeyError("private-marker-b0d")

    answer = await svc.ask(svc.Remote(build), timeout=1.0)
    assert answer.status_error == "service answer cannot be used: unexpected KeyError"


# ---------------------------------------------------------------- v5: counts null, incomplete


async def test_counts_null_with_usable_tasks_uses_the_counts_of_status_and_asks_again() -> None:
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {"tasks": s.tasks, "counts": None}
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    await _cycle(w, clock)
    rc = sink.docs[-1]["receipts"]
    assert rc["source"] == "fleet_snapshot"
    assert rc["counts"]["completed"] == 3 and rc["counts"]["uncertain"] == 1  # from status
    assert s.methods.count("fleet_snapshot") == 2 and "task_list" not in s.methods
    assert w.snapshots.refused is None


async def test_counts_that_are_not_an_object_are_a_refusal() -> None:
    for counts in ([1, 2], "3", 3, False):
        s = _up()
        s.snapshot = "answer"
        s.snapshot_answer = {"tasks": s.tasks, "counts": counts}
        w, clock, sink = _writer(s)
        await _cycle(w, clock)
        await _cycle(w, clock)
        assert s.methods.count("fleet_snapshot") == 1, counts
        assert sink.docs[-1]["receipts"]["source"] == "status+task_list", counts
        assert w.snapshots.refused == service.Refusal("svc"), counts


async def test_an_answer_that_is_not_complete_is_not_used_and_asked_again() -> None:
    for field in ("recent", "needs_person"):
        s = _up()
        s.snapshot = "answer"
        s.snapshot_answer = {"tasks": s.tasks, "counts": None, field: None}
        w, clock, sink = _writer(s)
        await _cycle(w, clock)
        assert s.methods == ["status", "fleet_snapshot", "task_list"], field
        assert sink.docs[-1]["receipts"]["source"] == "status+task_list", field
        assert w.snapshots.refused is None, field
        await _cycle(w, clock)
        assert s.methods[3:5] == ["status", "fleet_snapshot"], field


async def test_both_lists_there_and_counts_null_is_a_usable_answer() -> None:
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {"tasks": s.tasks, "counts": None, "recent": [], "needs_person": []}
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    assert sink.docs[-1]["receipts"]["source"] == "fleet_snapshot"
    assert "task_list" not in s.methods and w.snapshots.refused is None


async def test_total_is_the_sum_of_the_listed_states_not_the_total_of_the_answer() -> None:
    from answerback_bridge.tasks import STATES

    listed = {state: n + 1 for n, state in enumerate(STATES)}
    s = _up()
    s.snapshot = "answer"
    s.snapshot_answer = {
        "tasks": s.tasks,
        "counts": {**listed, "total": 999, "archived": 7},
    }
    w, clock, sink = _writer(s)
    await _cycle(w, clock)
    rc = sink.docs[-1]["receipts"]
    assert rc["source"] == "fleet_snapshot"
    assert {k: v for k, v in rc["counts"].items() if k != "total"} == listed
    assert rc["counts"]["total"] == sum(listed.values())
    assert "archived" not in rc["counts"]
