"""Transactional command inbox/outbox and value-free lifecycle events."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..retention import (
    content_identity,
    migrate_content,
    read_record,
    task_availability,
    task_projection_fingerprint,
)
from ..transport import private_directory
from .model import Command, canonical, digest

MAX_OUTBOX_ATTEMPTS = 20


class ContentUnavailable(ValueError):
    code = "content_unavailable"


class BusStore:
    def __init__(
        self, path: Path, *, clock: Callable[[], float] = time.time, max_commands: int = 10_000
    ) -> None:
        self.path, self.clock, self.max_commands = path, clock, max_commands
        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Bus database must not be a symlink")
        with self.connection() as db:
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    delivery TEXT NOT NULL, task_id TEXT UNIQUE,
                    task_fingerprint TEXT, target_key TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, detail TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, command_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, published_at REAL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    command_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(published_at, created);
                CREATE TABLE IF NOT EXISTS outbox_failures (
                    id TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
                    last_attempt_at REAL NOT NULL, error_type TEXT NOT NULL,
                    quarantined_at REAL);
                CREATE TRIGGER IF NOT EXISTS outbox_delete_failures
                    AFTER DELETE ON outbox BEGIN
                    DELETE FROM outbox_failures WHERE id=OLD.id;
                END;
                CREATE TABLE IF NOT EXISTS outbox_holds (
                    id TEXT PRIMARY KEY, not_before REAL NOT NULL, reason TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS outbox_delete_holds
                    AFTER DELETE ON outbox BEGIN
                    DELETE FROM outbox_holds WHERE id=OLD.id;
                END;
            """)
            # Additive companion migration preserves the six-column outbox format
            # and all existing rows, including positional inserts used by maintenance.
            # `outbox_holds` (provider backoff, T-227) is a companion table for the same
            # reason: a held command row is simply not published before `not_before`.
            migrate_content(db, "commands")
        path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row: sqlite3.Row, db: sqlite3.Connection) -> dict[str, Any]:
        retained = read_record(db, "commands", row["id"])
        hold = db.execute("SELECT not_before FROM outbox_holds WHERE id=?", (row["id"],)).fetchone()
        return {
            **(
                {"content_pruned": True, "pruned_fields": sorted(retained["fields"])}
                if retained
                else {}
            ),
            "command_id": row["id"],
            "request_id": row["request_key"],
            "delivery": row["delivery"],
            "task_id": row["task_id"],
            "created_at": row["created"],
            "updated_at": row["updated"],
            "detail": row["detail"],
            "not_before": hold["not_before"] if hold is not None else None,
        }

    @staticmethod
    def _row(db: sqlite3.Connection, ident: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM commands WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("Unknown bus command")
        return row

    def _event(
        self, db: sqlite3.Connection, ident: str, kind: str, payload: dict[str, Any]
    ) -> None:
        event_id = str(uuid5(NAMESPACE_URL, ident + ":" + kind + ":" + digest(payload)))
        now = self.clock()
        event = {
            "schema_version": 1,
            "event_id": event_id,
            "command_id": ident,
            "type": kind,
            "payload": payload,
            "created_at": now,
        }
        added = db.execute(
            "INSERT OR IGNORE INTO events(id,command_id,type,payload,created) VALUES (?,?,?,?,?)",
            (event_id, ident, kind, canonical(payload), now),
        ).rowcount
        if added:
            db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,NULL)",
                (event_id, "event", ident, canonical(event), now),
            )

    def submit(
        self,
        *,
        request_id: str,
        peer_id: str,
        project: str,
        routing_digest: str,
        message: str,
        ttl_seconds: int = 3600,
        not_before: float | None = None,
        hold_reason: str = "provider_backoff",
    ) -> dict[str, Any]:
        """Accept one command durably; `not_before` delays its publication, never rejects.

        A repeat of an accepted request returns the stored command unchanged: the hold,
        if any, is the one written when it was first accepted.
        """
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("TTL must be 1–86400 seconds")
        if not_before is not None and (
            isinstance(not_before, bool) or not isinstance(not_before, (int, float))
        ):
            raise ValueError("not_before must be a time in seconds")
        now = self.clock()
        command = Command(
            1,
            str(uuid4()),
            request_id,
            peer_id,
            project,
            routing_digest,
            message,
            now,
            now + ttl_seconds,
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM commands WHERE request_key=?", (request_id,)
            ).fetchone()
            if previous is not None:
                retained = read_record(db, "commands", previous["id"])
                if retained is None:
                    old = Command.from_bytes(previous["payload"].encode())
                    old_identity = (
                        old.peer_id,
                        old.project,
                        old.routing_digest,
                        content_identity(old.message),
                        old.expires_at - old.created_at,
                    )
                else:
                    envelope = retained["envelope"]
                    old_identity = (
                        envelope["peer_id"],
                        envelope["project"],
                        envelope["routing_digest"],
                        retained["message_identity"],
                        retained["ttl_seconds"],
                    )
                if old_identity != (
                    peer_id,
                    project,
                    routing_digest,
                    content_identity(message),
                    ttl_seconds,
                ):
                    raise ValueError("request_id already describes different work")
                return self._public(previous, db)
            if (
                db.execute(
                    "SELECT count(*) FROM commands c LEFT JOIN retention_records r "
                    "ON r.entity='commands' AND r.row_id=c.id WHERE r.record IS NULL"
                ).fetchone()[0]
                >= self.max_commands
            ):
                raise ValueError(
                    "Bus command capacity reached; retain/inspect history before cleanup"
                )
            raw = command.to_bytes().decode()
            db.execute(
                "INSERT INTO commands"
                "(id,request_key,payload,payload_hash,delivery,created,updated) "
                "VALUES (?,?,?,?,'queued',?,?)",
                (command.command_id, request_id, raw, digest(json.loads(raw)), now, now),
            )
            db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,NULL)",
                (command.command_id, "command", command.command_id, raw, now),
            )
            accepted: dict[str, Any] = {"delivery": "queued"}
            if not_before is not None:
                db.execute(
                    "INSERT INTO outbox_holds(id,not_before,reason) VALUES (?,?,?)",
                    (command.command_id, float(not_before), hold_reason[:200]),
                )
                # Only a held command's event carries the key, so an unheld command's
                # event (and its content-derived id) is byte-identical to before.
                accepted["not_before"] = float(not_before)
            self._event(db, command.command_id, "command.accepted", accepted)
            return self._public(self._row(db, command.command_id), db)

    def get(self, ident: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN")
            return self._public(self._row(db, ident), db)

    def command(self, ident: str) -> Command:
        with self.connection() as db:
            db.execute("BEGIN")
            row = self._row(db, ident)
            if read_record(db, "commands", ident) is not None:
                raise ContentUnavailable(
                    "Command content was pruned; its historical identity is retained"
                )
            return Command.from_bytes(row["payload"].encode())

    def verify_payload(self, command: Command) -> None:
        with self.connection() as db:
            row = self._row(db, command.command_id)
            if row["payload_hash"] != digest(json.loads(command.to_bytes())):
                raise ValueError("Broker payload does not match the admitted command")

    def counts(self) -> dict[str, int]:
        with self.connection() as db:
            return dict(db.execute("SELECT delivery,count(*) FROM commands GROUP BY delivery"))

    def entries(self, *states: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            db.execute("BEGIN")
            return [
                self._public(row, db)
                for row in db.execute("SELECT * FROM commands ORDER BY created")
                if row["delivery"] in states
            ]

    def claim(self, ident: str, target_key: str | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, ident)
            if row["delivery"] != "queued":
                return False
            command = Command.from_bytes(row["payload"].encode())
            if command.expires_at <= self.clock():
                self._finish(db, ident, "expired", "Command expired before admission")
                return False
            self._finish(db, ident, "dispatching", None)
            db.execute("UPDATE commands SET target_key=? WHERE id=?", (target_key, ident))
            return True

    def _finish(
        self,
        db: sqlite3.Connection,
        ident: str,
        state: str,
        detail: str | None,
        blocking_task_id: str | None = None,
    ) -> None:
        db.execute(
            "UPDATE commands SET delivery=?,detail=?,updated=? WHERE id=?",
            (state, detail, self.clock(), ident),
        )
        payload = {"delivery": state, "detail": detail}
        if blocking_task_id is not None:
            payload.update(reason_code="target_busy", blocking_task_id=blocking_task_id)
        self._event(db, ident, "command." + state, payload)

    def finish(
        self,
        ident: str,
        state: str,
        detail: str,
        *,
        blocking_task_id: str | None = None,
    ) -> None:
        if state not in {"rejected", "uncertain"}:
            raise ValueError("Invalid admission result")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            allowed = {"dispatching", "queued"} if state == "rejected" else {"dispatching"}
            if self._row(db, ident)["delivery"] not in allowed:
                return
            self._finish(db, ident, state, detail[:1000], blocking_task_id)

    def cancel(self, ident: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._row(db, ident)["delivery"]
            if state == "cancelled":
                return self._public(self._row(db, ident), db)
            if state != "queued":
                raise ValueError("Command already admitted; inspect its task before cancellation")
            self._finish(db, ident, "cancelled", "Cancelled before admission")
            db.execute(
                "DELETE FROM outbox WHERE command_id=? AND kind='command' AND published_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM outbox_failures f "
                "WHERE f.id=outbox.id AND f.quarantined_at IS NOT NULL)",
                (ident,),
            )
            return self._public(self._row(db, ident), db)

    def bind(self, ident: str, task: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, ident)
            if row["target_key"] is not None and task.get("target") != row["target_key"]:
                raise ValueError("Receipt does not match the admitted target")
            if row["task_id"] is not None and row["task_id"] != task["id"]:
                raise ValueError("Command already belongs to a different task")
            if row["delivery"] not in {"dispatching", "uncertain", "bound"}:
                raise ValueError("Command cannot be bound in this state")
            db.execute(
                "UPDATE commands SET delivery='bound',task_id=?,detail=NULL,updated=? WHERE id=?",
                (task["id"], self.clock(), ident),
            )
            self._project(db, ident, task)

    def _project(self, db: sqlite3.Connection, ident: str, task: dict[str, Any]) -> bool:
        row = self._row(db, ident)
        if row["task_id"] != task["id"]:
            raise ValueError("Task does not belong to this command")
        fingerprint = task_projection_fingerprint(task)
        if row["task_fingerprint"] == fingerprint:
            return False
        db.execute(
            "UPDATE commands SET task_fingerprint=?,updated=? WHERE id=?",
            (fingerprint, self.clock(), ident),
        )
        self._event(
            db,
            ident,
            "task." + task["status"],
            {
                "task_id": task["id"],
                "status": task["status"],
                "result_ref": f"task:{task['id']}"
                if task_availability(task)["has_result"]
                else None,
                "turn_id": task.get("turn_id"),
                "revision": fingerprint,
            },
        )
        return True

    def project_task(self, ident: str, task: dict[str, Any]) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._project(db, ident, task)

    def pending_outbox(self, limit: int = 100, *, kind: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid outbox page size")
        if kind not in (None, "command", "event"):
            raise ValueError("Invalid outbox kind")
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM outbox WHERE published_at IS NULL AND (? IS NULL OR kind=?) "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_failures f "
                    "WHERE f.id=outbox.id AND f.quarantined_at IS NOT NULL) "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_holds h "
                    "WHERE h.id=outbox.id AND h.not_before > ?) "
                    "ORDER BY created,rowid LIMIT ?",
                    (kind, kind, self.clock(), limit),
                )
            ]

    def record_outbox_failure(self, ident: str, error_type: str) -> bool:
        """Persist one failed publish; return whether this row is no longer retryable."""
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "INSERT INTO outbox_failures (id,attempts,last_attempt_at,error_type) "
                "SELECT id,1,?,? FROM outbox WHERE id=? AND published_at IS NULL "
                "ON CONFLICT(id) DO UPDATE SET attempts=attempts+1, "
                "last_attempt_at=excluded.last_attempt_at,error_type=excluded.error_type "
                "WHERE quarantined_at IS NULL RETURNING attempts",
                (now, error_type[:200], ident),
            ).fetchone()
            if row is None:
                return True
            if row["attempts"] < MAX_OUTBOX_ATTEMPTS:
                return False
            db.execute(
                "UPDATE outbox_failures SET quarantined_at=? WHERE id=?",
                (now, ident),
            )
            return True

    def quarantined_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """List retained failed-delivery metadata without exposing message payloads."""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid outbox page size")
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.id,o.kind,o.command_id,f.attempts,f.last_attempt_at,"
                    "f.quarantined_at,f.error_type FROM outbox o "
                    "JOIN outbox_failures f ON f.id=o.id "
                    "WHERE o.published_at IS NULL AND f.quarantined_at IS NOT NULL "
                    "ORDER BY f.quarantined_at,o.created,o.rowid LIMIT ?",
                    (limit,),
                )
            ]

    def outbox_quarantine_counts(self) -> dict[str, int]:
        """Count unpublished quarantined rows for coordinator status."""
        with self.connection() as db:
            return {
                "command": 0,
                "event": 0,
                **dict(
                    db.execute(
                        "SELECT o.kind,count(*) FROM outbox o JOIN outbox_failures f ON f.id=o.id "
                        "WHERE o.published_at IS NULL AND f.quarantined_at IS NOT NULL "
                        "GROUP BY o.kind"
                    )
                ),
            }

    def mark_published(self, ident: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE outbox SET published_at=? WHERE id=? AND published_at IS NULL",
                (self.clock(), ident),
            )

    def events(self, *, after: int = 0, limit: int = 50) -> dict[str, Any]:
        if type(after) is not int or after < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor or page size")
        with self.connection() as db:
            db.execute("BEGIN")
            high = db.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
            ).fetchone()[0]
            floor = db.execute("SELECT COALESCE(min(seq),?) FROM events", (high + 1,)).fetchone()[0]
            deleted = db.execute(
                "SELECT deleted_event_ceiling FROM retention_history WHERE singleton=1"
            ).fetchone()[0]
            events = [
                {**dict(row), "payload": json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
                )
            ]
        return {
            "events": events,
            "next_cursor": events[-1]["seq"] if events else max(after, high),
            "latest_cursor": high,
            "retained_event_floor": floor,
            "retention_gap": after < deleted,
        }
