import os
import sqlite3
from pathlib import Path
from stat import S_IMODE

import pytest

from codex_herdr_bridge.client_mailbox import ClientMailbox
from codex_herdr_bridge.process import BridgeError
from codex_herdr_bridge.tasks import TaskStore


def test_retry_returns_original_receipt_and_never_submits_twice(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    first, created = store.reserve("desktop:reviewer", "Review this", "review-1")
    assert created
    store.update(first["id"], "queued")
    retry, created = store.reserve("desktop:reviewer", "Review this", "review-1")
    assert not created
    assert retry["id"] == first["id"]
    assert retry["status"] == "queued"


def test_permission_receipts_keep_the_newest_128_events_and_count_dropped_events(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:reviewer", "Review", "permission-event-bound", {"kind": "kiro"})

    for ordinal in range(1, 131):
        store.record_permission_round(
            task["id"],
            permission_id=f"permission-{ordinal}",
            tool_call_id=f"call-{ordinal}",
            option_id="deny",
            requested_at=float(ordinal),
            answered_at=float(ordinal) + 0.5,
            ordinal=ordinal,
        )

    metadata = store.get(task["id"])["metadata"]
    assert [event["ordinal"] for event in metadata["permission_events"]] == list(range(3, 131))
    assert metadata["permission_events_truncated"] == 2


def test_permission_receipts_truncate_a_long_tool_call_id(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:reviewer", "Review", "long-tool-call-id", {"kind": "kiro"})

    store.record_permission_round(
        task["id"],
        permission_id="permission-1",
        tool_call_id="x" * 600,
        option_id="deny",
        requested_at=1.0,
        answered_at=2.0,
        ordinal=1,
    )

    tool_call_id = store.get(task["id"])["metadata"]["permission_events"][0]["toolCallId"]
    assert tool_call_id == "x" * 511 + "…"
    assert len(tool_call_id) == 512


def test_permission_receipts_at_the_event_cap_do_not_claim_truncation(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:reviewer", "Review", "event-cap-without-drop", {"kind": "kiro"})

    for ordinal in range(1, 129):
        store.record_permission_round(
            task["id"],
            permission_id=f"permission-{ordinal}",
            tool_call_id=f"call-{ordinal}",
            option_id="deny",
            requested_at=float(ordinal),
            answered_at=float(ordinal) + 0.5,
            ordinal=ordinal,
        )

    metadata = store.get(task["id"])["metadata"]
    assert len(metadata["permission_events"]) == 128
    assert "permission_events_truncated" not in metadata


def test_same_request_id_cannot_silently_change_work(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    store.reserve("desktop:reviewer", "Review this", "review-1")
    with pytest.raises(ValueError, match="different"):
        store.reserve("desktop:reviewer", "Delete this", "review-1")


def test_uncertain_delivery_blocks_new_work_until_investigated(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("desktop:reviewer", "Review", "review-1")
    store.update(task["id"], "uncertain")
    with pytest.raises(ValueError, match="already has task"):
        TaskStore(store.path).reserve("desktop:reviewer", "Second review", "review-2")
    retry, created = store.reserve("desktop:reviewer", "Review", "review-1")
    assert not created
    assert retry["status"] == "uncertain"


def test_finished_receipt_cannot_be_downgraded_by_late_acknowledgement(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("herdr:reviewer", "Review", "review-1")
    store.update(task["id"], "completed", result="Done")
    store.update(task["id"], "queued")
    assert store.get(task["id"])["result"] == "Done"
    assert store.get(task["id"])["status"] == "completed"


def test_legacy_worker_session_schema_migrates_without_losing_binding(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE worker_sessions "
            "(alias TEXT PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT NOT NULL)"
        )
        db.execute("INSERT INTO worker_sessions VALUES ('reviewer', 'old-session', '/project')")
    store = TaskStore(path)
    legacy = store.worker_session("reviewer")
    assert legacy["session_id"] == "old-session"
    assert legacy["profile"] is None
    store.save_worker_session("reviewer", "scoped-session", "/project", profile="herdr_bridge")
    reopened = TaskStore(path)
    assert reopened.worker_session("reviewer") == {
        "alias": "reviewer",
        "session_id": "scoped-session",
        "cwd": "/project",
        "profile": "herdr_bridge",
    }


def test_task_store_owns_idempotent_crew_creation_receipt_schema(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"

    TaskStore(path)
    TaskStore(path)

    with sqlite3.connect(path) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(crew_creation_receipts)").fetchall()
        }
    assert columns == {"request_key", "title", "project", "identity"}


@pytest.mark.parametrize("target_exists", [False, True])
def test_task_store_refuses_database_symlinks(tmp_path: Path, target_exists: bool) -> None:
    """red_at_base: reject both existing and dangling receipt database symlinks."""
    directory = tmp_path / "state"
    directory.mkdir()
    target = tmp_path / "target.sqlite"
    if target_exists:
        target.touch()
        target.chmod(0o640)
    path = directory / "tasks.sqlite"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="Task database must not be a symlink"):
        TaskStore(path)

    assert path.is_symlink()
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_bytes() == b""
        assert S_IMODE(target.stat().st_mode) == 0o640


def test_task_store_resecures_existing_directory_on_restart(tmp_path: Path) -> None:
    """red_at_base: preserve receipts while tightening the owned state directory."""
    path = tmp_path / "state" / "tasks.sqlite"
    original = TaskStore(path)
    receipt, _ = original.reserve("hc:t1", "Review", "review")
    path.parent.chmod(0o777)

    reopened = TaskStore(path)

    assert S_IMODE(path.parent.stat().st_mode) == 0o700
    assert S_IMODE(path.stat().st_mode) == 0o600
    assert reopened.get(receipt["id"]) == receipt


@pytest.mark.parametrize("store_type", [TaskStore, ClientMailbox])
def test_local_store_refuses_unowned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_type: type[TaskStore | ClientMailbox]
) -> None:
    """red_at_base: simulate foreign ownership without changing any real owner."""
    directory = tmp_path / "state"
    directory.mkdir()
    directory.chmod(0o777)
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path == directory:
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(BridgeError, match="directory owned by the current user"):
        store_type(directory / "store.sqlite")
    assert list(directory.iterdir()) == []
    assert S_IMODE(directory.stat().st_mode) == 0o777


@pytest.mark.parametrize("store_type", [TaskStore, ClientMailbox])
def test_local_store_refuses_symlink_parent(
    tmp_path: Path, store_type: type[TaskStore | ClientMailbox]
) -> None:
    """red_at_base: refuse an unresolved symlink parent before writing its target."""
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o777)
    directory = tmp_path / "state"
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(BridgeError, match="directory owned by the current user"):
        store_type(directory / "store.sqlite")
    assert list(target.iterdir()) == []
    assert S_IMODE(target.stat().st_mode) == 0o777


# T-187: a verified send that landed in the agent's own turn.


def test_typed_while_working_receipt_completes_on_the_workers_report(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:default:t1", "Review", "race-1", {"kind": "hc"})
    receipt = {"target": "t1", "turn": 4, "started": False, "status": "working"}
    saved = store.confirm_core_typed_while_working(task["id"], receipt, turn_before=3, turn_after=4)
    assert saved["status"] == "uncertain"
    assert saved["result"] is None
    assert "turn 3 before the send, 4 after" in saved["detail"]
    assert saved["metadata"]["delivery"] == receipt
    assert saved["metadata"]["delivery_verified"] is True
    assert saved["metadata"]["typed_while_working"] is True
    assert (saved["metadata"]["turn_before"], saved["metadata"]["turn_after"]) == (3, 4)
    with pytest.raises(ValueError, match="already has task"):
        store.reserve("hc:default:t1", "Next", "race-2", {"kind": "hc"})
    done = store.complete_worker(task["id"], "Reviewed")
    assert done["status"] == "completed" and done["result"] == "Reviewed"
    assert "provisional_result" not in done["metadata"]
    again, created = store.reserve("hc:default:t1", "Next", "race-2", {"kind": "hc"})
    assert created and again["id"] != task["id"]


def test_typed_while_working_receipt_without_turns_names_the_gap(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:default:t1", "Review", "race-1", {"kind": "hc"})
    saved = store.confirm_core_typed_while_working(
        task["id"],
        {"turn": 4, "started": True, "status": "blocked"},
        turn_before=None,
        turn_after=4,
    )
    assert saved["status"] == "uncertain"
    assert "turn counter not reported" in saved["detail"]
    assert "'blocked'" in saved["detail"]
    assert saved["metadata"]["turn_before"] is None


def test_typed_while_working_publishes_an_earlier_provisional_result(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:default:t1", "Review", "race-1", {"kind": "hc"})
    early = store.complete_worker(task["id"], "Early")
    assert early["status"] == "pending" and early["metadata"]["provisional_result"] == "Early"
    saved = store.confirm_core_typed_while_working(
        task["id"], {"turn": 4, "started": True, "status": "working"}, turn_before=3, turn_after=4
    )
    assert saved["status"] == "completed" and saved["result"] == "Early"
    assert saved["metadata"]["typed_while_working"] is True


def test_typed_while_working_is_refused_after_identity_invalidation(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:default:t1", "Review", "race-1", {"kind": "hc"})
    store.invalidate_submission(task["id"], "Core terminal identity changed after send")
    with pytest.raises(ValueError, match="invalidated"):
        store.confirm_core_typed_while_working(
            task["id"],
            {"turn": 4, "started": False, "status": "working"},
            turn_before=3,
            turn_after=4,
        )
    with pytest.raises(ValueError, match="invalidated"):
        store.complete_worker(task["id"], "Reviewed")
    assert store.get(task["id"])["status"] == "uncertain"
