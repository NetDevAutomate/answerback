"""Retention acceptance uses disposable real SQLite databases only."""

import json
import sqlite3
from pathlib import Path

import pytest

from answerback_bridge.bus.store import BusStore
from answerback_bridge.tasks import TaskStore


def test_preview_missing_stores_never_creates_files(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    config = tmp_path / "bridge.json"
    config.write_text("{}")
    before = {p: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.rglob("*") if p.is_file()}
    maintenance = Maintenance(config)
    assert all(store["status"] == "absent" for store in maintenance.stats()["stores"].values())
    assert maintenance.plan()["actions"] == []
    after = {p: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


def test_retention_schema_preserves_receipts_and_tracks_metadata_mutation(tmp_path: Path) -> None:
    from answerback_bridge.retention import migrate_tasks

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:test", "résumé\n", "one", {"kind": "kiro", "cwd": "/project"})
    store.update(task["id"], "completed", result="done")
    before = store.get(task["id"])
    with store.connection() as db:
        migrate_tasks(db)
        migration_age = db.execute(
            "SELECT changed_at FROM retention_records WHERE entity='tasks'"
        ).fetchone()[0]
    assert migration_age > 0
    assert store.get(task["id"])["message"] == before["message"]
    with store.connection() as db:
        db.execute("UPDATE retention_records SET changed_at=1")
    store.metadata(task["id"], {"note": "new evidence"})
    with store.connection() as db:
        row = db.execute(
            "SELECT updated,changed_at FROM tasks JOIN retention_records ON row_id=id"
        ).fetchone()
    assert row["updated"] == before["updated"]
    assert row["changed_at"] >= migration_age


def fixture_task(
    tmp_path: Path,
    *,
    key: str = "one",
    status: str = "completed",
    metadata: dict | None = None,
    subject: str | None = None,
) -> tuple[TaskStore, dict]:
    from answerback_bridge.retention import migrate_tasks

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve(
        "kiro:" + key,
        "private résumé\n",
        key,
        metadata or {"kind": "kiro", "cwd": "/project"},
        subject=subject,
    )
    task = store.update(
        task["id"], status, result="private done" if status == "completed" else None
    )
    with store.connection() as db:
        migrate_tasks(db)
        db.execute("UPDATE tasks SET created=1,updated=2 WHERE id=?", (task["id"],))
        db.execute("UPDATE retention_records SET changed_at=3 WHERE row_id=?", (task["id"],))
    return store, task


def test_plan_is_value_free_and_prunes_only_safe_terminal_history(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, completed = fixture_task(tmp_path)
    fixture_task(tmp_path, key="uncertain", status="uncertain")
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert [action["id"] for action in plan["actions"]] == [completed["id"]]
    assert "private done" not in json.dumps(plan)
    assert "private résumé" not in json.dumps(plan, ensure_ascii=False)
    assert store.get(completed["id"])["result"] == "private done"


def test_reviewed_plan_apply_and_exact_repeat(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert maintenance.apply(plan)["status"] == "applied"
    assert store.get(task["id"])["result"] is None
    assert store.get(task["id"])["status"] == "completed"
    assert maintenance.apply(plan)["status"] == "already_applied"
    assert maintenance.plan()["actions"] == []


def fixture_bus(tmp_path: Path) -> tuple[BusStore, TaskStore, str, str]:
    from answerback_bridge.bus.store import BusStore
    from answerback_bridge.retention import migrate_content, migrate_tasks

    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    command = bus.submit(
        request_id="cmd",
        peer_id="kiro:one",
        project="/project",
        routing_digest="a" * 64,
        message="work",
        ttl_seconds=60,
    )
    ident = command["command_id"]
    bus.claim(ident, "kiro:one")
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = tasks.reserve("kiro:one", "work", "bus:" + ident, {"kind": "kiro", "cwd": "/project"})
    tasks.update(task["id"], "completed", result="done")
    with tasks.connection() as db:
        migrate_tasks(db)
        db.execute("UPDATE tasks SET created=1,updated=2")
        db.execute("UPDATE retention_records SET changed_at=3")
    bus.bind(ident, tasks.get(task["id"]))
    with bus.connection() as db:
        migrate_content(db, "commands")
    for row in bus.pending_outbox():
        bus.mark_published(row["id"])
    return bus, tasks, ident, task["id"]


def test_cross_store_plan_deletes_only_published_events_and_prunes_spines(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert {a["table"] for a in plan["actions"]} == {"tasks", "commands", "events", "outbox"}
    assert maintenance.apply(plan)["status"] == "applied"
    assert bus.get(command)["task_id"] == task
    assert tasks.get(task)["status"] == "completed"
    assert bus.events()["events"] == []
    assert maintenance.apply(plan)["status"] == "already_applied"


def test_pending_publication_protects_whole_group(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        db.execute("UPDATE outbox SET published_at=NULL WHERE kind=?", ("event",))
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == []
    assert "pending_publication" in plan["protected"]
    assert tasks.get(task)["result"] == "done"


@pytest.mark.parametrize("mutation", ["metadata", "result", "new_reference", "schema"])
def test_stale_plan_refuses_all_stores(tmp_path: Path, mutation: str) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    if mutation == "metadata":
        tasks.metadata(task, {"note": "new evidence"})
    elif mutation == "result":
        with tasks.connection() as db:
            db.execute("UPDATE tasks SET result=? WHERE id=?", ("changed", task))
    elif mutation == "new_reference":
        other = bus.submit(
            request_id="other",
            peer_id="kiro:other",
            project="/project",
            routing_digest="a" * 64,
            message="other",
        )
        bus.finish(other["command_id"], "rejected", "busy", blocking_task_id=task)
    else:
        with tasks.connection() as db:
            db.execute("CREATE TABLE extra (value TEXT)")
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    with bus.connection() as db:
        assert db.execute("SELECT payload FROM commands WHERE id=?", (command,)).fetchone()[0]
    assert tasks.get(task)["message"] == "work"


def test_cross_store_failure_rolls_every_write_back(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with tasks.connection() as db:
        db.execute(
            "CREATE TRIGGER refuse_prune BEFORE UPDATE OF message ON tasks "
            "WHEN NEW.message='' BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        maintenance.apply(plan)
    with bus.connection() as db:
        assert db.execute("SELECT payload FROM commands WHERE id=?", (command,)).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
    assert tasks.get(task)["result"] == "done"


def test_stats_and_pages_are_bounded_value_free_and_utf8_accurate(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, RetentionSelection

    fixture_task(tmp_path)
    fixture_task(tmp_path, key="two")
    maintenance = Maintenance(tmp_path / "bridge.json")
    stats = maintenance.stats()
    assert stats["stores"]["tasks"]["tables"]["tasks"]["rows"] == 2
    assert stats["stores"]["tasks"]["logical_content_bytes"] == 2 * len(
        "private résumé\nprivate done".encode()
    )
    page = maintenance.plan(RetentionSelection(max_actions=1, max_roots=1))
    assert len(page["actions"]) == 1
    assert page["truncated"] is True
    assert page["continuation"]
    following = maintenance.plan(
        RetentionSelection(max_actions=1, max_roots=1), cursor=page["continuation"]
    )
    assert following["actions"][0]["id"] != page["actions"][0]["id"]
    assert "private done" not in json.dumps(stats)


def test_private_plan_roundtrip_and_tamper_rejection(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    path = tmp_path / "review.json"
    maintenance.write_plan(plan, path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert maintenance.read_plan(path) == plan
    changed = json.loads(path.read_text())
    changed["actions"][0]["id"] = "other"
    with pytest.raises(MaintenanceError):
        maintenance.apply(changed)


def test_compact_holds_real_owner_lock_and_preserves_identity(tmp_path: Path) -> None:
    import fcntl

    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, task = fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock = runtime / "owner.lock"
    lock.touch(mode=0o600)
    with lock.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MaintenanceError) as refused:
            maintenance.compact("tasks")
        assert refused.value.code == "service_running"
    before = store.get(task["id"])
    result = maintenance.compact("tasks")
    assert result["status"] == "compacted"
    assert result["after"]["file_bytes"] == store.path.stat().st_size
    assert store.get(task["id"]) == before


@pytest.mark.parametrize(
    "status", ["pending", "queued", "submitted", "running", "blocked", "uncertain", "unknown"]
)
def test_unfinished_and_unknown_task_states_are_unchanged(tmp_path: Path, status: str) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
        db.execute("UPDATE retention_records SET changed_at=3")
    before = store.path.read_bytes()
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []
    assert before == store.path.read_bytes()


@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": "fixtureprov", "cwd": "/project", "identity_invalidated": True},
        {"kind": "kiro", "cwd": "/project", "provisional_result": "unverified"},
        {"kind": "kiro", "cwd": "/project", "completion_received": True},
        {"kind": "unknown"},
        {},
    ],
)
def test_unknown_or_contradictory_metadata_is_protected(tmp_path: Path, metadata: dict) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE tasks SET metadata=? WHERE id=?", (json.dumps(metadata), task["id"]))
        db.execute("UPDATE retention_records SET changed_at=3")
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []


def test_reconcilable_desktop_failure_and_recent_metadata_are_protected(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE tasks SET status='failed',target='desktop:one',"
            "metadata=?,result=?,turn_id='turn'",
            ('{"kind":"desktop"}', "Codex turn interrupted"),
        )
        db.execute("UPDATE retention_records SET changed_at=3")
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []
    store.metadata(task["id"], {"recent": True})
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []


def test_legacy_positional_insert_survives_companion_migration(tmp_path: Path) -> None:
    from answerback_bridge.retention import migrate_content, migrate_tasks

    store = TaskStore(tmp_path / "tasks.sqlite")
    with store.connection() as db:
        migrate_tasks(db)
        db.execute(
            "INSERT INTO tasks VALUES ('legacy','legacy','herdr:old','old','pending',"
            "1,2,'{}',NULL,NULL,NULL)"
        )
    assert store.get("legacy")["message"] == "old"
    bus = BusStore(tmp_path / "bus" / "ledger.sqlite")
    with bus.connection() as db:
        migrate_content(db, "commands")
        db.execute("INSERT INTO outbox VALUES ('old','command','missing','{}',1,NULL)")
    assert bus.pending_outbox()[0]["id"] == "old"


@pytest.mark.parametrize(
    "selection",
    [
        {"older_than_days": 0},
        {"older_than_days": -1},
        {"older_than_days": True},
        {"older_than_days": 1.1},
        {"older_than_days": float("nan")},
        {"provider": "ki"},
        {"project": "relative"},
        {"max_actions": 1001},
    ],
)
def test_invalid_selection_is_rejected(selection: dict) -> None:
    from answerback_bridge.maintenance import RetentionSelection

    with pytest.raises(ValueError):
        RetentionSelection(**selection)


def test_exact_provider_project_filters_and_unknown_ownership(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, RetentionSelection

    store, task = fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert (
        len(maintenance.plan(RetentionSelection(provider="kiro", project="/project"))["actions"])
        == 1
    )
    assert maintenance.plan(RetentionSelection(project="/project-adjacent"))["actions"] == []
    with store.connection() as db:
        db.execute("UPDATE tasks SET target='desktop:one',metadata='{\"kind\":\"desktop\"}'")
        db.execute("UPDATE retention_records SET changed_at=3")
    assert len(maintenance.plan(RetentionSelection(provider="codex"))["actions"]) == 1
    unknown = maintenance.plan(RetentionSelection(provider="codex", project="/project"))
    assert unknown["actions"] == []
    assert unknown["protected"]["project_unknown"] == 1


def test_wal_multistore_apply_and_sqlite_lock_fail_without_changes(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json", lock_timeout=0.01)
    plan = maintenance.plan()
    with tasks.connection() as locked:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(MaintenanceError) as refused:
            maintenance.apply(plan)
        assert refused.value.code == "busy"
    with bus.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "unsupported_journal"
    assert tasks.get(task)["result"] == "done"


def test_file_replacement_and_duplicate_physical_paths_are_rejected(tmp_path: Path) -> None:
    import os
    import shutil

    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, task = fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    backup = tmp_path / "replacement"
    shutil.copy2(store.path, backup)
    backup.replace(store.path)
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    os.link(store.path, tmp_path / "client-mailbox.sqlite")
    with pytest.raises(MaintenanceError) as refused:
        maintenance.plan()
    assert refused.value.code == "duplicate_store"


def test_crew_creation_receipts_are_known_never_pruned_and_drift_fails_closed(
    tmp_path: Path,
) -> None:
    """T-190 re-check Y2: the Crew creation-receipt table is a known tasks-store table that
    retention never prunes (a receipt outlives its task so a later retry is refused, not
    adopted), and a column added to it protects the store like any other drift."""
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute(
            "INSERT INTO crew_creation_receipts(request_key, title, project, identity)"
            " VALUES (?,?,?,?)",
            ("k" * 64, "invented title", "/invented/project", "{}"),
        )
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["protected"].get("unsupported_schema", 0) == 0
    assert not any("crew_creation_receipts" in str(a) for a in plan["actions"])
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM crew_creation_receipts").fetchone()[0] == 1
        db.execute("ALTER TABLE crew_creation_receipts ADD COLUMN future_reference TEXT")
    drifted = Maintenance(tmp_path / "bridge.json").plan()
    assert drifted["actions"] == []
    assert drifted["protected"]["unsupported_schema"] == 1


def test_task_subjects_column_drift_fails_closed(tmp_path: Path) -> None:
    """T-277 review: the subject side table has a known shape, so a column added to it
    protects the store like drift in `tasks`, rather than being pruned through."""
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path, subject="v1.0")
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert [action["id"] for action in plan["actions"]] == [task["id"]]
    assert plan["protected"].get("unsupported_schema", 0) == 0
    with store.connection() as db:
        db.execute("ALTER TABLE task_subjects ADD COLUMN future_reference TEXT")
    drifted = Maintenance(tmp_path / "bridge.json").plan()
    assert drifted["actions"] == []
    assert drifted["protected"]["unsupported_schema"] == 1


def test_unknown_store_columns_fail_closed_without_preview_migration(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("ALTER TABLE tasks ADD COLUMN future_reference TEXT")
    before = store.path.read_bytes()
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == []
    assert plan["protected"]["unsupported_schema"] == 1
    assert store.path.read_bytes() == before


def test_invalid_and_missing_event_relationships_preserve_groups(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        db.execute(
            "UPDATE events SET payload='{\"task_id\":\"missing\"}' WHERE type='task.completed'"
        )
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []


def test_reserved_unbound_bus_task_is_never_pruned(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        db.execute("UPDATE commands SET task_id=NULL,delivery='rejected'")
    assert Maintenance(tmp_path / "bridge.json").plan()["actions"] == []


def test_actual_apply_time_is_separate_from_fixed_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    monkeypatch.setattr("answerback_bridge.maintenance.time.time", lambda: 10_000_000.0)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    monkeypatch.setattr("answerback_bridge.maintenance.time.time", lambda: 10_000_123.0)
    maintenance.apply(plan)
    with store.connection() as db:
        record = json.loads(db.execute("SELECT record FROM retention_records").fetchone()[0])
    assert record["pruned_at"] == 10_000_123.0
    assert maintenance.apply(plan)["status"] == "already_applied"


def test_new_legacy_commands_and_notifications_get_companion_identity(tmp_path: Path) -> None:
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.retention import migrate_content

    bus = BusStore(tmp_path / "bus" / "ledger.sqlite")
    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    with bus.connection() as db:
        migrate_content(db, "commands")
    with mailbox._connect() as db:
        migrate_content(db, "messages")
    command = bus.submit(
        request_id="new",
        peer_id="kiro:one",
        project="/project",
        routing_digest="a" * 64,
        message="new",
    )
    notice = mailbox.notify("codex", "new", "new")
    with bus.connection() as db:
        assert db.execute(
            "SELECT 1 FROM retention_records WHERE entity='commands' AND row_id=?",
            (command["command_id"],),
        ).fetchone()
    with mailbox._connect() as db:
        assert db.execute(
            "SELECT 1 FROM retention_records WHERE entity='messages' AND row_id=?",
            (str(notice["id"]),),
        ).fetchone()


def test_wal_preview_does_not_create_wal_or_shm_sidecars(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(MaintenanceError) as refused:
        Maintenance(tmp_path / "bridge.json").plan()
    assert refused.value.code == "unsupported_journal"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_stats_reports_retained_metadata_unknown_ownership_and_client_exclusion(
    tmp_path: Path,
) -> None:
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.maintenance import Maintenance, RetentionSelection

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE tasks SET target='desktop:one',metadata='{\"kind\":\"desktop\"}'")
        db.execute("UPDATE retention_records SET changed_at=3")
    ClientMailbox(tmp_path / "client-mailbox.sqlite")
    stats = Maintenance(tmp_path / "bridge.json").stats(RetentionSelection(provider="codex"))
    assert stats["stores"]["tasks"]["tables"]["tasks"]["project_unknown_rows"] == 1
    assert stats["stores"]["tasks"]["tables"]["tasks"]["retained_metadata_bytes"] > 0
    assert stats["stores"]["clients"]["excluded"] is True


@pytest.mark.parametrize(
    "field,value", [("published_at", 99), ("payload", '{"task_id":"missing"}')]
)
def test_publication_or_event_preimage_race_aborts_plan(
    tmp_path: Path, field: str, value: object
) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    with bus.connection() as db:
        table = "outbox" if field == "published_at" else "events"
        db.execute(f"UPDATE {table} SET {field}=?", (value,))
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert tasks.get(task)["result"] == "done"


def test_forged_plan_with_new_digest_cannot_bypass_policy(tmp_path: Path) -> None:
    from answerback_bridge.bus.model import digest
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, task = fixture_task(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    plan["actions"][0]["kind"] = "delete"
    plan["groups"][0]["actions"][0]["kind"] = "delete"
    plan["digest"] = digest({key: value for key, value in plan.items() if key != "digest"})
    with pytest.raises(MaintenanceError):
        maintenance.apply(plan)
    assert store.get(task["id"])["result"] == "private done"


def test_fixed_cutoff_never_expands_the_reviewed_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, first = fixture_task(tmp_path)
    _, second = fixture_task(tmp_path, key="second")
    now = 10_000_000.0
    with store.connection() as db:
        db.execute(
            "UPDATE tasks SET created=?,updated=? WHERE id=?",
            (now - 86400, now - 86400, second["id"]),
        )
        db.execute(
            "UPDATE retention_records SET changed_at=? WHERE row_id=?", (now - 86400, second["id"])
        )
    monkeypatch.setattr("answerback_bridge.maintenance.time.time", lambda: now)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert [a["id"] for a in plan["actions"]] == [first["id"]]
    monkeypatch.setattr("answerback_bridge.maintenance.time.time", lambda: now + 90 * 86400)
    maintenance.apply(plan)
    assert store.get(second["id"])["result"] == "private done"


def test_unknown_retention_version_is_not_interpreted(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE retention_records SET record=?", ('{"schema_version":2,"fields":{}}',))
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == [] and plan["protected"]["malformed_record"] == 1


def test_incomplete_and_conflicting_bus_components_stay_intact(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        db.execute("UPDATE commands SET task_fingerprint=?", ("f" * 64,))
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == [] and plan["protected"]["unprojected_task"] >= 1
    bus.project_task(command, tasks.get(task))
    for row in bus.pending_outbox():
        bus.mark_published(row["id"])
    with tasks.connection() as db:
        db.execute("UPDATE tasks SET metadata=?", ('{"kind":"kiro","cwd":"/different"}',))
        db.execute("UPDATE retention_records SET changed_at=3")
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == [] and plan["protected"]["conflicting_project"] >= 1


def test_tampered_published_command_payload_is_protected(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        payload = json.loads(
            db.execute("SELECT payload FROM outbox WHERE kind='command'").fetchone()[0]
        )
        payload["message"] = "tampered"
        db.execute("UPDATE outbox SET payload=? WHERE kind='command'", (json.dumps(payload),))
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == []
    assert plan["protected"]["conflicting_command_publication"] >= 1


def test_unknown_schema_version_refuses_runtime_migration_atomically(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
        db.execute("CREATE TABLE future(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        TaskStore(path)
    assert path.read_bytes() == before


def test_historical_project_filter_does_not_follow_changed_symlinks(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, RetentionSelection

    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    link = tmp_path / "project"
    link.symlink_to(left, target_is_directory=True)
    store, task = fixture_task(tmp_path, metadata={"kind": "kiro", "cwd": str(link)})
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan(RetentionSelection(project=str(link)))
    assert plan["actions"]
    link.unlink()
    link.symlink_to(right, target_is_directory=True)
    assert maintenance.apply(plan)["status"] == "applied"
    assert store.get(task["id"])["status"] == "completed"


def test_many_large_bodies_keep_stats_aggregate_and_entire_plan_capped(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, RetentionSelection

    store = TaskStore(tmp_path / "tasks.sqlite")
    message, result = "界" * 500, "private-large-result" * 100
    with store.connection() as db:
        db.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"id-{number:04d}",
                    f"req-{number}",
                    f"herdr:{number}",
                    message,
                    "completed",
                    1,
                    2,
                    '{"kind":"herdr"}',
                    result,
                    None,
                    None,
                )
                for number in range(1200)
            ],
        )
        db.execute("UPDATE retention_records SET changed_at=3")
    maintenance = Maintenance(tmp_path / "bridge.json")
    stats = maintenance.stats()
    assert stats["stores"]["tasks"]["tables"]["tasks"]["rows"] == 1200
    assert stats["stores"]["tasks"]["logical_content_bytes"] == 1200 * len(
        (message + result).encode()
    )
    plan = maintenance.plan(RetentionSelection(max_actions=25, max_roots=100))
    assert len(plan["actions"]) == 25 and plan["truncated"] is True
    assert len(json.dumps(plan).encode()) < 150_000
    assert message not in json.dumps(plan, ensure_ascii=False)
    assert result not in json.dumps(stats)


def test_legacy_preview_does_not_migrate_or_claim_historical_mutation_age(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    path = tmp_path / "tasks.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,"
            "target TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,"
            "updated REAL NOT NULL,metadata TEXT NOT NULL,result TEXT,detail TEXT,turn_id TEXT)"
        )
        db.execute(
            "INSERT INTO tasks VALUES ('old','old','herdr:old','body','completed',1,2,"
            "'{\"kind\":\"herdr\"}','answer',NULL,NULL)"
        )
    before = path.read_bytes()
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert maintenance.stats()["stores"]["tasks"]["retention_schema_available"] is False
    plan = maintenance.plan()
    assert plan["actions"] == [] and plan["protected"]["migration_required"] == 1
    assert path.read_bytes() == before
    TaskStore(path)
    assert maintenance.plan()["protected"]["recent_mutation"] == 1


def test_unacknowledged_notifications_and_ack_race_remain_safe(tmp_path: Path) -> None:
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    pending = mailbox.notify("codex", "pending", "pending")
    acknowledged = mailbox.notify("codex", "acknowledged", "acknowledged")
    mailbox.acknowledge("codex", [acknowledged["id"]])
    with mailbox._connect() as db:
        db.execute(
            "UPDATE messages SET created=1,acknowledged=CASE "
            "WHEN acknowledged IS NULL THEN NULL ELSE 2 END"
        )
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert [a["id"] for a in plan["actions"]] == [str(acknowledged["id"])]
    with mailbox._connect() as db:
        db.execute("UPDATE messages SET acknowledged=3 WHERE id=?", (acknowledged["id"],))
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert mailbox.pending("codex")["messages"][0]["id"] == pending["id"]
    with mailbox._connect() as db:
        assert (
            db.execute("SELECT message FROM messages WHERE id=?", (acknowledged["id"],)).fetchone()[
                0
            ]
            == "acknowledged"
        )


def test_partial_postimage_is_stale_and_never_finished_automatically(tmp_path: Path) -> None:
    from answerback_bridge.bus.model import canonical
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, _ = fixture_task(tmp_path)
    fixture_task(tmp_path, key="second")
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    first, second = plan["actions"]
    with store.connection() as db:
        db.execute(
            "UPDATE retention_records SET record=? WHERE row_id=?",
            (canonical(first["retention"]), first["id"]),
        )
        db.execute("UPDATE tasks SET message='',result=NULL,detail=NULL WHERE id=?", (first["id"],))
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert store.get(second["id"])["result"] == "private done"


def test_compaction_sqlite_lock_failure_preserves_database(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    store, task = fixture_task(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "owner.lock").touch(mode=0o600)
    before = store.path.read_bytes()
    with store.connection() as locked:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(MaintenanceError) as refused:
            Maintenance(tmp_path / "bridge.json", lock_timeout=0.01).compact("tasks")
        assert refused.value.code == "busy"
    assert store.path.read_bytes() == before
    assert store.get(task["id"])["result"] == "private done"


def test_incomplete_retention_record_is_protected_as_malformed(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    store, task = fixture_task(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE retention_records SET record=?",
            ('{"schema_version":1,"pruned_at":100,"fields":{}}',),
        )
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == []
    assert plan["protected"]["malformed_record"] == 1


@pytest.mark.parametrize("reference", ["task_id", "blocking_task_id", "result_ref"])
@pytest.mark.parametrize("completed_first", [False, True])
@pytest.mark.parametrize("location", ["both", "nested_outbox", "outbox_payload_key"])
def test_ambiguous_incoming_references_protect_pending_delivery(
    tmp_path: Path, reference: str, completed_first: bool, location: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    active, _ = tasks.reserve(
        "kiro:active", "active", "active", {"kind": "kiro", "cwd": "/project"}
    )
    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    command = bus.submit(
        request_id="rejected",
        peer_id="kiro:other",
        project="/project",
        routing_digest="a" * 64,
        message="other",
    )
    bus.finish(command["command_id"], "rejected", "busy")
    for row in bus.pending_outbox():
        bus.mark_published(row["id"])
    prefix = "task:" if reference == "result_ref" else ""
    values = [prefix + active["id"], prefix + completed["id"]]
    if completed_first:
        values.reverse()
    duplicate = (
        "{" + ",".join(json.dumps(reference) + ":" + json.dumps(value) for value in values) + "}"
    )
    with bus.connection() as db:
        event = db.execute("SELECT * FROM events WHERE type='command.rejected'").fetchone()
        if location == "both":
            db.execute("UPDATE events SET payload=? WHERE id=?", (duplicate, event["id"]))
        envelope = json.loads(
            db.execute("SELECT payload FROM outbox WHERE id=?", (event["id"],)).fetchone()[0]
        )
        envelope.pop("payload")
        fields = json.dumps(envelope)[:-1]
        if location == "outbox_payload_key":
            payloads = [
                json.dumps({reference: prefix + active["id"]}),
                json.dumps({reference: prefix + completed["id"]}),
            ]
            if completed_first:
                payloads.reverse()
            raw = fields + ',"payload":' + payloads[0] + ',"payload":' + payloads[1] + "}"
        else:
            raw = fields + ',"payload":' + duplicate + "}"
        db.execute("UPDATE outbox SET payload=?,published_at=NULL WHERE id=?", (raw, event["id"]))
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert all(action["id"] != completed["id"] for action in plan["actions"])
    assert plan["protected"].get("malformed_relationship", 0) > 0
    maintenance.apply(plan)
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(completed["id"])["result"] == "private done"
    assert any(row["id"] == event["id"] for row in bus.pending_outbox())


@pytest.mark.parametrize(
    "payload",
    ['{"blocking_task_id":7}', '{"result_ref":"unknown:task"}', '{"task_id":""}'],
)
def test_invalid_unselected_reference_cannot_prove_standalone_task_unreferenced(
    tmp_path: Path, payload: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    command = bus.submit(
        request_id="rejected",
        peer_id="kiro:other",
        project="/project",
        routing_digest="a" * 64,
        message="other",
    )
    bus.finish(command["command_id"], "rejected", "busy")
    with bus.connection() as db:
        db.execute("UPDATE events SET payload=? WHERE type='command.rejected'", (payload,))
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert all(action["id"] != completed["id"] for action in plan["actions"])
    assert plan["protected"].get("malformed_relationship", 0) > 0
    assert tasks.get(completed["id"])["result"] == "private done"


@pytest.mark.parametrize("limit", ["rows", "bytes"])
def test_incoming_reference_inventory_limits_protect_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    if limit == "rows":
        monkeypatch.setattr("answerback_bridge.maintenance_policy.MAX_COMPONENT_ROWS", 3)
    else:
        monkeypatch.setattr("answerback_bridge.maintenance_policy.MAX_COMPONENT_BYTES", 4096)
        with bus.connection() as db:
            db.execute(
                "UPDATE events SET payload=? WHERE type='command.cancelled'",
                (json.dumps({"detail": "x" * 5000}),),
            )
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert all(action["id"] != completed["id"] for action in plan["actions"])
    assert plan["protected"].get("component_limit", 0) > 0
    assert tasks.get(completed["id"])["result"] == "private done"


def fixture_legacy_tasks(tmp_path: Path) -> Path:
    path = tmp_path / "tasks.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,"
            "target TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,"
            "updated REAL NOT NULL,metadata TEXT NOT NULL,result TEXT,detail TEXT,turn_id TEXT)"
        )
        db.execute(
            "INSERT INTO tasks VALUES ('legacy','legacy','kiro:old','old','completed',1,2,"
            '\'{"kind":"kiro","cwd":"/project"}\',\'answer\',NULL,NULL)'
        )
    return path


def fixture_cancelled_bus(tmp_path: Path) -> tuple[BusStore, str]:
    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    command = bus.submit(
        request_id="cancel",
        peer_id="kiro:one",
        project="/project",
        routing_digest="a" * 64,
        message="work",
    )
    bus.cancel(command["command_id"])
    for row in bus.pending_outbox():
        bus.mark_published(row["id"])
    return bus, command["command_id"]


def test_legacy_main_uses_migrated_bus_application_receipts(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance

    legacy = fixture_legacy_tasks(tmp_path)
    original = legacy.read_bytes()
    bus, command = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert len(plan["actions"]) == 5
    assert legacy.read_bytes() == original
    assert maintenance.apply(plan)["status"] == "applied"
    assert plan["application_store"] == "bus"
    assert maintenance.apply(plan)["status"] == "already_applied"
    assert maintenance.plan()["actions"] == []
    assert legacy.read_bytes() == original
    with bus.connection() as db:
        assert db.execute("SELECT payload FROM commands WHERE id=?", (command,)).fetchone()[0] == ""
        assert (
            db.execute(
                "SELECT COUNT(*) FROM retention_applications WHERE digest=?", (plan["digest"],)
            ).fetchone()[0]
            == 1
        )


def test_mixed_store_repeat_requires_exact_recorded_application_time(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    legacy = fixture_legacy_tasks(tmp_path)
    original = legacy.read_bytes()
    bus, _ = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    maintenance.apply(plan)
    with bus.connection() as db:
        db.execute("UPDATE retention_applications SET applied_at=applied_at+1")
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert legacy.read_bytes() == original


def test_mixed_store_schema_migration_stales_reviewed_plan(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    legacy = fixture_legacy_tasks(tmp_path)
    bus, command = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    TaskStore(legacy)
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    with bus.connection() as db:
        assert db.execute("SELECT payload FROM commands WHERE id=?", (command,)).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM retention_applications").fetchone()[0] == 0


def test_mixed_store_receipt_failure_rolls_back_all_attached_mutations(tmp_path: Path) -> None:
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.maintenance import Maintenance

    legacy = fixture_legacy_tasks(tmp_path)
    bus, command = fixture_cancelled_bus(tmp_path)
    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    message = mailbox.notify("codex", "notification", "notification")
    mailbox.acknowledge("codex", [message["id"]])
    with mailbox._connect() as db:
        db.execute("UPDATE messages SET created=1,acknowledged=2")
    with bus.connection() as db:
        db.execute(
            "CREATE TRIGGER refuse_application BEFORE INSERT ON retention_applications "
            "BEGIN SELECT RAISE(ABORT,'fixture receipt failure'); END"
        )
    paths = (legacy, bus.path, tmp_path / "client-mailbox.sqlite")
    before = {path: path.read_bytes() for path in paths}
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert {action["store"] for action in plan["actions"]} == {"bus", "clients"}
    with pytest.raises(sqlite3.IntegrityError, match="fixture receipt failure"):
        maintenance.apply(plan)
    assert {path: path.read_bytes() for path in paths} == before
    with bus.connection() as db:
        assert db.execute("SELECT payload FROM commands WHERE id=?", (command,)).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM retention_applications").fetchone()[0] == 0
    with mailbox._connect() as db:
        assert db.execute("SELECT message FROM messages").fetchone()[0] == "notification"
        assert db.execute("SELECT record FROM retention_records").fetchone()[0] is None


def test_legacy_main_uses_migrated_client_application_receipts(tmp_path: Path) -> None:
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.maintenance import Maintenance

    legacy = fixture_legacy_tasks(tmp_path)
    original = legacy.read_bytes()
    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    message = mailbox.notify("codex", "notification", "notification")
    mailbox.acknowledge("codex", [message["id"]])
    with mailbox._connect() as db:
        db.execute("UPDATE messages SET created=1,acknowledged=2")
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert maintenance.apply(plan)["status"] == "applied"
    assert plan["application_store"] == "clients"
    assert maintenance.apply(plan)["status"] == "already_applied"
    assert legacy.read_bytes() == original


def test_forged_application_store_is_rechecked_before_mutations(tmp_path: Path) -> None:
    from answerback_bridge.bus.model import digest
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    legacy = fixture_legacy_tasks(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    before = {path: path.read_bytes() for path in (legacy, bus.path)}
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    plan["application_store"] = "tasks"
    plan["digest"] = digest({key: value for key, value in plan.items() if key != "digest"})
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert {path: path.read_bytes() for path in before} == before


def test_new_ambiguous_incoming_reference_stales_task_plan(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    tasks, completed = fixture_task(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    with bus.connection() as db:
        db.execute(
            "UPDATE events SET payload=? WHERE type='command.cancelled'",
            ('{"task_id":"missing","task_id":' + json.dumps(completed["id"]) + "}",),
        )
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert {path: path.read_bytes() for path in before} == before


def test_command_cleanup_can_reduce_reference_inventory_for_later_task_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    for number in range(3):
        bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
        command = bus.submit(
            request_id=f"cancel-{number}",
            peer_id="kiro:one",
            project="/project",
            routing_digest="a" * 64,
            message="work",
        )
        bus.cancel(command["command_id"])
    for row in bus.pending_outbox():
        bus.mark_published(row["id"])
    monkeypatch.setattr("answerback_bridge.maintenance_policy.MAX_COMPONENT_ROWS", 10)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert plan["protected"]["component_limit"] == 1
    assert {action["store"] for action in plan["actions"]} == {"bus"}
    assert len([action for action in plan["actions"] if action["table"] == "commands"]) == 3
    maintenance.apply(plan)
    following = maintenance.plan()
    assert [action["id"] for action in following["actions"]] == [completed["id"]]
    assert tasks.get(completed["id"])["result"] == "private done"


@pytest.mark.parametrize("table", ["commands", "events", "outbox"])
def test_unknown_bus_graph_columns_protect_standalone_tasks_with_pending_delivery(
    tmp_path: Path, table: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    command = bus.submit(
        request_id="pending",
        peer_id="kiro:other",
        project="/project",
        routing_digest="a" * 64,
        message="pending delivery",
    )
    with bus.connection() as db:
        db.execute(f"ALTER TABLE {table} ADD COLUMN future_task_reference TEXT")
        db.execute(f"UPDATE {table} SET future_task_reference=?", (completed["id"],))
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert plan["actions"] == []
    assert plan["protected"].get("unsupported_schema", 0) > 0
    maintenance.apply(plan)
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(completed["id"])["result"] == "private done"
    assert any(row["command_id"] == command["command_id"] for row in bus.pending_outbox())


def test_unknown_unrelated_bus_table_is_retained_without_inventing_task_references(
    tmp_path: Path,
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    with bus.connection() as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserved')")
    before = bus.path.read_bytes()
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert [action["id"] for action in plan["actions"]] == [completed["id"]]
    assert plan["protected"]["unsupported_schema"] == 1
    maintenance.apply(plan)
    assert bus.path.read_bytes() == before
    assert tasks.get(completed["id"])["result"] is None


@pytest.mark.parametrize(
    "metadata",
    ["missing_table", "missing_row", "invalid_ceiling", "noninteger_ceiling", "extra_row"],
)
def test_missing_or_invalid_bus_retention_history_never_yields_actions(
    tmp_path: Path, metadata: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    bus, command = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert len(maintenance.plan()["actions"]) == 5
    with bus.connection() as db:
        if metadata == "missing_table":
            db.execute("DROP TABLE retention_history")
        elif metadata == "missing_row":
            db.execute("DELETE FROM retention_history")
        elif metadata == "invalid_ceiling":
            db.execute("UPDATE retention_history SET deleted_event_ceiling=-1")
        elif metadata == "noninteger_ceiling":
            db.execute("UPDATE retention_history SET deleted_event_ceiling='unknown'")
        else:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("INSERT INTO retention_history VALUES (2,0)")
    before = bus.path.read_bytes()
    plan = maintenance.plan()
    assert plan["actions"] == []
    assert plan["protected"].get("unsupported_schema", 0) > 0
    maintenance.apply(plan)
    assert bus.path.read_bytes() == before
    assert bus.command(command).message == "work"
    with bus.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


@pytest.mark.parametrize("metadata", ["missing_table", "missing_row"])
def test_retention_history_disappearance_stales_plan_before_any_mutation(
    tmp_path: Path, metadata: str
) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert plan["actions"]
    with bus.connection() as db:
        db.execute(
            "DROP TABLE retention_history"
            if metadata == "missing_table"
            else "DELETE FROM retention_history"
        )
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(task)["result"] == "done"
    assert bus.command(command).message == "work"


@pytest.mark.parametrize("schema", ["commands", "events", "outbox", "version"])
def test_incomplete_or_newer_bus_graph_schema_protects_standalone_tasks(
    tmp_path: Path, schema: str
) -> None:
    from answerback_bridge.maintenance import Maintenance

    tasks, completed = fixture_task(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    with bus.connection() as db:
        db.execute("PRAGMA user_version=1" if schema == "version" else f"DROP TABLE {schema}")
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"] == []
    assert plan["protected"].get("unsupported_schema", 0) > 0
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(completed["id"])["result"] == "private done"


def test_new_bus_reference_schema_stales_standalone_task_plan(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    tasks, completed = fixture_task(tmp_path)
    bus, _ = fixture_cancelled_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert any(action["id"] == completed["id"] for action in plan["actions"])
    with bus.connection() as db:
        db.execute("ALTER TABLE outbox ADD COLUMN future_task_reference TEXT")
        db.execute("UPDATE outbox SET future_task_reference=?", (completed["id"],))
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(completed["id"])["result"] == "private done"


def test_ignored_history_update_cannot_report_event_deletion_success(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    bus, tasks, command, task = fixture_bus(tmp_path)
    with bus.connection() as db:
        db.execute(
            "CREATE TRIGGER ignore_history BEFORE UPDATE ON retention_history "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    before = {path: path.read_bytes() for path in (tasks.path, bus.path)}
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert plan["actions"]
    with pytest.raises(MaintenanceError) as refused:
        maintenance.apply(plan)
    assert refused.value.code == "stale_plan"
    assert {path: path.read_bytes() for path in before} == before
    assert tasks.get(task)["result"] == "done"
    assert bus.command(command).message == "work"
    assert bus.events()["events"]
