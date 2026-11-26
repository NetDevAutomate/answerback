"""A pure choice of a machine (fabric row P3), on the measurement's `assess` and `rank`.

Labels: `red_at_pretip` fails at d98fba8 (no `placement.choose` module there);
`pins_existing` passes there on purpose.
"""

import copy
import itertools
import json
import math
import re
import sys
from dataclasses import fields, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.placement.assess import assess, rank
from answerback_bridge.placement.probe import Reading, parse_reading

FIXTURES = Path(__file__).parent / "fixtures/placement"
# The maintainers' own policy file (`status/routing.json` of the combined repository, now in
# their private archive) with its project path, launcher path and space neutralised and its
# host, credential and ruling notes left out (T-271).
REPO_POLICY = Path(__file__).parent / "fixtures" / "repository-routing-policy.json"
NOW = 1000.0
NODES = ("node-a", "node-b", "node-c")
READY = {"codex": {"ready": True}}


def reading(machine: str, fixture: str, **changes: Any) -> Reading:
    """A probe fixture read as `machine`, taken ten seconds before NOW."""
    text = (FIXTURES / f"{fixture}.txt").read_text()
    return replace(parse_reading(machine, text, NOW - 10), **changes)


def idle() -> list[Reading]:
    """The coordinator and three nodes, all `ok` and all below a quarter core of load."""
    return [
        reading("local", "darwin-idle"),
        reading("node-a", "linux-a"),
        reading("node-b", "linux-b"),
        reading("node-c", "linux-a"),
    ]


def make_facts(machines: dict[str, Any] | None = None, **block: Any) -> Any:
    """Facts read from a policy file: every machine ready for codex unless given."""
    from answerback_bridge.placement.facts import parse_facts

    raw = json.loads(REPO_POLICY.read_text())
    entries = {
        name: {"project": f"/work/{name}", "tools": ["uv"], "harnesses": copy.deepcopy(READY)}
        for name in ("local", *NODES)
    }
    for name, entry in (machines or {}).items():
        entries.setdefault(name, {}).update(entry)
    raw["placement"] = {"order": list(NODES), "machines": entries, **block}
    return parse_facts(raw)


def burst(readings: list[Reading], facts: Any, count: int, **arguments: Any) -> list[str | None]:
    """`count` choices in one second, each fed the earlier ones as recent placements."""
    from answerback_bridge.placement.choose import Placement, choose

    recent: list[Placement] = []
    chosen: list[str | None] = []
    for index in range(count):
        choice = choose(
            readings, facts, harness="codex", now=NOW + index / 4, recent=recent, **arguments
        )
        chosen.append(choice.machine)
        # The status, the machine, its project and the head of `ranked` say the same thing.
        assert (choice.status == "placed") == (choice.machine is not None)
        assert choice.ranked[:1] == (() if choice.machine is None else (choice.machine,))
        assert choice.project == (None if choice.machine is None else f"/work/{choice.machine}")
        if choice.machine is not None:
            recent.append(Placement(choice.machine, NOW + index / 4))
    return chosen


def test_pins_existing_rank_alone_puts_the_coordinator_first_for_light_work() -> None:
    # pins_existing: what `rank` alone answers, and so what `choose` has to add.
    rows = [assess(row, now=NOW) for row in idle()]
    assert [row.state for row in rows] == ["ok"] * 4
    assert rank(rows, heavy=False, order=["local", *NODES])[0] == "local"
    assert rank(rows, heavy=True, order=NODES) == ["node-a", "node-b", "node-c"]
    assert rank(rows, heavy=True, order=NODES) == rank(rows, heavy=True, order=NODES)


def test_red_at_pretip_deciding_burst_of_four_heavy_routes_spreads() -> None:
    # The plan's deciding test: measured machines with free capacity, a burst of four.
    # Readings, facts and worker counts stay as they are; only the placements grow.
    chosen = burst(idle(), make_facts(), 4, heavy=True)
    assert None not in chosen
    assert chosen == ["node-a", "node-b", "node-c", "node-a"]
    assert len(set(chosen)) > 1 and max(map(chosen.count, chosen)) < 4
    assert "local" not in chosen
    # Without the earlier placements every route of the burst goes to the same machine.
    from answerback_bridge.placement.choose import choose

    alone = [choose(idle(), make_facts(), harness="codex", now=NOW).machine for _ in range(4)]
    assert alone == ["node-a"] * 4


def test_red_at_pretip_deciding_light_work_stays_off_the_coordinator_while_a_node_is_ok() -> None:
    from answerback_bridge.placement.choose import choose

    facts = make_facts()
    assert "local" not in burst(idle(), facts, 4, heavy=False)
    # Also when the coordinator is the least loaded: nodes in band 1, the coordinator in band 0.
    busy_nodes = [idle()[0], *(replace(row, load1=6.0, load5=6.0) for row in idle()[1:])]
    choice = choose(busy_nodes, facts, harness="codex", now=NOW, heavy=False)
    assert (choice.status, choice.machine, choice.project) == ("placed", "node-a", "/work/node-a")
    assert choice.ranked == ("node-a", "node-b", "node-c", "local")
    # One node left: still that node.
    one = [idle()[0], reading("node-b", "linux-b")]
    assert choose(one, facts, harness="codex", now=NOW, heavy=False).ranked == ("node-b", "local")


def not_ok_nodes() -> dict[str, list[Reading]]:
    """Node readings of which none is `ok`, by kind."""
    return {
        "full": [reading(name, "darwin-compressor-warning") for name in NODES],
        "busy": [reading(name, "linux-a", load1=12.0, load5=4.0) for name in NODES],
        "timeout": [Reading(name, NOW - 10, error="timeout") for name in NODES],
        "stale": [reading(name, "linux-b", taken_at=NOW - 60.5) for name in NODES],
        "future": [reading(name, "linux-b", taken_at=NOW + 5) for name in NODES],
        "absent": [],
    }


@pytest.mark.parametrize("kind", ["full", "busy", "timeout", "stale", "future", "absent"])
def test_red_at_pretip_deciding_no_node_ok(kind: str) -> None:
    from answerback_bridge.placement.choose import Choice, choose

    facts = make_facts()
    nodes = not_ok_nodes()[kind]
    if kind in ("full", "busy"):
        assert {assess(row, now=NOW).state for row in nodes} == {kind}
    measured = [reading("local", "darwin-idle"), *nodes]
    light = choose(measured, facts, harness="codex", now=NOW, heavy=False)
    assert (light.status, light.machine, light.ranked) == ("placed", "local", ("local",))
    heavy = choose(measured, facts, harness="codex", now=NOW, heavy=True)
    assert isinstance(heavy, Choice)
    assert (heavy.status, heavy.machine, heavy.project, heavy.ranked) == (
        "at_capacity",
        None,
        None,
        (),
    )
    assert {row.machine for row in heavy.skipped} == {"local", *NODES}
    # The coordinator not `ok` either: no machine, for light work too.
    nobody = [reading("local", "darwin-swap-residue"), *nodes]
    for is_heavy in (True, False):
        answer = choose(nobody, facts, harness="codex", now=NOW, heavy=is_heavy)
        assert (answer.status, answer.machine, answer.ranked) == ("at_capacity", None, ())
        assert burst(nobody, facts, 4, heavy=is_heavy) == [None] * 4


def test_red_at_pretip_recent_placements_add_load() -> None:
    from answerback_bridge.placement.choose import Placement, Skip, choose

    small = [
        reading("node-a", "linux-a", cores=4, load1=2.0, load5=2.0),
        reading("node-b", "linux-b", cores=4, load1=2.5, load5=2.5),
    ]
    facts = make_facts()
    assert choose(small, facts, harness="codex", now=NOW).machine == "node-a"
    # One placement at worker_cost 1.0 on four cores: 0.50 becomes 0.75 a core, `busy`.
    one = choose(small, facts, harness="codex", now=NOW, recent=[Placement("node-a", NOW - 1)])
    assert one.machine == "node-b" and one.ranked == ("node-b",)
    assert [(row.machine, row.reason) for row in one.skipped if row.machine == "node-a"] == [
        ("node-a", "busy: CPU load/core 0.750")
    ]
    assert [row.load_per_core for row in one.assessed] == [0.75, 0.625]
    # Each of the two loads carries the placement: `assess` takes the larger of them.
    for loads in ({"load1": 2.0, "load5": 0.0}, {"load1": 0.0, "load5": 2.0}):
        lone = [replace(small[0], **loads)]
        assert choose(lone, facts, harness="codex", now=NOW).machine == "node-a"
        after = choose(lone, facts, harness="codex", now=NOW, recent=[Placement("node-a", NOW)])
        assert after.status == "at_capacity" and after.skipped[0].reason.startswith("busy: CPU")
    # Just below the boundary: 1.79 + 1.0 on four cores is 0.6975 a core, still `ok`.
    near = [replace(small[0], load1=1.79, load5=1.79)]
    assert choose(
        near, facts, harness="codex", now=NOW, recent=[Placement("node-a", NOW)]
    ).ranked == ("node-a",)
    # The band decides before the count: node-b has no recent placement but a higher band.
    wide = [replace(small[0], cores=16), replace(small[1], cores=16, load1=5.0, load5=5.0)]
    two = choose(wide, facts, harness="codex", now=NOW, recent=[Placement("node-a", NOW - 1)])
    assert two.ranked == ("node-a", "node-b")
    # worker_cost 0 leaves only the count as the tie-break; a cost of 3 fills the machine.
    free = make_facts(worker_cost=0)
    kept = choose(small, free, harness="codex", now=NOW, recent=[Placement("node-a", NOW - 1)])
    assert kept.ranked == ("node-b", "node-a")
    assert [row.load_per_core for row in kept.assessed] == [0.5, 0.625]
    dear = make_facts(worker_cost=3)
    gone = choose(small, dear, harness="codex", now=NOW, recent=[Placement("node-a", NOW - 1)])
    assert gone.ranked == ("node-b",)
    assert gone.skipped[0] == Skip("node-a", "full: CPU load/core 1.250")


def test_red_at_pretip_recent_window() -> None:
    from answerback_bridge.placement.choose import Placement, choose

    facts = make_facts()
    small = [reading("node-a", "linux-a", cores=4, load1=2.0, load5=2.0)]

    def at(when: float) -> str | None:
        placements = [Placement("node-a", when), Placement("elsewhere", NOW)]
        return choose(small, facts, harness="codex", now=NOW, recent=placements).machine

    assert at(NOW - 120) is None
    assert at(NOW - 120.5) == "node-a"
    assert at(NOW - 1) is None
    assert at(NOW + 5) is None  # dated after now: counted
    assert at(NOW + 5000) is None


def test_red_at_pretip_count_breaks_the_tie_before_the_order() -> None:
    from answerback_bridge.placement.choose import Placement, choose

    recent = [Placement("node-a", NOW - 5), Placement("node-a", NOW - 4), Placement("node-b", NOW)]
    choice = choose(idle(), make_facts(), harness="codex", now=NOW, recent=recent)
    assert choice.ranked == ("node-c", "node-b", "node-a")
    backwards = make_facts(order=["node-c", "node-b", "node-a"])
    assert choose(idle(), backwards, harness="codex", now=NOW).ranked == (
        "node-c",
        "node-b",
        "node-a",
    )
    unordered = make_facts(order=[])
    assert choose(idle(), unordered, harness="codex", now=NOW).ranked == NODES


@pytest.mark.parametrize(
    ("machines", "arguments", "reason"),
    [
        ({"node-a": {"project": None}}, {}, "no project folder recorded"),
        ({"node-a": {"harnesses": {}}}, {}, "harness codex is not recorded there"),
        (
            {"node-a": {"harnesses": {"codex": {"ready": False}}}},
            {},
            "harness codex is not ready there",
        ),
        (
            {"node-a": {"harnesses": {"codex": {"ready": False, "why": "no login"}}}},
            {},
            "harness codex is not ready there: no login",
        ),
        ({"node-a": {"harnesses": {"claude": {"ready": True}}}}, {}, "harness codex is not rec"),
        ({}, {"harness": "claude"}, "harness claude is not recorded there"),
        (
            {"node-a": {"tools": ["uv"]}, "node-b": {"tools": ["uv", "gh", "just"]}},
            {"tools": ["just", "uv", "gh"], "heavy": False},
            "tools not recorded there: gh, just",
        ),
        (
            {"node-a": {"harnesses": {"codex": {"ready": True, "max_workers": 2}}}},
            {"active": {"node-a": {"codex": 2}}},
            "at max_workers 2 for codex",
        ),
        (
            {"node-a": {"max_total": 3}},
            {"active": {"node-a": {"codex": 1, "kiro": 2}}},
            "full: Active agents 3, ceiling 3",
        ),
    ],
)
def test_red_at_pretip_facts_gate(
    machines: dict[str, Any], arguments: dict[str, Any], reason: str
) -> None:
    from answerback_bridge.placement.choose import choose

    facts = make_facts(machines)
    assert choose(idle(), make_facts(), harness="codex", now=NOW).machine == "node-a"
    choice = choose(idle(), facts, **{"harness": "codex", "now": NOW, **arguments})
    assert "node-a" not in choice.ranked
    found = [row.reason for row in choice.skipped if row.machine == "node-a"]
    assert len(found) == 1 and found[0].startswith(reason)
    if arguments.get("harness") == "claude":
        assert choice.status == "at_capacity"
    else:
        assert choice.machine == "node-b"


def test_red_at_pretip_ceilings_count_live_workers_only() -> None:
    from answerback_bridge.placement.choose import Placement, choose

    facts = make_facts(
        {"node-a": {"max_total": 3, "harnesses": {"codex": {"ready": True, "max_workers": 2}}}}
    )

    def first(**arguments: Any) -> str | None:
        return choose(idle(), facts, harness="codex", now=NOW, **arguments).machine

    # A machine with a ceiling and no count is not taken for an empty one.
    for absent in (None, {}, {"node-b": {"codex": 0}}):
        missing = choose(idle(), facts, harness="codex", now=NOW, active=absent)
        assert missing.machine == "node-b"
        assert missing.skipped[1].machine == "node-a"
        assert missing.skipped[1].reason == "it has a ceiling and active holds no count for it"
    assert first(active={"node-a": {}}) == "node-a"
    assert first(active={"node-a": {"codex": 1, "kiro": 1}}) == "node-a"
    assert first(active={"node-a": {"codex": 1, "kiro": 2}}) == "node-b"
    assert first(active={"node-a": {"kiro": 2, "codex": 0}}) == "node-a"
    busy_elsewhere = {"node-a": {}, "node-b": {"codex": 9}, "elsewhere": {"codex": 9}}
    assert first(active=busy_elsewhere) == "node-a"
    # A recent placement is load, not a worker: three sends to live terminals fill no ceiling.
    sends = [Placement("node-a", NOW)] * 3
    sent = choose(idle(), facts, harness="codex", now=NOW, recent=sends, active={"node-a": {}})
    assert sent.ranked == ("node-b", "node-c", "node-a")
    assert [row.machine for row in sent.skipped] == ["local"]


def test_red_at_pretip_missing_reading_or_facts() -> None:
    from answerback_bridge.placement.choose import Skip, choose

    facts = make_facts()
    stranger = reading("node-x", "linux-b")
    choice = choose([stranger, reading("node-c", "linux-a")], facts, harness="codex", now=NOW)
    assert choice.ranked == ("node-c",)
    assert choice.skipped == (
        Skip("node-x", "no machine facts"),
        Skip("local", "not measured"),
        Skip("node-a", "not measured"),
        Skip("node-b", "not measured"),
    )
    assert [row.machine for row in choice.assessed] == ["node-c"]
    from answerback_bridge.placement.facts import Facts

    nothing = choose(idle(), Facts(), harness="codex", now=NOW, heavy=False)
    assert (nothing.status, nothing.machine) == ("at_capacity", None)
    assert {row.reason for row in nothing.skipped} == {"no machine facts"}
    empty = choose([], Facts(), harness="codex", now=NOW)
    assert (empty.status, empty.skipped, empty.assessed) == ("at_capacity", (), ())


BAD = {
    "full": lambda: reading("node-a", "darwin-compressor-warning"),
    "full-cpu": lambda: reading("node-a", "darwin-swap-residue"),
    "busy": lambda: reading("node-a", "linux-a", load1=12.0, load5=4.0),
    "timeout": lambda: Reading("node-a", NOW - 10, error="timeout"),
    "truncated": lambda: reading("node-a", "linux-b", error="truncated"),
    "stale": lambda: reading("node-a", "linux-b", taken_at=NOW - 61),
    "future": lambda: reading("node-a", "linux-b", taken_at=NOW + 1),
    "no-cores": lambda: reading("node-a", "linux-b", cores=None),
    "no-load": lambda: reading("node-a", "linux-b", load5=None),
    "nan-load": lambda: reading("node-a", "linux-b", load1=math.nan),
    "nan-load5": lambda: reading("node-a", "linux-b", load5=math.nan),
    "nan-load15": lambda: reading("node-a", "linux-b", load15=math.nan),
    "nan-taken-at": lambda: reading("node-a", "linux-b", taken_at=math.nan),
    "infinite-load": lambda: reading("node-a", "linux-b", load1=math.inf),
}


@pytest.mark.parametrize("kind", sorted(BAD))
def test_red_at_pretip_a_machine_that_is_not_ok_is_never_chosen(kind: str) -> None:
    from answerback_bridge.placement.choose import Placement, choose

    bad = BAD[kind]()
    if kind not in ("future", "nan-load", "nan-load5", "nan-load15", "nan-taken-at"):
        assert assess(bad, now=NOW).state != "ok"
    others = {
        "none": [],
        "local": [reading("local", "darwin-idle")],
        "all": [reading("local", "darwin-idle"), reading("node-b", "linux-b")],
        "loaded": [reading("node-b", "linux-a", load1=10.0, load5=10.0)],
    }
    facts = [make_facts(order=["node-a"]), make_facts(worker_cost=0)]
    recents = [[], [Placement("node-b", NOW)] * 6, [Placement("node-a", NOW - 500)]]
    actives = [None, {"node-b": {"codex": 5}}, {"node-a": {"codex": 0}}]
    table = itertools.product(others.values(), facts, (True, False), recents, actives, ((), ["uv"]))
    for rest, fact, heavy, recent, active, tools in table:
        for measured in ([bad, *rest], [*rest, bad]):
            choice = choose(
                measured,
                fact,
                harness="codex",
                now=NOW,
                heavy=heavy,
                recent=recent,
                active=active,
                tools=tools,
            )
            assert choice.machine != "node-a" and "node-a" not in choice.ranked
            assert [row.machine for row in choice.skipped].count("node-a") == 1
            if not rest:
                assert (choice.status, choice.machine) == ("at_capacity", None)


def test_red_at_pretip_threshold_overrides_change_the_answer() -> None:
    from answerback_bridge.placement.choose import choose

    # linux-a carries 2.11 on 16 cores (0.132 a core); linux-b 0.39 (0.024).
    assert choose(idle(), make_facts(), harness="codex", now=NOW).ranked == NODES
    strict = make_facts(thresholds={"cpu_busy": 0.1})
    choice = choose(idle(), strict, harness="codex", now=NOW, heavy=False)
    assert choice.ranked == ("node-b",)
    assert {row.machine: row.reason for row in choice.skipped} == {
        "local": "busy: CPU load/core 0.241",
        "node-a": "busy: CPU load/core 0.132",
        "node-c": "busy: CPU load/core 0.132",
    }
    short = make_facts(thresholds={"max_age": 5})
    assert choose(idle(), short, harness="codex", now=NOW).status == "at_capacity"


def test_red_at_pretip_same_inputs_same_answer_and_inputs_unchanged() -> None:
    from answerback_bridge.placement.choose import Placement, choose

    readings, facts = idle(), make_facts()
    recent = [Placement("node-a", NOW - 3), Placement("node-b", NOW - 200)]
    active = {"node-c": {"codex": 1}}
    before = (copy.deepcopy(readings), make_facts(), list(recent), copy.deepcopy(active))

    def run(measured: Any) -> Any:
        return choose(
            measured, facts, harness="codex", now=NOW, tools=["uv"], recent=recent, active=active
        )

    first = run(readings)
    assert first == run(readings) == run(tuple(readings)) == run(iter(readings))
    # One-pass inputs give the same answer: nothing is read a second time per machine.
    assert first == choose(
        (row for row in readings),
        facts,
        harness="codex",
        now=NOW,
        tools=(tool for tool in ["uv"]),
        recent=(item for item in recent),
        active=active,
    )
    lacking = choose(readings, facts, harness="codex", now=NOW, tools=iter(["uv", "gh"]))
    assert lacking.status == "at_capacity" and len(lacking.skipped) == 4
    assert (readings, facts, recent, active) == before
    assert first.ranked == ("node-b", "node-c", "node-a")
    # The order of the readings does not change the choice.
    assert run(readings[::-1]).ranked == first.ranked
    record = json.loads(json.dumps(first.to_json()))
    assert record["status"] == "placed" and record["machine"] == "node-b"
    assert record["project"] == "/work/node-b" and record["ranked"] == list(first.ranked)
    assert record["skipped"] == [
        {"machine": "local", "reason": "the coordinator takes no heavy work"}
    ]
    assert [row["machine"] for row in record["assessed"]] == ["node-a", "node-b", "node-c"]
    assert record["assessed"][0]["load_per_core"] == pytest.approx(3.11 / 16)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"now": math.nan}, "now must be finite"),
        ({"now": math.inf}, "now must be finite"),
        ({"recent": [("node-a", math.nan)]}, "Placement on node-a has a time that is not finite"),
        ({"recent": [("node-a", -math.inf)]}, "Placement on node-a has a time that is not finite"),
        ({"active": {"node-a": {"codex": -1}}}, "must be an int from 0"),
        ({"active": {"node-a": {"codex": True}}}, "must be an int from 0"),
        ({"active": {"node-a": {"codex": 1.0}}}, "must be an int from 0"),
        ({"active": {"elsewhere": {"kiro": None}}}, "must be an int from 0"),
    ],
)
def test_red_at_pretip_callers_faults_raise(arguments: dict[str, Any], message: str) -> None:
    from answerback_bridge.placement.choose import Placement, choose

    if "recent" in arguments:
        arguments = {"recent": [Placement(*item) for item in arguments["recent"]]}
    with pytest.raises(ValueError, match=message):
        choose(idle(), make_facts(), **{"harness": "codex", "now": NOW, **arguments})


def test_red_at_pretip_two_readings_of_one_machine_raise() -> None:
    from answerback_bridge.placement.choose import choose

    twice = [*idle(), Reading("node-b", NOW, error="timeout")]
    with pytest.raises(ValueError, match="Two readings for one machine: node-b"):
        choose(twice, make_facts(), harness="codex", now=NOW)


def test_red_at_pretip_review1_a_load_that_is_not_finite_is_not_measured() -> None:
    # Review 1, B1: finite inputs whose sum overflows must not reach the JSON record.
    from answerback_bridge.placement.choose import Placement, Skip, choose

    huge = make_facts(worker_cost=1e308)
    recent = [Placement("node-a", NOW - 1), Placement("node-a", NOW - 2)]
    choice = choose(idle(), huge, harness="codex", now=NOW, recent=recent)
    assert choice.machine == "node-b" and "node-a" not in choice.ranked
    reason = "not measured: the load with the recent placements added is out of range"
    assert Skip("node-a", reason) in choice.skipped
    assert [row.machine for row in choice.assessed] == ["node-b", "node-c"]
    json.dumps(choice.to_json(), allow_nan=False)
    for name in ("load1", "load5", "load15"):
        for value in (math.inf, -math.inf, math.nan):
            readings = [reading("node-a", "linux-a", **{name: value})]
            answer = choose(readings, make_facts(), harness="codex", now=NOW)
            assert answer.status == "at_capacity" and answer.assessed == ()
            assert answer.skipped[0] == Skip("node-a", "not measured: a load is out of range")
            json.dumps(answer.to_json(), allow_nan=False)


def test_red_at_pretip_review2_a_load_out_of_range_is_not_measured() -> None:
    # Review 2, R2-1 and R2-4: `rank` cannot band a huge load; each load is checked alone.
    from answerback_bridge.placement.choose import MAX_LOAD, Placement, Skip, choose

    assert MAX_LOAD == 1e9
    wide = make_facts(worker_cost=1e308, thresholds={"cpu_busy": 1.6e308, "cpu_full": 1.7e308})
    one = [reading("node-a", "linux-a", cores=1)]
    recent = [Placement("node-a", NOW - 1)]
    added = "not measured: the load with the recent placements added is out of range"
    answer = choose(one, wide, harness="codex", now=NOW, recent=recent)
    assert (answer.status, answer.assessed) == ("at_capacity", ())
    assert answer.skipped[0] == Skip("node-a", added)
    # Each of the two loads alone, at the bound and just over it, with the other missing or not.
    facts = make_facts(worker_cost=2.0, thresholds={"cpu_busy": 1e300, "cpu_full": 1e301})
    for over in ("load1", "load5"):
        other = "load5" if over == "load1" else "load1"
        at = [reading("node-a", "linux-a", **{over: MAX_LOAD - 2.0, other: 0.0})]
        inside = choose(at, facts, harness="codex", now=NOW, recent=recent)
        assert inside.machine == "node-a" and inside.assessed[0].load_per_core == MAX_LOAD / 16
        for rest in (0.0, None):
            beyond = [reading("node-a", "linux-a", **{over: MAX_LOAD - 1.0, other: rest})]
            outside = choose(beyond, facts, harness="codex", now=NOW, recent=recent)
            assert (outside.status, outside.assessed) == ("at_capacity", ())
            assert outside.skipped[0] == Skip("node-a", added)
    for name in ("load1", "load5", "load15"):
        for value in (MAX_LOAD * 1.001, -MAX_LOAD * 1.001, 1e308):
            raw = [reading("node-a", "linux-a", **{name: value})]
            answer = choose(raw, facts, harness="codex", now=NOW)
            assert (answer.status, answer.assessed) == ("at_capacity", ())
            assert answer.skipped[0] == Skip("node-a", "not measured: a load is out of range")
        raw = [reading("node-a", "linux-a", **{name: MAX_LOAD})]
        assert choose(raw, facts, harness="codex", now=NOW).machine == "node-a"


def test_red_at_pretip_review1_each_load_the_reading_holds_gets_the_placements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 1, B3: one load missing must not keep the placements off the other.
    from answerback_bridge.placement import choose as module

    seen: list[tuple[float | None, float | None, float | None]] = []

    def spy(row: Reading, **arguments: Any) -> Any:
        seen.append((row.load1, row.load5, row.load15))
        return assess(row, **arguments)

    monkeypatch.setattr(module, "assess", spy)
    recent = [module.Placement("node-a", NOW - 1)]
    for changes, expected in [
        ({"load1": 2.0, "load5": None, "load15": 0.5}, (3.5, None, 0.5)),
        ({"load1": None, "load5": 2.0, "load15": 0.5}, (None, 3.5, 0.5)),
        ({"load1": 2.0, "load5": 1.0, "load15": None}, (3.5, 2.5, None)),
    ]:
        readings = [reading("node-a", "linux-a", **changes)]
        choice = module.choose(
            readings, make_facts(worker_cost=1.5), harness="codex", now=NOW, recent=recent
        )
        assert seen.pop() == expected and not seen
        # A reading without all three loads is not `ok` and is never chosen.
        assert choice.status == "at_capacity" and choice.machine is None


def test_red_at_pretip_review1_equal_facts_give_equal_answers() -> None:
    # Review 1, B5: two blocks that parse to equal Facts, the machines written in another order.
    from answerback_bridge.placement.choose import choose
    from answerback_bridge.placement.facts import parse_facts

    raw = json.loads(REPO_POLICY.read_text())
    entry = {"project": "/work/x", "harnesses": copy.deepcopy(READY)}
    one = parse_facts({**raw, "placement": {"machines": {"node-b": entry, "node-a": entry}}})
    two = parse_facts({**raw, "placement": {"machines": {"node-a": entry, "node-b": entry}}})
    assert one == two and list(one.machines) != list(two.machines)
    measured = [reading("node-z", "linux-a"), reading("local", "darwin-idle")]
    first = choose(measured, one, harness="codex", now=NOW)
    assert first == choose(measured, two, harness="codex", now=NOW)
    # Measured machines in the order of the readings, then the unmeasured by name.
    assert [row.machine for row in first.skipped] == ["node-z", "local", "node-a", "node-b"]


def test_red_at_pretip_closing_the_sign_of_a_zero_changes_no_choice() -> None:
    # Closing round, R3-2. Red at d98fba8 like the rest; it passes at 95e92bc1, where review 3
    # saw this: equal readings that differ in the sign of a zero give the same choice, and
    # only the diagnostic text of `assess` differs.
    from answerback_bridge.placement.choose import choose

    facts = make_facts()

    def run(zero: float) -> Any:
        measured = [
            reading("local", "darwin-idle"),
            reading("node-b", "linux-b", load1=zero, load5=zero),
            reading("node-c", "linux-a"),
        ]
        return choose(measured, facts, harness="codex", now=NOW, heavy=False)

    plus, minus = run(0.0), run(-0.0)
    assert reading("node-b", "linux-b", load1=0.0) == reading("node-b", "linux-b", load1=-0.0)
    assert (plus.status, plus.machine, plus.project) == ("placed", "node-b", "/work/node-b")
    assert plus.ranked == ("node-b", "node-c", "local")
    for part in ("status", "machine", "project", "ranked", "skipped"):
        assert getattr(plus, part) == getattr(minus, part), part
    assert plus.assessed != minus.assessed and plus != minus


# T-145: the cost of one worker. For the tests below, `red_at_pretip` means red at feffff6,
# the commit T-145 started from. Their facts are built directly, not by `parse_facts`: the
# reader refuses every cost that `choose` must refuse, so only a `Facts` that nothing has
# checked can carry such a cost to `choose`; and an accepted cost that is an int stays an
# int only in a `Facts` built directly.
COST_TEXT = "facts.worker_cost must be a finite number from 0"
REFUSED_COSTS: list[Any] = [
    -1.0,
    -5e-324,
    -1000000001.0,
    -sys.float_info.max,
    math.inf,
    -math.inf,
    math.nan,
    True,
    False,
    "1",
    None,
    10**400,
]
ACCEPTED_COSTS: list[Any] = [0, 0.0, -0.0, 5e-324, 0.25, 1, 3.0, 1e9, sys.float_info.max, 10**308]


class HasFloat:
    """Not an int or a float, though `float()` takes it."""

    def __float__(self) -> float:
        return 0.25

    def __repr__(self) -> str:
        return "HasFloat()"


class HasIndex:
    """Not an int or a float, though `float()` takes it by its `__index__`."""

    def __index__(self) -> int:
        return 1

    def __repr__(self) -> str:
        return "HasIndex()"


# Numbers and number-like values that are neither an int nor a float: the rule is about the
# two types, not about what `float()` can convert.
NOT_INT_OR_FLOAT: list[Any] = [
    Decimal("0.25"),
    Decimal("NaN"),
    Fraction(1, 4),
    complex(1, 0),
    HasFloat(),
    HasIndex(),
]


def cost_id(cost: Any) -> str:
    text = repr(cost)
    return text if len(text) <= 30 else f"{type(cost).__name__} of {len(text)} characters"


def direct_facts(cost: Any) -> Any:
    """The facts of `make_facts()`, built directly, with a cost that nothing has checked."""
    from answerback_bridge.placement.facts import Facts, HarnessFacts, MachineFacts

    machines = {
        name: MachineFacts(
            name,
            project=f"/work/{name}",
            tools=frozenset({"uv"}),
            harnesses={"codex": HarnessFacts(True)},
        )
        for name in ("local", *NODES)
    }
    return Facts(order=NODES, worker_cost=cost, machines=machines)


def loaded(machine: str, fixture: str, load: str, taken_at: float = NOW - 10) -> Reading:
    """A probe fixture with `load` as its three loads, read by the parser as `machine`."""
    text = (FIXTURES / f"{fixture}.txt").read_text()
    changed, count = re.subn(r"(?m)^load=.*$", f"load={load} {load} {load}", text)
    assert count == 1
    return parse_reading(machine, changed, taken_at)


@pytest.mark.parametrize("situation", ["no reading", "idle readings", "idle and one recent"])
@pytest.mark.parametrize("cost", REFUSED_COSTS, ids=cost_id)
def test_red_at_pretip_t145_a_cost_the_reader_refuses_is_refused(cost: Any, situation: str) -> None:
    from answerback_bridge.placement.choose import Placement, choose

    readings = [] if situation == "no reading" else idle()
    recent = [Placement("node-a", NOW)] if situation == "idle and one recent" else []
    with pytest.raises(ValueError) as caught:
        choose(readings, direct_facts(cost), harness="codex", now=NOW, recent=recent)
    message = str(caught.value)
    # The whole text, not a match: it holds no value of the caller.
    assert message == COST_TEXT
    assert len(repr(cost)) <= 30 or repr(cost)[:30] not in message
    # `pins_existing` at 556fdc7: the refusal names no cause and shows no context.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_red_at_pretip_t145_the_cell_of_the_check_is_refused() -> None:
    # The cell of the check of T-133's last commits (its `witness.json`): a reading made by
    # the parser with loads 0, one recent placement, and a cost of -1000000001.0 in facts
    # built directly. At feffff6 `choose` names node-a; before a58e657f it said at_capacity.
    from answerback_bridge.placement.choose import Placement, choose
    from answerback_bridge.placement.facts import Facts, HarnessFacts, MachineFacts

    measured = loaded("node-a", "linux-a", "0", taken_at=NOW)
    assert (measured.load1, measured.load5, measured.load15) == (0.0, 0.0, 0.0)
    machine = MachineFacts(
        "node-a", project="/work/node-a", harnesses={"codex": HarnessFacts(True)}
    )
    facts = Facts(worker_cost=-1000000001.0, machines={"node-a": machine})
    with pytest.raises(ValueError) as caught:
        choose([measured], facts, harness="codex", now=NOW, recent=[Placement("node-a", NOW)])
    assert str(caught.value) == COST_TEXT


def test_red_at_pretip_t145_the_reader_and_choose_accept_the_same_costs() -> None:
    # One table through both: the reader of the policy file and `choose` with facts built
    # directly. Each cost is accepted by both or refused by both, as the table says. The
    # rows of NOT_INT_OR_FLOAT are `pins_existing` at 556fdc7: the reader refuses each with
    # its text for a value that is not a number.
    from answerback_bridge.placement.choose import Placement, choose
    from answerback_bridge.placement.facts import FactsError

    seen = []
    tables = ((False, REFUSED_COSTS), (True, ACCEPTED_COSTS), (False, NOT_INT_OR_FLOAT))
    for accepted, costs in tables:
        for cost in costs:
            try:
                make_facts(worker_cost=cost)
                reader = True
            except FactsError as error:
                assert "placement.worker_cost" in str(error)
                # `pins_existing` at 556fdc7: the reader's refusal names no cause either.
                assert error.__cause__ is None and error.__suppress_context__ is True
                if costs is NOT_INT_OR_FLOAT:
                    assert str(error) == "placement.worker_cost must be a number"
                reader = False
            try:
                choose(
                    idle(),
                    direct_facts(cost),
                    harness="codex",
                    now=NOW,
                    recent=[Placement("node-a", NOW)],
                )
                chooser = True
            except ValueError as error:
                assert str(error) == COST_TEXT
                chooser = False
            seen.append((cost_id(cost), reader, chooser))
            assert (reader, chooser) == (accepted, accepted), seen[-1]
    assert len(seen) == len(REFUSED_COSTS) + len(ACCEPTED_COSTS) + len(NOT_INT_OR_FLOAT) == 28


def test_red_at_pretip_t145_an_accepted_cost_gives_the_answer_of_the_readers_facts() -> None:
    # For every accepted cost, facts built directly give the answer that the facts
    # `parse_facts` makes from the same cost give. Red at feffff6 in one group of cells: the
    # int 10**308 with 257 recent placements raised OverflowError there.
    from answerback_bridge.placement.choose import Choice, Placement, choose

    # The two kinds of facts differ in nothing but how the cost came in.
    assert direct_facts(1.0) == make_facts(worker_cost=1.0)
    # 11.19 on 16 cores is just under the busy threshold of 0.70 a core (11.2).
    cells = list(itertools.product(ACCEPTED_COSTS, (0, 1, 257), ("0", "1.0", "11.19")))
    assert len(cells) == 90
    answers = {}
    for cost, count, load in cells:
        readings = [
            reading("local", "darwin-idle"),
            loaded("node-a", "linux-a", load),
            loaded("node-b", "linux-b", load),
            reading("node-c", "linux-a"),
        ]
        recent = [Placement("node-a", NOW)] * count
        cell = (cost_id(cost), count, load)
        direct = choose(readings, direct_facts(cost), harness="codex", now=NOW, recent=recent)
        parsed = choose(
            readings, make_facts(worker_cost=cost), harness="codex", now=NOW, recent=recent
        )
        for part in fields(Choice):
            assert getattr(direct, part.name) == getattr(parsed, part.name), (cell, part.name)
        # Equal, and written the same: an int and the float of it, or the two zeros, are not.
        assert repr(direct) == repr(parsed), cell
        answers[cell] = direct
    # The cells are not all one answer: under the busy threshold one placement matters.
    assert "node-a" in answers[("0.25", 0, "11.19")].ranked
    assert "node-a" not in answers[("0.25", 1, "11.19")].ranked
    assert "node-a" in answers[("0", 257, "11.19")].ranked
    assert answers[("1", 1, "0")].ranked == ("node-b", "node-c", "node-a")
    assert skip_reason(answers[(cost_id(10**308), 257, "0")]) == (
        "not measured: the load with the recent placements added is out of range"
    )


class FloatRaising(float):
    """A float whose own conversion raises the error it was made with."""

    def __new__(cls, value: float, error: Exception) -> "FloatRaising":
        made = super().__new__(cls, value)
        made.error = error
        return made

    def __float__(self) -> float:
        raise self.error


class IntRaising(int):
    """An int whose own conversion to a float raises the error it was made with."""

    def __new__(cls, value: int, error: Exception) -> "IntRaising":
        made = super().__new__(cls, value)
        made.error = error
        return made

    def __float__(self) -> float:
        raise self.error


class ReadsAsFloat:
    """Not a float and with no `__float__`, but its `__class__` reads as float."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        return float


def both_errors(cost: Any) -> tuple[Exception, Exception]:
    """What the reader raises for `cost` as `worker_cost`, and what `choose` raises."""
    from answerback_bridge.placement.choose import Placement, choose

    with pytest.raises(Exception) as reader:  # noqa: B017 - the caller asserts the class
        make_facts(worker_cost=cost)
    with pytest.raises(Exception) as chooser:  # noqa: B017
        choose(
            idle(), direct_facts(cost), harness="codex", now=NOW, recent=[Placement("node-a", NOW)]
        )
    return reader.value, chooser.value


def test_pins_existing_t145_other_errors_of_a_subclass_conversion() -> None:
    # `pins_existing` at 556fdc7. An error of another class from the conversion of a subclass
    # instance passes through the reader and `choose` as it is. An OverflowError from there
    # is taken by the rule's own `except`, as for an int too large for a float: the reader
    # (at feffff6 too) says the number is not finite, and `choose` refuses the cost.
    from answerback_bridge.placement.facts import FactsError

    reader, chooser = both_errors(FloatRaising(17.0, RuntimeError("caller-runtime=17.0")))
    assert (type(reader), str(reader)) == (RuntimeError, "caller-runtime=17.0")
    assert (type(chooser), str(chooser)) == (RuntimeError, "caller-runtime=17.0")

    reader, chooser = both_errors(FloatRaising(17.0, OverflowError("caller-overflow")))
    assert type(reader) is FactsError
    assert str(reader) == "placement.worker_cost must be finite and not negative"
    assert (type(chooser), str(chooser)) == (ValueError, COST_TEXT)


@pytest.mark.parametrize(
    ("make", "kind", "text"),
    [
        (
            lambda: FloatRaising(17.0, TypeError("caller-value=17.0")),
            TypeError,
            "caller-value=17.0",
        ),
        (
            lambda: FloatRaising(17.0, ValueError("caller-value=17.0")),
            ValueError,
            "caller-value=17.0",
        ),
        (lambda: IntRaising(17, TypeError("caller-value=17")), TypeError, "caller-value=17"),
        # No `__float__` here: `float()` itself raises, and its text is CPython's.
        (ReadsAsFloat, TypeError, None),
    ],
    ids=["float TypeError", "float ValueError", "int TypeError", "class reads as float"],
)
def test_red_at_pretip_t145_an_error_of_a_subclass_conversion_is_not_a_refusal(
    make: Any, kind: type[Exception], text: str | None
) -> None:
    # Red at 556fdc7, where the rule signalled with TypeError and ValueError and its callers
    # caught those two classes: the reader raised FactsError and `choose` the fixed text.
    # The error that the conversion of a subclass instance raises is the caller's own. It
    # passes through the reader (as at feffff6) and through `choose`, unchanged.
    reader, chooser = both_errors(make())
    assert type(reader) is kind and type(chooser) is kind
    assert str(reader) == str(chooser)
    assert str(reader) != COST_TEXT and "placement.worker_cost" not in str(reader)
    if text is not None:
        assert str(reader) == text


class ClassRaising:
    """Not an int or a float: reading its `__class__`, as the type test does, raises."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    @property
    def __class__(self) -> type:  # type: ignore[override]
        raise self.error


class FloatGiving(float):
    """A float whose own conversion gives another value than the one it holds."""

    def __new__(cls, stored: float, given: Any) -> "FloatGiving":
        made = super().__new__(cls, stored)
        made.given = given
        return made

    def __float__(self) -> Any:
        return self.given


@pytest.mark.parametrize(
    "error",
    [
        TypeError("type-test-caller"),
        ValueError("type-test-caller"),
        OverflowError("type-test-overflow"),
    ],
    ids=["TypeError", "ValueError", "OverflowError"],
)
def test_pins_existing_t145_an_error_of_the_type_test_passes_through(error: Exception) -> None:
    # `pins_existing` at 0b15f72 (review 2, R2-S1). What the type test of the rule raises is
    # the caller's own, of whatever class: the rule's `except OverflowError` is around the
    # conversion alone, and its callers catch the rule's own error alone.
    reader, chooser = both_errors(ClassRaising(error))
    assert reader is error and chooser is error
    assert (type(reader), str(reader)) == (type(error), error.args[0])


@pytest.mark.parametrize(
    ("stored", "given"),
    [(3.0, -5.0), (1.0, math.inf), (1.0, -math.inf), (1.0, math.nan)],
    ids=["3.0 gives -5.0", "1.0 gives inf", "1.0 gives -inf", "1.0 gives nan"],
)
def test_pins_existing_t145_a_converted_float_out_of_range_is_refused(
    stored: float, given: float
) -> None:
    # `pins_existing` at 0b15f72 (review 2, R2-S2). The rule judges the float that the
    # conversion gives, not the value as given: here the stored value is in range.
    from answerback_bridge.placement.facts import FactsError

    reader, chooser = both_errors(FloatGiving(stored, given))
    assert type(reader) is FactsError
    assert str(reader) == "placement.worker_cost must be finite and not negative"
    assert (type(chooser), str(chooser)) == (ValueError, COST_TEXT)


@pytest.mark.parametrize(
    ("stored", "given"),
    [(-3.0, 0.25), (math.inf, 1.0), (-math.inf, 1.0), (math.nan, 1.0)],
    ids=["-3.0 gives 0.25", "inf gives 1.0", "-inf gives 1.0", "nan gives 1.0"],
)
def test_pins_existing_t145_a_converted_float_in_range_is_the_cost(
    stored: float, given: float
) -> None:
    # `pins_existing` at 0b15f72 (review 2, R2-S2). The stored value is one the rule would
    # refuse; the float of the conversion is accepted, and it is the cost: the reader
    # stores it, and `choose` gives the answer of the reader's facts of that float.
    from answerback_bridge.placement.choose import Choice, Placement, choose

    cost = FloatGiving(stored, given)
    assert float.__repr__(cost) == repr(stored)
    read = make_facts(worker_cost=cost)
    assert type(read.worker_cost) is float and repr(read.worker_cost) == repr(given)
    assert read == make_facts(worker_cost=given)
    # Just under the busy threshold, so that the one recent placement decides for node-a.
    readings = [
        reading("local", "darwin-idle"),
        loaded("node-a", "linux-a", "11.19"),
        loaded("node-b", "linux-b", "11.19"),
        reading("node-c", "linux-a"),
    ]
    recent = [Placement("node-a", NOW)]
    direct = choose(readings, direct_facts(cost), harness="codex", now=NOW, recent=recent)
    parsed = choose(readings, read, harness="codex", now=NOW, recent=recent)
    for part in fields(Choice):
        assert getattr(direct, part.name) == getattr(parsed, part.name), part.name
    assert repr(direct) == repr(parsed)
    assert "node-a" not in direct.ranked
    without = choose(readings, make_facts(worker_cost=0), harness="codex", now=NOW, recent=recent)
    assert "node-a" in without.ranked and repr(without) != repr(direct)


def test_pins_existing_t145_other_faults_of_a_conversion_pass_through() -> None:
    # `pins_existing` at 0b15f72 (review 2). A conversion that gives a text: `float()` itself
    # raises TypeError, with CPython's text. A conversion that raises KeyError: that error.
    reader, chooser = both_errors(FloatGiving(3.0, "not a float"))
    assert type(reader) is TypeError and type(chooser) is TypeError
    assert str(reader) == str(chooser) != COST_TEXT
    assert "placement.worker_cost" not in str(reader)

    error = KeyError("caller-key")
    reader, chooser = both_errors(FloatRaising(17.0, error))
    assert reader is error and chooser is error
    assert (type(reader), str(reader)) == (KeyError, "'caller-key'")


def skip_reason(choice: Any) -> str:
    """The reason node-a was skipped."""
    return next(row.reason for row in choice.skipped if row.machine == "node-a")
