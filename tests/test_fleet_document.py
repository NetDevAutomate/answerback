"""fleet: the whole document, its size rule and the check before publication."""

import json
import math
from typing import Any

import pytest

from answerback_bridge.fleet import cadence, document, readings, receipts
from answerback_bridge.placement.probe import parse_reading

GOOD = (
    "os=Linux\ncores=10\nload=1 2 3\nmemtotal_kb=1000\nmemavailable_kb=900\n"
    "largest_kb=100\nlargest_name=python\nprocs=12\nsuites=0\nprobe_end=1\n"
)
WALL = 1_800_000_000.0


def _inputs(**changes: Any) -> document.Inputs:
    local = parse_reading("local", GOOD, WALL)
    worker = parse_reading("worker-a", GOOD, WALL)
    memos: dict[str | None, readings.Memo] = {
        None: readings.record(readings.Memo(), local, WALL, 0, active=None, ceiling=None),
        "worker-a": readings.record(readings.Memo(), worker, WALL, 0, active=None, ceiling=None),
        "worker-b": readings.Memo(error="no link"),
    }
    base: dict[str, Any] = {
        "writer": {"version": "1.0", "instance_id": "i", "pid": 1},
        "cadence": cadence.Cadence("normal", 15.0, None),
        "paused_since": None,
        "taken_at": WALL,
        "node_order": ["worker-a", "worker-b"],
        "links": {"worker-a": "connected", "worker-b": "unreachable"},
        "memos": memos,
        "active": {None: 2, "worker-a": 0},
        "lanes": {None: 0, "worker-a": 0},
        "ceilings": {},
        "launches": (),
        "health": receipts.unreachable(None, error="service did not answer"),
        "receipts": receipts.view(None, stale=True, error="down", source="status+task_list"),
        "errors": [],
    }
    base.update(changes)
    return document.Inputs(**base)


def _receipt(n: int, title: str = "t") -> dict[str, Any]:
    r = receipts.receipt(
        {"id": f"id-{n}", "status": "uncertain", "target": "hc:t1", "created": float(n)}
    )
    assert r is not None
    r["title"] = title
    return r


def test_document_has_every_field_and_the_coordinator_first() -> None:
    doc = document.build(_inputs(), written_at=WALL + 1, now_mono=1)
    assert set(doc) == {
        "schema",
        "written_at",
        "writer",
        "health",
        "capacity",
        "machines",
        "receipts",
        "errors",
    }
    assert doc["schema"] == "answerback.fleet/1"
    assert doc["writer"] == {
        "version": "1.0",
        "instance_id": "i",
        "pid": 1,
        "mode": "normal",
        "interval_s": 15.0,
    }
    assert [m["name"] for m in doc["machines"]] == [None, "worker-a", "worker-b"]
    assert doc["machines"][0]["link"]["state"] == "self"
    assert set(doc["capacity"]) == {
        "taken_at",
        "max_age_s",
        "paused",
        "node_order",
        "ranked_for_heavy_work",
        "new_work",
        "new_suite",
        "recent_launches",
    }
    assert doc["capacity"]["new_work"]["machine"] == "worker-a"
    assert document.validate(document.encode(doc)) == []


def test_document_passes_the_readers_own_rules() -> None:
    data = document.encode(document.build(_inputs(), written_at=WALL, now_mono=1))
    doc = json.loads(data)
    # in/reader/fleet.js parseDocument: size, schema, finite written_at, machines list.
    assert len(data) <= 1_048_576
    assert doc["schema"] == "answerback.fleet/1"
    assert isinstance(doc["written_at"], int | float) and math.isfinite(doc["written_at"])
    assert isinstance(doc["machines"], list)


def test_a_worker_whose_link_is_not_connected_keeps_its_link_word() -> None:
    doc = document.build(_inputs(), written_at=WALL, now_mono=1)
    b = doc["machines"][2]
    assert b["link"]["state"] == "unreachable"
    assert b["reading"]["state"] == "unknown" and b["reading"]["error"] == "no link"


def test_paused_capacity_says_why_and_since_when() -> None:
    pause = cadence.Pause("pressure_critical", "kernel memory pressure level 4")
    doc = document.build(
        _inputs(cadence=cadence.Cadence("paused", 45.0, pause), paused_since=WALL - 10),
        written_at=WALL,
        now_mono=1,
    )
    assert doc["capacity"]["paused"] == {
        "reason": "pressure_critical",
        "since": WALL - 10,
        "text": "kernel memory pressure level 4",
    }
    assert doc["capacity"]["new_work"]["machine"] is None
    assert doc["writer"]["mode"] == "paused"


def test_size_rule_drops_recent_then_oldest_needs_person_and_counts_them() -> None:
    big = "é" * 120
    needs = [_receipt(n, big) for n in range(200)]
    for r in needs:
        r["recipient"]["text"] = "\u4e00" * 200
        r["request_key"] = "\u4e00" * 200
    view = {
        "source": "status+task_list",
        "read_at": WALL,
        "stale": False,
        "error": None,
        "counts": {"uncertain": 200, "blocked": 0, "total": 200},
        "needs_person": needs,
        "needs_person_complete": True,
        "needs_person_omitted": 0,
        "recent": needs[:8],
    }
    doc = document.build(_inputs(receipts=view), written_at=WALL, now_mono=1)
    assert len(json.dumps(doc, ensure_ascii=False).encode()) > document.LIMIT
    data = document.encode(doc)
    out = json.loads(data)
    assert len(data) <= document.LIMIT
    kept = out["receipts"]["needs_person"]
    dropped = 200 - len(kept)
    assert out["receipts"]["recent"] == [] and dropped > 0
    assert out["receipts"]["needs_person_omitted"] == dropped
    assert out["receipts"]["needs_person_complete"] is False
    # The oldest went first: the newest receipt is still there.
    assert kept[-1]["id"] == "id-199" and kept[0]["id"] == f"id-{dropped}"
    assert document.validate(data) == []


def test_size_rule_counts_what_it_drops_from_0_when_the_number_was_null() -> None:
    needs = [_receipt(n, "é" * 120) for n in range(200)]
    for r in needs:
        r["recipient"]["text"] = "\u4e00" * 200
        r["request_key"] = "\u4e00" * 200
    view = {
        "source": "status+task_list",
        "read_at": WALL,
        "stale": False,
        "error": None,
        "counts": None,
        "needs_person": needs,
        "needs_person_complete": False,
        "needs_person_omitted": None,
        "recent": [],
    }
    doc = document.build(_inputs(receipts=view), written_at=WALL, now_mono=1)
    out = json.loads(document.encode(doc))
    dropped = 200 - len(out["receipts"]["needs_person"])
    assert dropped > 0
    assert out["receipts"]["needs_person_omitted"] == dropped
    assert out["receipts"]["needs_person_complete"] is False


@pytest.mark.parametrize(
    "breakage",
    [
        lambda d: d.update(schema="answerback.fleet/2"),
        lambda d: d.update(written_at=None),
        lambda d: d.update(machines={}),
        lambda d: d["machines"].reverse(),
        lambda d: d["machines"][1]["reading"].update(state="fine"),
        lambda d: d["machines"][1]["reading"].update(was="unknown"),
        lambda d: d["health"]["service"].update(state="up"),
        lambda d: d["writer"].update(mode="calm"),
        lambda d: d["machines"][0].update(role="worker"),
    ],
)
def test_check_before_publication_refuses_a_broken_document(breakage: Any) -> None:
    doc = document.build(_inputs(), written_at=WALL, now_mono=1)
    breakage(doc)
    assert document.validate(json.dumps(doc).encode()) != []


def test_check_refuses_text_that_is_not_json_or_has_a_non_finite_time() -> None:
    assert document.validate(b"{") != []
    assert document.validate(b'{"schema": "answerback.fleet/1", "written_at": NaN}') != []


def test_changed_ignores_written_at() -> None:
    a = document.build(_inputs(), written_at=WALL, now_mono=1)
    b = document.build(_inputs(), written_at=WALL + 9, now_mono=1)
    assert document.content_key(a) == document.content_key(b)
    c = document.build(_inputs(node_order=["worker-b", "worker-a"]), written_at=WALL, now_mono=1)
    assert document.content_key(a) != document.content_key(c)


def test_errors_are_at_most_20_newest_last() -> None:
    errors = [{"at": WALL + n, "where": "probe", "text": f"e{n}"} for n in range(30)]
    doc = document.build(_inputs(errors=errors), written_at=WALL, now_mono=1)
    assert [e["text"] for e in doc["errors"]] == [f"e{n}" for n in range(10, 30)]


def test_document_stays_under_256_kib_with_16_hostile_machines() -> None:
    hostile = "\x1b" + "\u4e00" * 999
    names = [f"worker-{n}" for n in range(15)]
    memos: dict[str | None, readings.Memo] = {None: readings.Memo(error=hostile)}
    memos.update({n: readings.Memo(error=hostile) for n in names})
    errors = [{"at": WALL, "where": hostile, "text": hostile}] * 20
    doc = document.build(
        _inputs(
            memos=memos,
            node_order=names,
            links={n: hostile for n in names},
            errors=errors,
        ),
        written_at=WALL,
        now_mono=1,
    )
    data = document.encode(doc)
    assert len(data) <= document.LIMIT
    assert "\x1b" not in data.decode()
    assert document.validate(data) == []


def test_the_check_accepts_the_mode_unmeasured() -> None:
    doc = document.build(
        _inputs(cadence=cadence.Cadence("unmeasured", 45.0, None)), written_at=WALL, now_mono=1
    )
    assert document.validate(document.encode(doc)) == []
