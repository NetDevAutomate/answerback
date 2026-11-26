"""fleet: receipts and health from the service; a failure never reads as nothing to do."""

from typing import Any

import pytest

from answerback_bridge.fleet import receipts

STATUS: dict[str, Any] = {
    "version": "0.13.0+internal",
    "running": True,
    "accepting": True,
    "instance_id": "abc",
    "started": 1000.5,
    "tasks": {"completed": 90, "failed": 6, "uncertain": 1, "blocked": 2},
    "bus": {"connected": True, "last_error": None},
    "clients": {
        "clients": [
            {"client": "claude", "available": True, "live_instances": 2, "last_heartbeat_age": 2.1}
        ],
        "heartbeat_interval": 10,
    },
}


def _task(n: int, status: str, target: str = "hc:t8", **more: Any) -> dict[str, Any]:
    return {
        "id": f"id-{n}",
        "request_key": f"ab-20261120-key-{n}",
        "target": target,
        "status": status,
        "created": 1000.0 + n,
        "updated": 1001.0 + n,
        "detail": "free text that says peer_not_running",
        **more,
    }


def test_receipt_maps_store_fields_and_never_derives_a_cause_from_free_text() -> None:
    r = receipts.receipt(_task(1, "failed", "hc:worker-a/t21"))
    assert r == {
        "id": "id-1",
        "state": "failed",
        "title": None,
        "recipient": {
            "provider": "hc",
            "machine": "worker-a",
            "terminal": "t21",
            "text": "hc:worker-a/t21",
        },
        "request_key": "ab-20261120-key-1",
        "reason": None,
        "cause": None,
        "identity_guard": None,
        "identity_observer": None,
        "created": 1001.0,
        "updated": 1002.0,
    }
    assert receipts.receipt(_task(2, "failed", cause="peer_not_running"))["cause"] == (
        "peer_not_running"
    )
    assert receipts.receipt(_task(3, "failed", cause="peer not running"))["cause"] is None


def _sent(observer: str | None = None, **metadata: Any) -> dict[str, Any]:
    delivery: dict[str, Any] = {"identity_guard": "observed_before_and_after"}
    if observer is not None:
        delivery["identity_observer"] = observer
    return {"metadata": {"delivery": delivery, **metadata}}


def test_receipt_identity_observer_needs_an_observed_delivery() -> None:
    """T-226 item 3 and review F2: `identity read by` only after a verified send."""
    local = _task(1, "submitted", "hc:default:t84", **_sent("core:local"))
    assert receipts.receipt(local)["identity_observer"] == "core:local"
    assert receipts.receipt(local)["identity_guard"] == "observed_before_and_after"
    remote = _task(1, "completed", "hc:default:nuc01/t4", **_sent("core:nuc01"))
    assert receipts.receipt(remote)["identity_observer"] == "core:nuc01"
    # A delivery recorded before T-226 has the guard but no observer field.
    assert receipts.receipt(_task(1, "completed", "hc:t84", **_sent()))["identity_observer"] == (
        "core:local"
    )
    for target in ("fixtureprov:ui-0e15d650", "hc:../t1;rm", "crew:slot", 7):
        assert receipts.receipt(_task(1, "failed", target, **_sent()))["identity_observer"] is None


@pytest.mark.parametrize(
    "more",
    [
        {},  # a fleet_snapshot row: no metadata at all
        {"metadata": {}},  # failed before `hc send` (delivery_not_attempted)
        {"metadata": {"delivery": {"identity_guard": "other"}}},
        {"metadata": {"delivery": "observed_before_and_after"}},
        _sent("core:nuc01", identity_invalidated=True),
        _sent("core:example-host"),  # disagrees with the target's machine
    ],
)
def test_receipt_without_an_observed_delivery_names_no_observer(more: dict[str, Any]) -> None:
    item = _task(1, "failed", "hc:default:nuc01/t4", reason="delivery_not_attempted", **more)
    assert receipts.receipt(item)["identity_observer"] is None
    assert receipts.receipt(item)["identity_guard"] is None


def test_receipt_machine_and_terminal_come_from_a_core_peer_id_only() -> None:
    plugin = receipts.receipt(_task(1, "failed", "fixtureprov:ui-0e15d650"))
    assert plugin["recipient"]["machine"] is None and plugin["recipient"]["terminal"] is None
    assert plugin["recipient"]["provider"] == "fixtureprov"
    local = receipts.receipt(_task(1, "completed", "hc:default:t84"))
    assert (local["recipient"]["machine"], local["recipient"]["terminal"]) == (None, "t84")
    bad = receipts.receipt(_task(1, "completed", "hc:../t1;rm"))
    assert bad["recipient"]["terminal"] is None


def test_needs_person_complete_when_found_equals_counts_else_omitted_is_the_difference() -> None:
    tasks = [_task(1, "uncertain"), _task(2, "blocked"), _task(3, "blocked"), _task(4, "failed")]
    good = receipts.good("status+task_list", 2000, STATUS, tasks)
    view = receipts.view(good, stale=False, error=None, source="status+task_list")
    assert len(view["needs_person"]) == 3
    assert view["needs_person_complete"] is True and view["needs_person_omitted"] == 0
    short = receipts.good("status+task_list", 2000, STATUS, tasks[1:])
    view = receipts.view(short, stale=False, error=None, source="status+task_list")
    assert view["needs_person_complete"] is False and view["needs_person_omitted"] == 1
    assert view["counts"]["total"] == 99 and view["counts"]["pending"] == 0


def test_a_good_read_without_counts_is_not_complete_and_omits_an_unknown_number() -> None:
    no_counts = {key: value for key, value in STATUS.items() if key != "tasks"}
    good = receipts.good("status+task_list", 2000, no_counts, [_task(1, "uncertain")])
    assert good.counts is None
    view = receipts.view(good, stale=False, error=None, source="status+task_list")
    assert len(view["needs_person"]) == 1
    assert view["needs_person_complete"] is False
    assert view["needs_person_omitted"] is None


def test_of_201_waiting_one_without_a_creation_time_is_kept_as_the_newest() -> None:
    tasks = [_task(n, "blocked") for n in range(200, 0, -1)]  # newest first
    tasks.append(_task(0, "uncertain", created="not a time"))
    good = receipts.good("status+task_list", 2000, STATUS, tasks)
    listed = [r["id"] for r in good.needs_person]
    assert len(listed) == 200
    assert listed[-1] == "id-0" and good.needs_person[-1]["created"] is None
    assert "id-1" not in listed  # the oldest with a creation time
    assert listed[:-1] == [f"id-{n}" for n in range(2, 201)]


def test_recent_is_the_8_newest_receipts() -> None:
    tasks = [_task(n, "completed") for n in range(20, 0, -1)]
    view = receipts.view(
        receipts.good("status+task_list", 2000, STATUS, tasks),
        stale=False,
        error=None,
        source="status+task_list",
    )
    assert [r["id"] for r in view["recent"]] == [f"id-{n}" for n in range(20, 12, -1)]


def test_service_down_keeps_last_good_receipts_stale_with_error_and_old_read_at() -> None:
    good = receipts.good("status+task_list", 2000, STATUS, [_task(1, "uncertain")])
    view = receipts.view(good, stale=True, error="service did not answer", source="status")
    assert view["stale"] is True and view["error"] == "service did not answer"
    assert view["read_at"] == 2000 and len(view["needs_person"]) == 1
    assert view["counts"]["uncertain"] == 1


def test_service_down_before_any_good_read_counts_null_and_never_reads_as_none_waiting() -> None:
    view = receipts.view(None, stale=True, error="service did not answer", source="status")
    assert view["counts"] is None and view["needs_person"] == [] and view["recent"] == []
    assert view["needs_person_complete"] is False
    assert view["read_at"] is None and view["stale"] is True and view["error"]


def test_health_from_status_and_unreachable_without_it() -> None:
    h = receipts.health(STATUS, read_at=2000)
    assert h["service"] == {
        "state": "running",
        "accepting": True,
        "started": 1000.5,
        "version": "0.13.0+internal",
        "instance_id": "abc",
    }
    assert h["bus"] == {"connected": True, "last_error": None}
    assert h["clients"] == [
        {"client": "claude", "available": True, "live_instances": 2, "last_heartbeat_age_s": 2.1}
    ]
    assert receipts.health(STATUS | {"accepting": False}, read_at=1)["service"]["state"] == (
        "stopping"
    )
    down = receipts.unreachable(h, error="service did not answer")
    assert down["service"]["state"] == "unreachable" and down["stale"] is True
    assert down["read_at"] == 2000 and down["error"] == "service did not answer"
    first = receipts.unreachable(None, error="x")
    assert first["read_at"] is None and first["clients"] == []


def test_hostile_service_texts_are_cut_and_lose_control_characters() -> None:
    hostile = "\x1b[2J" + "x" * 5000
    r = receipts.receipt(_task(1, "failed", target="fixtureprov:" + hostile, request_key=hostile))
    assert len(r["recipient"]["text"]) <= 200 and "\x1b" not in r["recipient"]["text"]
    assert len(r["request_key"]) <= 200
    status = STATUS | {"version": hostile, "bus": {"connected": True, "last_error": hostile}}
    status["clients"] = {"clients": [{"client": hostile}] * 50}
    h = receipts.health(status, read_at=1)
    assert len(h["service"]["version"]) <= 64 and len(h["bus"]["last_error"]) <= 200
    assert len(h["clients"]) == 32 and len(h["clients"][0]["client"]) <= 64
    assert receipts.receipt({"id": "\x00"}) is None


def test_stopping_only_when_status_says_accepting_false() -> None:
    no_field = {k: v for k, v in STATUS.items() if k != "accepting"}
    assert receipts.health(no_field, read_at=1)["service"]["state"] == "running"
    assert receipts.health(no_field, read_at=1)["service"]["accepting"] is None
    assert receipts.health(STATUS | {"accepting": "no"}, read_at=1)["service"]["state"] == (
        "running"
    )
    assert receipts.health(STATUS | {"accepting": False}, read_at=1)["service"]["state"] == (
        "stopping"
    )


def test_a_terminal_id_of_64_characters_is_kept_and_of_65_gives_no_machine_and_no_terminal() -> (
    None
):
    at_64 = "t" + "a" * 63
    r = receipts.receipt(_task(1, "failed", f"hc:worker-a/{at_64}"))
    assert (r["recipient"]["machine"], r["recipient"]["terminal"]) == ("worker-a", at_64)
    at_65 = "t" + "a" * 64
    r = receipts.receipt(_task(1, "failed", f"hc:worker-a/{at_65}"))
    assert (r["recipient"]["machine"], r["recipient"]["terminal"]) == (None, None)
