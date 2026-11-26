import json
from pathlib import Path

import pytest

from codex_herdr_bridge.bus.model import Command
from codex_herdr_bridge.bus.store import BusStore

PEER = "desktop:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DIGEST = "a" * 64


def submit(store: BusStore, key: str = "request-1", **changes: object) -> dict:
    values = {
        "request_id": key,
        "peer_id": PEER,
        "project": "/work/project",
        "routing_digest": DIGEST,
        "message": "Review the change",
        "ttl_seconds": 60,
    }
    return store.submit(**(values | changes))


def test_command_and_outboxes_are_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "bus.sqlite"
    first = BusStore(path, clock=lambda: 100)
    receipt = submit(first)
    assert receipt["delivery"] == "queued"
    assert receipt["task_id"] is None
    assert receipt["command_id"] != "request-1"
    reopened = BusStore(path, clock=lambda: 110)
    assert submit(reopened) == receipt
    assert len(reopened.pending_outbox()) == 2
    assert reopened.events()["events"][0]["type"] == "command.accepted"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="different"):
        submit(reopened, message="Different work")


def test_failed_submission_leaves_no_partial_command(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    with pytest.raises(ValueError):
        submit(store, project="relative")
    assert store.counts() == {}
    assert store.pending_outbox() == []
    assert store.events()["events"] == []


def test_claim_bind_and_event_projection_do_not_redispatch(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    receipt = submit(store)
    ident = receipt["command_id"]
    assert store.claim(ident)
    assert not store.claim(ident)
    task = {
        "id": "task-1",
        "status": "queued",
        "result": None,
        "detail": None,
        "updated": 100,
        "turn_id": None,
    }
    store.bind(ident, task)
    assert store.get(ident)["task_id"] == "task-1"
    assert not store.claim(ident)
    assert not store.project_task(ident, task)
    task.update(status="completed", result="PRIVATE_RESULT", updated=120)
    assert store.project_task(ident, task)
    assert not store.project_task(ident, task)
    events = store.events()["events"]
    assert [e["type"] for e in events] == [
        "command.accepted",
        "command.dispatching",
        "task.queued",
        "task.completed",
    ]
    assert "PRIVATE_RESULT" not in json.dumps(events)
    assert events[-1]["payload"]["result_ref"] == "task:task-1"
    assert store.get(ident)["delivery"] == "bound"


def test_cancel_and_expiry_prevent_dispatch(tmp_path: Path) -> None:
    now = [100.0]
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: now[0])
    cancelled = submit(store)["command_id"]
    assert store.cancel(cancelled)["delivery"] == "cancelled"
    assert not store.claim(cancelled)
    expired = submit(store, "expiring", ttl_seconds=5)["command_id"]
    now[0] = 105
    assert not store.claim(expired)
    assert store.get(expired)["delivery"] == "expired"
    active = submit(store, "active")["command_id"]
    assert store.claim(active)
    with pytest.raises(ValueError, match="already"):
        store.cancel(active)


def test_uncertain_claim_never_becomes_queued_automatically(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    ident = submit(store)["command_id"]
    store.claim(ident)
    store.finish(ident, "uncertain", "Admission not verified")
    assert not store.claim(ident)
    assert store.get(ident)["delivery"] == "uncertain"


def test_capacity_and_event_paging_are_bounded(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100, max_commands=2)
    submit(store)
    submit(store, "second")
    with pytest.raises(ValueError, match="capacity"):
        submit(store, "third")
    page = store.events(limit=1)
    assert len(page["events"]) == 1
    second = store.events(after=page["next_cursor"], limit=1)
    assert len(second["events"]) == 1
    assert second["events"][0]["id"] != page["events"][0]["id"]
    with pytest.raises(ValueError):
        store.events(limit=1001)


def test_wire_payload_must_match_admitted_command(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    ident = submit(store)["command_id"]
    admitted = store.command(ident)
    assert Command.from_bytes(admitted.to_bytes()) == admitted
    altered = json.loads(admitted.to_bytes())
    altered["message"] = "Injected"
    with pytest.raises(ValueError, match="match"):
        store.verify_payload(Command.from_bytes(json.dumps(altered).encode()))
    with pytest.raises(ValueError, match="duplicate"):
        Command.from_bytes(b'{"schema_version":1,"schema_version":1}')


def test_unrepresentable_timestamp_is_an_invalid_envelope(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    row = submit(store)
    value = json.loads(store.command(row["command_id"]).to_bytes())
    value["created_at"] = 10**400
    with pytest.raises(ValueError, match="Invalid"):
        Command.from_bytes(json.dumps(value).encode())


@pytest.mark.parametrize(
    "changes",
    [
        {"peer_id": "fuzzy name"},
        {"routing_digest": "not-a-digest"},
        {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
        {"message": "x" * 100001},
        {"message": ""},
        {"request_id": "bad key"},
    ],
)
def test_invalid_contract_is_rejected(tmp_path: Path, changes: dict) -> None:
    store = BusStore(tmp_path / "bus.sqlite", clock=lambda: 100)
    with pytest.raises(ValueError):
        submit(store, **changes)


def test_outbox_quarantine_is_reported_after_coordinator_restart(tmp_path: Path) -> None:
    """red_at_base: quarantine metadata remains visible after the flush error clears."""
    from types import SimpleNamespace

    from answerback_bridge.bus.coordinator import Coordinator

    store = BusStore(tmp_path / "ledger.sqlite", clock=lambda: 120)
    submit(store)
    row = store.pending_outbox(kind="event")[0]
    for _ in range(20):
        store.record_outbox_failure(row["id"], "APIError")
    reopened = BusStore(store.path)
    coordinator = Coordinator(reopened, SimpleNamespace())
    assert coordinator.state["outbox_quarantined"] == {"command": 0, "event": 1}
    with pytest.raises(ValueError, match="Invalid outbox page size"):
        reopened.quarantined_outbox(limit=0)
    assert len(reopened.quarantined_outbox(limit=1)) == 1
