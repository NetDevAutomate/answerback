"""Store-owned retention identities. No migration occurs during read-only maintenance."""

import hashlib
import json
import math
import re
import sqlite3
import time
from typing import Any

from .digests import digest

VERSION = 1
SQL_CLOCK = "((julianday('now') - 2440587.5) * 86400.0)"


def migrate_content(db: sqlite3.Connection, table: str) -> None:
    if table not in ("tasks", "commands", "messages"):
        raise ValueError("Unknown retention table")
    if db.execute("PRAGMA user_version").fetchone()[0] != 0:
        raise ValueError("Unsupported store schema version")
    for name, expected in (
        ("retention_records", {"entity", "row_id", "changed_at", "record"}),
        ("retention_applications", {"digest", "applied_at"}),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
        if columns and columns != expected:
            raise ValueError("Unsupported retention schema")
    db.execute("""CREATE TABLE IF NOT EXISTS retention_records (
        entity TEXT NOT NULL, row_id TEXT NOT NULL, changed_at REAL NOT NULL,
        record TEXT, PRIMARY KEY(entity,row_id))""")
    db.execute(
        "CREATE TABLE IF NOT EXISTS retention_applications "
        "(digest TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    if table == "commands":
        db.execute(
            "CREATE TABLE IF NOT EXISTS retention_history ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "deleted_event_ceiling INTEGER NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO retention_history VALUES (1,0)")
    trigger = f"retention_{table}_inserted"
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone():
        db.execute(
            f"INSERT OR IGNORE INTO retention_records(entity,row_id,changed_at) "
            f"SELECT ?, CAST(id AS TEXT), ? FROM {table}",
            (table, time.time()),
        )
        db.execute(f"""CREATE TRIGGER {trigger} AFTER INSERT ON {table}
            BEGIN INSERT OR IGNORE INTO retention_records(entity,row_id,changed_at)
                VALUES ('{table}',CAST(NEW.id AS TEXT),{SQL_CLOCK}); END""")


def migrate_tasks(db: sqlite3.Connection) -> None:
    migrate_content(db, "tasks")
    unchanged = " AND ".join(
        f"NEW.{key} IS OLD.{key}"
        for key in (
            "id",
            "request_key",
            "target",
            "status",
            "created",
            "updated",
            "metadata",
            "turn_id",
        )
    )
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS task_retention_changed
        AFTER UPDATE ON tasks
        WHEN NOT (OLD.message!='' AND NEW.message='' AND NEW.result IS NULL
                  AND NEW.detail IS NULL AND {unchanged}
                  AND EXISTS (SELECT 1 FROM retention_records
                    WHERE entity='tasks' AND row_id=NEW.id AND record IS NOT NULL))
        BEGIN INSERT INTO retention_records(entity,row_id,changed_at)
            VALUES ('tasks',NEW.id,{SQL_CLOCK})
            ON CONFLICT(entity,row_id) DO UPDATE SET changed_at={SQL_CLOCK}; END""")


def migrate_subjects(db: sqlite3.Connection) -> None:
    """The subject of each receipt that has one (T-277), in its own table.

    A table rather than a `tasks` column, so the `tasks` columns that positional inserts and
    maintenance's schema check rely on are unchanged. A row is written by the reservation
    that creates the receipt, never updated, and removed with its task.
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS task_subjects (task_id TEXT PRIMARY KEY, subject TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS task_subjects_subject ON task_subjects(subject)")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS task_subjects_immutable BEFORE UPDATE ON task_subjects "
        "BEGIN SELECT RAISE(ABORT, 'Task subject is immutable'); END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS task_subjects_deleted AFTER DELETE ON tasks "
        "BEGIN DELETE FROM task_subjects WHERE task_id=OLD.id; END"
    )


def read_subject(db: sqlite3.Connection, task_id: str) -> str | None:
    """The receipt's subject, or None for one without (or a store without the table)."""
    try:
        row = db.execute("SELECT subject FROM task_subjects WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise
    return None if row is None else row[0]


def content_identity(value: str | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, str):
        raise ValueError("Content must be text or null")
    raw = b"" if value is None else value.encode("utf-8")
    return {
        "present": value is not None,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def decode_retention(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    record = json.loads(value)
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != VERSION
        or type(record.get("schema_version")) is not int
        or not isinstance(record.get("fields"), dict)
    ):
        raise ValueError("Unsupported retention record")
    for field, item in record["fields"].items():
        if field not in ("message", "result", "detail", "payload") or (
            not isinstance(item, dict)
            or set(item) != {"present", "bytes", "sha256"}
            or type(item["present"]) is not bool
            or type(item["bytes"]) is not int
            or item["bytes"] < 0
            or not isinstance(item["sha256"], str)
            or re.fullmatch("[a-f0-9]{64}", item["sha256"]) is None
        ):
            raise ValueError("Invalid retained content identity")
    fields = set(record["fields"])
    common = {"schema_version", "pruned_at", "fields"}
    if fields == {"message", "result", "detail"}:
        if (
            set(record) != common | {"current_result_present", "projection"}
            or type(record["current_result_present"]) is not bool
        ):
            raise ValueError("Invalid retained task facts")
        projection = record["projection"]
        if projection is not None and (
            not isinstance(projection, dict)
            or set(projection) != {"state", "fingerprint"}
            or not isinstance(projection["state"], dict)
            or set(projection["state"])
            != {"id", "request_key", "target", "status", "updated", "turn_id"}
            or not isinstance(projection["fingerprint"], str)
            or re.fullmatch("[a-f0-9]{64}", projection["fingerprint"]) is None
        ):
            raise ValueError("Invalid retained task projection")
    elif fields == {"payload", "detail"}:
        if set(record) != common | {"envelope", "message_identity", "ttl_seconds"}:
            raise ValueError("Invalid retained command facts")
        envelope = record["envelope"]
        if (
            not isinstance(envelope, dict)
            or set(envelope)
            != {
                "schema_version",
                "command_id",
                "request_id",
                "peer_id",
                "project",
                "routing_digest",
                "created_at",
                "expires_at",
            }
            or envelope.get("schema_version") != 1
            or type(record["ttl_seconds"]) not in (int, float)
            or not 0 < record["ttl_seconds"] <= 86400
        ):
            raise ValueError("Invalid retained command identity")
        identity = record["message_identity"]
        if (
            not isinstance(identity, dict)
            or set(identity) != {"present", "bytes", "sha256"}
            or identity["present"] is not True
            or type(identity["bytes"]) is not int
            or not 1 <= identity["bytes"] <= 100000
            or not isinstance(identity["sha256"], str)
            or re.fullmatch("[a-f0-9]{64}", identity["sha256"]) is None
        ):
            raise ValueError("Invalid retained command message identity")
    elif fields == {"message"}:
        if set(record) != common:
            raise ValueError("Invalid retained notification facts")
    else:
        raise ValueError("Unknown retained content fields")
    when = record.get("pruned_at")
    if type(when) not in (int, float) or not math.isfinite(when) or when <= 0:
        raise ValueError("Invalid retention timestamp")
    return record


def content_matches(task: dict[str, Any], field: str, value: str | None) -> bool:
    """Compare live content without confirming guesses about a pruned field."""
    retention = task.get("_retention")
    if retention is None or field not in retention["fields"]:
        return task.get(field) == value
    raise ValueError("Pruned task content cannot be verified; inspect the existing receipt")


# `matches_work` without a subject leaves it to the caller: a provider's own repeat check
# does not know the submission's subject, and the router compares it on every receipt.
ANY_SUBJECT: Any = object()


def matches_work(
    task: dict[str, Any],
    target: str,
    message: str,
    request_key: str,
    provider: str | None = None,
    *,
    subject: Any = ANY_SUBJECT,
) -> bool:
    """The stored receipt describes this work: request, target, provider, subject, message.

    A receipt from before subjects existed has none, so it matches only a repeat without one.
    """
    return (
        task["request_key"] == request_key
        and (subject is ANY_SUBJECT or task.get("subject") == subject)
        and task["target"] == target
        and (provider is None or task["metadata"].get("kind") == provider)
        and content_matches(task, "message", message)
    )


def projection_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: task.get(key)
        for key in ("id", "request_key", "target", "status", "updated", "turn_id")
    }


def task_projection_fingerprint(task: dict[str, Any]) -> str:
    retention = task.get("_retention")
    projection = retention.get("projection") if retention else None
    if projection is not None and projection.get("state") == projection_state(task):
        if (
            task.get("result") is not None
            or task.get("detail") is not None
            or not isinstance(projection.get("fingerprint"), str)
            or re.fullmatch("[a-f0-9]{64}", projection["fingerprint"]) is None
        ):
            raise ValueError("Retained projection does not match stored content")
        return projection["fingerprint"]
    return digest(
        {key: task.get(key) for key in ("status", "result", "detail", "updated", "turn_id")}
    )


def task_retention(task: dict[str, Any], when: float) -> dict[str, Any]:
    return {
        "schema_version": VERSION,
        "pruned_at": when,
        "fields": {key: content_identity(task.get(key)) for key in ("message", "result", "detail")},
        "current_result_present": task.get("result") is not None,
        "projection": {
            "state": projection_state(task),
            "fingerprint": task_projection_fingerprint(task),
        },
    }


def task_availability(task: dict[str, Any]) -> dict[str, Any]:
    retention = task.get("_retention")
    if retention is None:
        return {
            "has_result": task.get("result") is not None,
            "result_available": task.get("result") is not None,
        }
    return {
        "content_pruned": True,
        "pruned_fields": sorted(retention["fields"]),
        "has_result": retention.get("current_result_present", False),
        "result_available": task.get("result") is not None,
    }


def read_record(db: sqlite3.Connection, entity: str, ident: str | int) -> dict[str, Any] | None:
    if entity not in ("tasks", "commands", "messages"):
        raise ValueError("Unknown retained entity")
    row = db.execute(
        "SELECT record FROM retention_records WHERE entity=? AND row_id=?", (entity, str(ident))
    ).fetchone()
    return decode_retention(row[0]) if row else None


def decode_task(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
    task = dict(row)
    task["metadata"] = json.loads(task["metadata"])
    task["subject"] = read_subject(db, task["id"])
    record = read_record(db, "tasks", task["id"])
    if record is not None:
        if set(record["fields"]) != {"message", "result", "detail"} or task["message"] != "":
            raise ValueError("Invalid retained task content")
        task["_retention"] = record
    return task
