"""fleet: the check before publication covers every declared field, from one table."""

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
from test_fleet_document import WALL, _inputs

from answerback_bridge.fleet import advice, cadence, document, receipts

STATUS: dict[str, Any] = {
    "running": True,
    "accepting": True,
    "version": "1.0",
    "instance_id": "svc",
    "started": 5.0,
    "tasks": {"uncertain": 1, "completed": 2},
    "bus": {"connected": True, "last_error": None},
    "clients": {"clients": [{"client": "c", "available": True, "live_instances": 1}]},
}
TASKS = [
    {
        "id": "r-1",
        "request_key": "k1",
        "target": "hc:worker-a/t1",
        "status": "uncertain",
        "created": 1.0,
        "updated": 2.0,
    },
    {
        "id": "r-2",
        "request_key": "k2",
        "target": "fixtureprov:x",
        "status": "completed",
        "created": 3.0,
        "updated": 4.0,
    },
]


def _full() -> dict[str, Any]:
    """A document with every section filled: service up, a pause, a launch, receipts."""
    good = receipts.good("status+task_list", WALL, STATUS, TASKS)
    launch = advice.Launch("worker-a", "worker-a/t9", WALL, 0.5)
    pause = cadence.Pause("pressure_critical", "kernel memory pressure level 4")
    return document.build(
        _inputs(
            cadence=cadence.Cadence("paused", 45.0, pause),
            paused_since=WALL,
            launches=(launch,),
            health=receipts.health(STATUS, read_at=WALL),
            receipts=receipts.view(good, stale=False, error=None, source="status+task_list"),
            errors=[{"at": WALL, "where": "hc", "text": "x"}],
        ),
        written_at=WALL,
        now_mono=1,
    )


def _check(doc: dict[str, Any]) -> list[str]:
    return document.validate(json.dumps(doc).encode())


@pytest.mark.parametrize(
    "variant",
    [
        lambda: _full(),
        lambda: document.build(_inputs(), written_at=WALL, now_mono=1),
        lambda: document.build(
            _inputs(cadence=cadence.Cadence("unmeasured", 45.0, None)), written_at=WALL, now_mono=1
        ),
        lambda: document.build(_inputs(), written_at=WALL, now_mono=500),  # all expired
    ],
)
def test_a_complete_document_from_the_builder_has_no_problem(variant: Callable[[], Any]) -> None:
    assert _check(variant()) == []


def _reading(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["machines"][1]["reading"]


# (what is changed, the change) — one field at a time to a wrong type, as section 8 lists,
# then one field of every declared section.
CHANGES: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("agents to a list", lambda d: _reading(d).update(agents=[1])),
    ("loads to a text", lambda d: _reading(d).update(loads="1 2 3")),
    ("suites to an object", lambda d: _reading(d).update(suites={})),
    ("harness_processes to a list", lambda d: _reading(d).update(harness_processes=[])),
    ("cores to a text", lambda d: _reading(d).update(cores="10")),
    (
        "a receipt's created to a text",
        lambda d: d["receipts"]["needs_person"][0].update(created="yesterday"),
    ),
    (
        "a recent receipt's updated to a text",
        lambda d: d["receipts"]["recent"][0].update(updated="today"),
    ),
    ("agents.active to a text", lambda d: _reading(d)["agents"].update(active="4")),
    ("agents.lanes to a number with a fraction", lambda d: _reading(d)["agents"].update(lanes=1.5)),
    ("memory.text to a number", lambda d: _reading(d)["memory"].update(text=3)),
    ("a load to a text", lambda d: _reading(d).update(loads=[1.0, "2", 3.0])),
    ("load_per_core to a bool", lambda d: _reading(d).update(load_per_core=True)),
    ("taken_at to a text", lambda d: _reading(d).update(taken_at="now")),
    ("link.since_ms to a text", lambda d: d["machines"][1]["link"].update(since_ms="0")),
    ("os to a number", lambda d: d["machines"][1].update(os=1)),
    ("writer.pid to a text", lambda d: d["writer"].update(pid="1")),
    ("writer.interval_s to null", lambda d: d["writer"].update(interval_s=None)),
    ("health.stale to a text", lambda d: d["health"].update(stale="no")),
    ("health.service.started to a text", lambda d: d["health"]["service"].update(started="x")),
    ("health.bus.connected to a number", lambda d: d["health"]["bus"].update(connected=1)),
    (
        "a client's live_instances to a text",
        lambda d: d["health"]["clients"][0].update(live_instances="1"),
    ),
    ("capacity.max_age_s to a text", lambda d: d["capacity"].update(max_age_s="60")),
    ("capacity.paused.since to a text", lambda d: d["capacity"]["paused"].update(since="x")),
    ("capacity.node_order to a text", lambda d: d["capacity"].update(node_order="worker-a")),
    ("a ranked machine to a number", lambda d: d["capacity"].update(ranked_for_heavy_work=[1])),
    ("new_work.why to null", lambda d: d["capacity"]["new_work"].update(why=None)),
    ("a launch's count to a text", lambda d: d["capacity"]["recent_launches"][0].update(count="1")),
    ("receipts.counts to a list", lambda d: d["receipts"].update(counts=[1])),
    ("a count to a text", lambda d: d["receipts"]["counts"].update(uncertain="1")),
    (
        "needs_person_complete to a text",
        lambda d: d["receipts"].update(needs_person_complete="yes"),
    ),
    ("a recipient to a text", lambda d: d["receipts"]["needs_person"][0].update(recipient="x")),
    (
        "a receipt's cause to an unknown word",
        lambda d: d["receipts"]["needs_person"][0].update(cause="it broke"),
    ),
    ("an error's text to a number", lambda d: d["errors"][0].update(text=5)),
    ("a missing field: suites", lambda d: _reading(d).pop("suites")),
    ("a missing field: receipts.recent", lambda d: d["receipts"].pop("recent")),
]


@pytest.mark.parametrize(("what", "change"), CHANGES, ids=[what for what, _ in CHANGES])
def test_one_field_of_a_wrong_type_is_a_problem(
    what: str, change: Callable[[dict[str, Any]], None]
) -> None:
    doc = copy.deepcopy(_full())
    change(doc)
    problems = _check(doc)
    assert problems != [], what


def test_the_check_names_the_field_that_is_wrong() -> None:
    doc = _full()
    _reading(doc).update(suites={})
    assert any("machines[1].reading.suites" in problem for problem in _check(doc))


@pytest.mark.parametrize("value", [None, [], "x", 1, {"machines": [None, 1, "x"]}, [[[]]]])
def test_the_check_never_raises(value: Any) -> None:
    doc = _full()
    for key in list(doc):
        changed = dict(doc)
        changed[key] = value
        assert isinstance(_check(changed), list)


def test_the_check_uses_one_table_of_the_declared_fields() -> None:
    table = document.FIELDS
    assert set(table) >= {"schema", "written_at", "writer", "health", "capacity", "machines"}
    assert set(table) >= {"receipts", "errors"}
