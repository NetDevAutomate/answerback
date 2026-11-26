"""The rebalance planner: pure, at most one move, and the rules that keep work where it is."""

from __future__ import annotations

import ast
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.placement import rebalance
from answerback_bridge.placement.facts import Facts, HarnessFacts, MachineFacts
from answerback_bridge.placement.probe import LOCAL, Reading
from answerback_bridge.placement.rebalance import (
    NOTHING_MOVES,
    ManagedWorker,
    MoveRecord,
    Plan,
    plan_rebalance,
)

NOW = 1_800_000_000.0
SEND = ("mac-a", "mac-b")


def reading(
    machine: str,
    load: float,
    *,
    cores: int = 10,
    at: float = NOW - 5,
    available: float = 0.9,
    error: Any = None,
) -> Reading:
    """A Linux reading with one load per core on all three averages."""
    return Reading(
        machine,
        at,
        os="Linux",
        cores=cores,
        load1=load * cores,
        load5=load * cores,
        load15=load * cores,
        mem_total_kb=1000,
        mem_available_kb=int(available * 1000),
        largest_process_kb=100,
        largest_process_name="python",
        process_count=12,
        error=error,
    )


def two(
    *rows: tuple[Any, ...], gap: float = 120.0
) -> tuple[dict[str, Reading], dict[str, Reading]]:
    """(machine, load now, load before[, reading keywords]) -> readings and previous."""
    readings, previous = {}, {}
    for name, now_load, was_load, *rest in rows:
        extra = rest[0] if rest else {}
        readings[name] = reading(name, now_load, **extra)
        if was_load is not None:
            previous[name] = reading(name, was_load, at=NOW - 5 - gap, **extra)
    return readings, previous


def facts(*names: str, max_total: int | None = None, order: tuple[str, ...] = ()) -> Facts:
    ready = {"claude": HarnessFacts(True), "codex": HarnessFacts(True)}
    return Facts(
        order=order,
        machines={
            name: MachineFacts(name, project="/work/p", max_total=max_total, harnesses=ready)
            for name in names
        },
    )


def worker(address: str, machine: str = LOCAL, work: str = "idle", **changes: Any) -> ManagedWorker:
    base = ManagedWorker(
        id=address,
        machine=machine,
        harness="claude",
        task_class="port-fix",
        lane="rb",
        work=work,  # type: ignore[arg-type]
        receipt="completed" if work == "idle" else "running",
        opened_by_person=False,
        lane_stays=False,
        gate_class=False,
        since=100.0,
        task="T81",
    )
    return replace(base, **changes)


def plan(
    rows: tuple[dict[str, Reading], dict[str, Reading]],
    workers: list[ManagedWorker],
    *,
    known: Facts | None = None,
    send: tuple[str, ...] = SEND,
    moves: tuple[MoveRecord, ...] = (),
    now: float = NOW,
) -> Plan:
    readings, previous = rows
    return plan_rebalance(
        now, readings, previous, known or facts(LOCAL, "mac-a", "mac-b"), workers, send, moves
    )


def role(result: Plan, machine: str) -> str:
    return next(view.role for view in result.machines if view.machine == machine)


FULL_TO_A = (LOCAL, 1.5, 1.5), ("mac-a", 0.1, 0.1)


def test_a_full_coordinator_with_an_idle_worker_and_a_free_machine_gives_one_move() -> None:
    result = plan(two(*FULL_TO_A), [worker("t04")])
    assert result.status == "move" and result.cause is None
    move = result.move
    assert move is not None
    assert (move.worker, move.giver, move.taker, move.kind) == ("t04", LOCAL, "mac-a", "hand_over")
    assert (move.task, move.harness, move.work) == ("T81", "claude", "idle")
    assert move.giver_load == pytest.approx(1.5)
    assert move.projected == pytest.approx(0.2) and move.gap == pytest.approx(1.3)
    assert [item.machine for item in move.giver_readings] == [LOCAL, LOCAL]
    assert move.giver_readings[0].taken_at < move.giver_readings[1].taken_at
    assert [item.machine for item in move.taker_readings] == ["mac-a", "mac-a"]
    assert result.id is not None and result.id.startswith("R")
    assert role(result, LOCAL) == "giver" and role(result, "mac-a") == "taker"
    json.dumps(result.to_json())


def test_kind_follows_the_state_of_the_work() -> None:
    kinds = {
        state: plan(two(*FULL_TO_A), [worker("t04", work=state)]).move.kind  # type: ignore[union-attr]
        for state in ("queued", "idle", "working")
    }
    assert kinds == {"queued": "resubmit_queued", "idle": "hand_over", "working": "at_boundary"}


def test_single_busy_reading_gives_nothing() -> None:
    workers = [worker("t04")]
    once = plan(two((LOCAL, 0.8, 0.2), ("mac-a", 0.1, 0.1)), workers)
    assert once.status == NOTHING_MOVES and once.move is None and once.id is None
    assert "busy on two readings" in (once.cause or "")
    assert role(once, LOCAL) != "giver"
    first = plan(two((LOCAL, 0.8, None), ("mac-a", 0.1, 0.1)), workers)
    assert first.status == NOTHING_MOVES
    twice = plan(two((LOCAL, 0.8, 0.8), ("mac-a", 0.1, 0.1)), workers)
    assert twice.status == "move"


def test_readings_must_be_60_to_600_seconds_apart() -> None:
    workers = [worker("t04")]
    for gap in (59.0, 601.0):
        result = plan(two((LOCAL, 0.8, 0.8), ("mac-a", 0.1, 0.1), gap=gap), workers)
        assert result.status == NOTHING_MOVES
        assert result.cause is not None and result.cause.startswith("one reading only")
    for gap in (60.0, 600.0):
        assert plan(two((LOCAL, 0.8, 0.8), ("mac-a", 0.1, 0.1), gap=gap), workers).status == "move"
    none = plan(two((LOCAL, 1.5, None), ("mac-a", 0.1, None)), workers)
    assert none.status == NOTHING_MOVES
    assert none.cause is not None and none.cause.startswith("one reading only")


def test_taker_needs_two_ok_readings_and_ceiling_040() -> None:
    workers = [worker("t04")]
    was_busy = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.1, 0.8)), workers)
    assert was_busy.status == NOTHING_MOVES
    assert was_busy.cause == "no machine has room; new work waits"
    first = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.1, None)), workers)
    assert first.status == NOTHING_MOVES
    # 0.35 is `ok` for assess, and one more worker on 10 cores projects 0.45: the dead band.
    dead_band = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.35, 0.35)), workers)
    assert dead_band.status == NOTHING_MOVES
    assert dead_band.cause == "no machine has room; new work waits"
    assert role(dead_band, "mac-a") == ""
    # Exactly 0.40 is not under the ceiling: 0.2 + 1/5.
    edge = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.2, 0.2, {"cores": 5})), workers)
    assert edge.status == NOTHING_MOVES
    under = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.25, 0.25)), workers)
    assert under.status == "move" and under.move is not None
    assert under.move.projected == pytest.approx(0.35)
    assert rebalance.TAKER_CEILING == 0.40


def test_taker_must_be_listed_for_sends_and_have_facts() -> None:
    workers = [worker("t04")]
    assert plan(two(*FULL_TO_A), workers, send=()).status == NOTHING_MOVES
    assert plan(two(*FULL_TO_A), workers, known=facts(LOCAL)).status == NOTHING_MOVES
    no_folder = Facts(
        machines={"mac-a": MachineFacts("mac-a", harnesses={"claude": HarnessFacts(True)})}
    )
    assert plan(two(*FULL_TO_A), workers, known=no_folder).status == NOTHING_MOVES
    not_ready = Facts(
        machines={
            "mac-a": MachineFacts(
                "mac-a", project="/work/p", harnesses={"claude": HarnessFacts(False)}
            )
        }
    )
    assert plan(two(*FULL_TO_A), workers, known=not_ready).status == NOTHING_MOVES
    at_ceiling = plan(
        two(*FULL_TO_A),
        [worker("t04"), worker("mac-a/t01", "mac-a")],
        known=facts(LOCAL, "mac-a", max_total=1),
    )
    assert at_ceiling.status == NOTHING_MOVES


def test_gap_below_030_gives_nothing() -> None:
    workers = [worker("t04")]
    # Full by memory, not by load: the giver's load per core is 0.50.
    low = {"available": 0.05}
    near = plan(two((LOCAL, 0.50, 0.50, low), ("mac-a", 0.15, 0.15)), workers)
    assert near.status == NOTHING_MOVES
    assert near.cause is not None and near.cause.startswith("no worker may move")
    assert "gap" in near.cause
    far = plan(two((LOCAL, 0.60, 0.60, low), ("mac-a", 0.15, 0.15)), workers)
    assert far.status == "move" and far.move is not None
    assert far.move.gap == pytest.approx(0.35)
    assert rebalance.MIN_GAP == 0.30


def test_no_back_and_forth_within_cooldowns() -> None:
    rows = two((LOCAL, 0.1, 0.1), ("mac-a", 0.1, 0.1), ("mac-b", 0.8, 0.8))
    # mac-a gave t01 to mac-b; its successor there is t07. t09 was on mac-b all along.
    successor, other = worker("mac-b/t07", "mac-b"), worker("mac-b/t09", "mac-b")

    def after(seconds: float, *workers: ManagedWorker) -> Plan:
        gave = MoveRecord("M1", "mac-a/t01", "mac-a", "mac-b", NOW - seconds, successor="mac-b/t07")
        return plan(rows, list(workers), moves=(gave,))

    # Five minutes: the machine that gave takes nothing back, whatever worker it is.
    machine = after(299, other)
    assert machine.status == NOTHING_MOVES
    assert machine.cause == "no machine has room; new work waits"
    assert role(machine, "mac-a") == "" and "gave work" in machine.machines[1].why
    assert after(301, other).move is not None
    # Thirty minutes: the worker that moved stays where it went.
    moved = after(301, successor)
    assert moved.status == NOTHING_MOVES
    assert moved.cause is not None and moved.cause.startswith("no worker may move")
    assert any("moved in the last 30 min" in note for note in moved.notes)
    assert after(1799, successor).status == NOTHING_MOVES
    both = after(1799, successor, other)
    assert both.move is not None and both.move.worker == "mac-b/t09"
    free = after(1801, successor)
    assert free.status == "move" and free.move is not None
    assert (free.move.worker, free.move.taker) == ("mac-b/t07", "mac-a")
    assert (rebalance.MACHINE_COOLDOWN, rebalance.WORKER_COOLDOWN) == (300.0, 1800.0)


def test_at_most_one_move_per_plan() -> None:
    rows = two((LOCAL, 1.5, 1.5), ("mac-a", 0.1, 0.1), ("mac-b", 0.1, 0.1), ("mac-c", 1.2, 1.2))
    workers = [
        worker("t04"),
        worker("t05"),
        worker("mac-c/t01", "mac-c"),
        worker("mac-c/t02", "mac-c"),
    ]
    result = plan(rows, workers, send=("mac-a", "mac-b", "mac-c"))
    assert result.status == "move" and result.move is not None
    assert result.move.worker == "t04"
    assert isinstance(result.to_json()["move"], dict)
    assert rebalance.MAX_MOVES == 1


def test_candidate_order_and_tie_breaks_are_stable() -> None:
    rows = two(*FULL_TO_A)
    workers = [
        worker("t01", work="working", receipt="running", since=1.0),
        worker("t09", since=50.0),
        worker("t05", since=50.0),
        worker("t06", since=10.0),
        worker("t08", work="queued", receipt="queued", since=100.0),
    ]

    def chosen(pool: list[ManagedWorker]) -> str:
        result = plan(rows, pool)
        assert result.move is not None
        return result.move.worker

    assert chosen(workers) == "t08"  # queued before idle, though it is the youngest
    assert chosen(workers[:4]) == "t06"  # then the oldest idle worker
    assert chosen([workers[0], workers[1], workers[2]]) == "t05"  # then the id
    assert chosen(workers[:1]) == "t01"  # a working worker last
    shuffled = list(workers)
    random.Random(7).shuffle(shuffled)
    assert plan(rows, shuffled) == plan(rows, workers) == plan(rows, workers)
    assert plan(rows, shuffled).id == plan(rows, workers).id

    # Givers: the coordinator first, then full before busy, then the higher load, then the name.
    many = two(
        (LOCAL, 0.8, 0.8), ("mac-a", 0.1, 0.1), ("mac-c", 1.5, 1.5), ("mac-d", 0.9, 0.9),
        ("mac-e", 0.9, 0.9),
    )  # fmt: skip
    everywhere = [
        worker("t04"),
        *(worker(f"{name}/t01", name) for name in ("mac-c", "mac-d", "mac-e")),
    ]
    givers = []
    while everywhere:
        result = plan(many, everywhere)
        assert result.move is not None
        givers.append(result.move.giver)
        everywhere = [item for item in everywhere if item.id != result.move.worker]
    assert givers == [LOCAL, "mac-c", "mac-d", "mac-e"]

    # Takers: `choose` decides. Same band of load, so the order of the facts, then the name.
    both = two((LOCAL, 1.5, 1.5), ("mac-a", 0.1, 0.1), ("mac-b", 0.1, 0.1))
    assert plan(both, [worker("t04")]).move.taker == "mac-a"  # type: ignore[union-attr]
    ordered = facts(LOCAL, "mac-a", "mac-b", order=("mac-b", "mac-a"))
    assert plan(both, [worker("t04")], known=ordered).move.taker == "mac-b"  # type: ignore[union-attr]


def test_no_taker_answers_nothing_moves_not_error() -> None:
    result = plan(two((LOCAL, 1.5, 1.5), ("mac-a", 0.8, 0.8)), [worker("t04")])
    assert result.status == NOTHING_MOVES == "NOTHING_MOVES"
    assert result.cause == "no machine has room; new work waits"
    assert result.move is None and result.id is None
    assert result.to_json()["move"] is None


def test_22_november_readings_move_nothing() -> None:
    # 13:04: load 181 on 16 cores on the coordinator; mac-a at 0.18 per core, 0 of 10 agents.
    rows = two((LOCAL, 181 / 16, 181 / 16, {"cores": 16}), ("mac-a", 0.18, 0.18, {"cores": 16}))
    result = plan(rows, [], known=facts("mac-a", max_total=10), send=("mac-a",))
    assert result.status == NOTHING_MOVES and result.move is None
    assert result.cause == f"no worker load on {LOCAL}"
    local, mac_a = result.machines
    assert (local.machine, local.state, local.was, local.workers) == (LOCAL, "full", "full", 0)
    assert local.load == pytest.approx(11.3125)
    assert (mac_a.state, mac_a.was, mac_a.role, mac_a.ceiling) == ("ok", "ok", "taker", 10)
    assert any("may not move" in note for note in result.notes)
    assert any("mac-a" in note for note in result.notes)
    # Had an idle worker been there, the card's plan: gap 11.07, projected 0.24.
    with_worker = plan(rows, [worker("t04")], known=facts("mac-a", max_total=10), send=("mac-a",))
    assert with_worker.move is not None
    assert round(with_worker.move.gap, 2) == 11.07 and round(with_worker.move.projected, 2) == 0.24


def test_coordinator_is_never_taker() -> None:
    rows = two((LOCAL, 0.05, 0.05), ("mac-b", 0.9, 0.9))
    result = plan(rows, [worker("mac-b/t01", "mac-b")], send=(LOCAL, "mac-a", "mac-b"))
    assert result.status == NOTHING_MOVES
    assert result.cause == "no machine has room; new work waits"
    view = next(item for item in result.machines if item.machine == LOCAL)
    assert view.role == "" and "coordinator" in view.why
    assert rebalance.COORDINATOR == LOCAL


@pytest.mark.parametrize(
    "target",
    [
        reading("mac-a", 1.2),
        reading("mac-a", 0.1, available=0.05),
        reading("mac-a", 0.1, error="unreachable"),
        reading("mac-a", 0.1, error="timeout"),
        reading("mac-a", 0.1, at=NOW - 61),
        reading("mac-a", 0.1, at=NOW + 1),
        replace(reading("mac-a", 0.1), cores=None),
        None,
    ],
    ids=[
        "full-cpu",
        "full-memory",
        "unreachable",
        "timeout",
        "stale",
        "future",
        "no-cores",
        "absent",
    ],
)
def test_full_or_unknown_machine_is_never_taker(target: Reading | None) -> None:
    readings = {LOCAL: reading(LOCAL, 1.5)}
    previous = {
        LOCAL: reading(LOCAL, 1.5, at=NOW - 125),
        "mac-a": reading("mac-a", 0.1, at=NOW - 125),
    }
    if target is not None:
        readings["mac-a"] = target
    result = plan((readings, previous), [worker("t04")])
    assert result.status == NOTHING_MOVES and result.move is None
    assert result.cause == "no machine has room; new work waits"
    assert all(view.role != "taker" for view in result.machines)


def test_good_memory_does_not_downgrade_full() -> None:
    # 97% of the memory free and a load of 1.2 per core: `full`, whatever the memory says.
    rows = two((LOCAL, 1.5, 1.5), ("mac-a", 1.2, 1.2, {"available": 0.97}))
    result = plan(rows, [worker("t04")])
    view = next(item for item in result.machines if item.machine == "mac-a")
    assert (view.state, view.was, view.role) == ("full", "full", "")
    assert result.status == NOTHING_MOVES


@pytest.mark.parametrize(
    "change",
    [
        {"lane_stays": True},
        {"gate_class": True},
        {"opened_by_person": True},
        {"receipt": "uncertain"},
        {"receipt": "blocked"},
        {"receipt": "unknown"},
        {"work": "finished"},
    ],
    ids=lambda change: "-".join(f"{key}={value}" for key, value in change.items()),
)
def test_protected_lanes_classes_and_person_terminals_stay(change: dict[str, Any]) -> None:
    rows = two(*FULL_TO_A)
    protected = worker("t04", **change)
    alone = plan(rows, [protected])
    assert alone.status == NOTHING_MOVES and alone.move is None
    assert alone.cause is not None and alone.cause.startswith("no worker may move")
    assert role(alone, LOCAL) != "giver"
    beside = plan(rows, [protected, worker("t09")])
    assert beside.move is not None and beside.move.worker == "t09"


def test_plan_then_plan_again_same_readings_moves_nothing() -> None:
    rows, workers = two(*FULL_TO_A), [worker("t04")]
    first = plan(rows, workers)
    assert first.move is not None and first.id is not None
    applied = MoveRecord(
        first.id, first.move.worker, first.move.giver, first.move.taker, NOW, open=True
    )
    again = plan(rows, workers, moves=(applied,))
    assert again.status == NOTHING_MOVES and again.move is None
    assert again.cause == f"move {first.id} in flight"
    # And the plan itself, asked twice with no move started, is the same plan.
    assert plan(rows, workers) == first


def test_bad_input_is_refused() -> None:
    rows = two(*FULL_TO_A)
    with pytest.raises(ValueError, match="now"):
        plan(rows, [], now=float("nan"))
    with pytest.raises(ValueError, match="twice"):
        plan(rows, [worker("t04"), worker("t04")])
    with pytest.raises(ValueError, match="names"):
        plan(({LOCAL: reading("mac-a", 0.1)}, {}), [])


def test_module_reads_no_clock_file_or_process() -> None:
    source = Path(rebalance.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported <= {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "hashlib",
        "math",
        "typing",
        ".assess",
        ".choose",
        ".facts",
        ".probe",
    }
    names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert not names & {"open", "print", "input", "exec", "eval", "__import__"}


@pytest.mark.parametrize(
    "change",
    [
        {"load1": "not-a-number"},
        {"load5": {}},
        {"load15": True},
        {"taken_at": "not-a-time"},
        {"taken_at": float("inf")},
        {"cores": "sixteen"},
        {"cores": 2.5},
        {"mem_total_kb": "unknown"},
    ],
)
@pytest.mark.parametrize("which", ["newest", "earlier"])
def test_a_reading_with_a_field_that_is_no_number_is_not_measured(
    change: dict[str, Any], which: str
) -> None:
    for name in ("mac-a", LOCAL):
        readings, previous = two(*FULL_TO_A)
        group = readings if which == "newest" else previous
        group[name] = replace(group[name], **change)
        result = plan((readings, previous), [worker("t04")])
        view = next(item for item in result.machines if item.machine == name)
        if (name, which) == (LOCAL, "earlier"):
            # `full` gives on its newest reading alone; the earlier one is only not counted.
            assert result.status == "move" and view.role == "giver"
        else:
            assert result.status == NOTHING_MOVES and result.move is None and result.cause
            assert view.role == ""
        if which == "newest":
            assert (view.state, view.load) == ("unknown", None)
        else:
            assert view.was is None and view.spacing == "a reading is not a measurement"
        json.dumps(result.to_json())


def test_a_newest_reading_older_than_60_seconds_is_not_measured_whatever_the_facts_say() -> None:
    known = replace(facts(LOCAL, "mac-a"), thresholds=replace(Facts().thresholds, max_age=600.0))
    for age, status in ((60.0, "move"), (61.0, NOTHING_MOVES)):
        readings, previous = two(*FULL_TO_A)
        readings["mac-a"] = replace(readings["mac-a"], taken_at=NOW - age)
        previous["mac-a"] = replace(previous["mac-a"], taken_at=NOW - age - 120)
        result = plan((readings, previous), [worker("t04")], known=known)
        assert result.status == status, age
    assert role(result, "mac-a") == ""
    assert next(v.state for v in result.machines if v.machine == "mac-a") == "unknown"
    # The giver's newest reading is held to the same age.
    readings, previous = two(*FULL_TO_A)
    readings[LOCAL] = replace(readings[LOCAL], taken_at=NOW - 61)
    previous[LOCAL] = replace(previous[LOCAL], taken_at=NOW - 181)
    assert plan((readings, previous), [worker("t04")], known=known).status == NOTHING_MOVES
    assert rebalance.MAX_AGE == 60.0


def test_busy_now_and_full_before_is_not_busy_on_both_readings() -> None:
    result = plan(two((LOCAL, 0.8, 1.5), ("mac-a", 0.1, 0.1)), [worker("t04")])
    assert result.status == NOTHING_MOVES
    assert result.cause == "no machine is full, or busy on two readings"
    assert role(result, LOCAL) != "giver"


def test_only_the_first_machine_choose_ranks_is_tried_for_a_worker() -> None:
    # Full by memory at 0.55 a core. `choose` ranks mac-a first; its projected load is
    # 0.30, a gap of 0.25. mac-b would pass the gap, and is not asked.
    low = {"available": 0.05}
    rows = two((LOCAL, 0.55, 0.55, low), ("mac-a", 0.2, 0.2), ("mac-b", 0.1, 0.1))
    result = plan(rows, [worker("t04")])
    assert result.status == NOTHING_MOVES
    assert result.cause == "no worker may move: gap 0.25 from local to mac-a is below 0.30"
    alone = plan(two((LOCAL, 0.55, 0.55, low), ("mac-b", 0.1, 0.1)), [worker("t04")])
    assert alone.move is not None and alone.move.taker == "mac-b"


def test_a_full_giver_comes_before_a_busy_one_with_more_load() -> None:
    rows = two(
        (LOCAL, 0.1, 0.1),
        ("mac-a", 0.1, 0.1),
        ("mac-b", 0.9, 0.9),
        ("mac-c", 0.55, 0.55, {"available": 0.05}),
    )
    result = plan(rows, [worker("mac-b/t01", "mac-b"), worker("mac-c/t01", "mac-c")])
    assert result.move is not None and result.move.giver == "mac-c"


def test_the_giver_with_more_load_comes_before_an_earlier_name() -> None:
    rows = two((LOCAL, 0.1, 0.1), ("mac-a", 0.1, 0.1), ("mac-b", 1.1, 1.1), ("mac-c", 1.5, 1.5))
    result = plan(rows, [worker("mac-b/t01", "mac-b"), worker("mac-c/t01", "mac-c")])
    assert result.move is not None and result.move.giver == "mac-c"


@pytest.mark.parametrize("field", ["process_count", "harness_processes", "suites_running"])
@pytest.mark.parametrize("which", ["newest", "earlier"])
def test_an_integer_too_large_for_a_float_makes_the_reading_not_measured(
    field: str, which: str
) -> None:
    readings, previous = two(*FULL_TO_A)
    group = readings if which == "newest" else previous
    group["mac-a"] = replace(group["mac-a"], **{field: 10**400})
    result = plan((readings, previous), [worker("t04")])
    assert result.status == NOTHING_MOVES and result.move is None
    assert result.cause == "no machine has room; new work waits"
    view = next(item for item in result.machines if item.machine == "mac-a")
    assert view.role == ""
    if which == "newest":
        assert (view.state, view.load) == ("unknown", None)
    else:
        assert view.was is None and view.spacing == "a reading is not a measurement"
