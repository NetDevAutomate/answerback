"""Durable cooperative Rex enrollment and correlated receipts in the task database."""

import json
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ...config import public_task
from ...retention import content_matches
from ...tasks import TERMINAL, Task, TaskStore, ended_metadata_text
from .client import _full_id

Record = dict[str, Any]
_UNFINISHED = "status NOT IN ('completed', 'failed', 'abandoned', 'cancelled')"


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)
    ):
        raise ValueError(f"{field} must be a nonempty string without controls")
    return value


def _binding(binding: Record) -> Record:
    if not isinstance(binding, dict):
        raise ValueError("binding must be an object")
    canonical: Record = {
        key: _text(binding.get(key), key)
        for key in (
            "endpoint",
            "server_instance_id",
            "server_host_id",
            "session_id",
            "block_id",
            "block_host_id",
            "foreground_name",
            "foreground_cwd",
        )
    }
    endpoint = urlsplit(canonical["endpoint"])
    if (
        endpoint.scheme != "unix"
        or endpoint.netloc
        or not endpoint.path.startswith("/")
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Rex requires an explicit local Unix endpoint")
    if canonical["server_host_id"] != canonical["block_host_id"]:
        raise ValueError("Rex host identities must agree")
    _full_id(canonical["session_id"], "session")
    _full_id(canonical["block_id"], "block")
    if not PurePosixPath(canonical["foreground_cwd"]).is_absolute():
        raise ValueError("foreground_cwd must be absolute")
    pid = binding.get("foreground_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("foreground_pid must be a positive integer")
    canonical["foreground_pid"] = pid
    for key in ("foreground_path", "foreground_script"):
        value = binding.get(key)
        canonical[key] = None if value is None else _text(value, key)
    if len(json.dumps(canonical).encode("utf-8")) > 16384:
        raise ValueError("Binding exceeds 16 KiB")
    return canonical


class RexMailbox:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS rex_peers ("
                "peer_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, "
                "name TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, "
                "created REAL NOT NULL, updated REAL NOT NULL, "
                "endpoint TEXT NOT NULL, session_id TEXT NOT NULL, block_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS rex_peer_terminal ON "
                "rex_peers(endpoint, session_id, block_id) WHERE state != 'retired'"
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> Record:
        return {
            "peer_id": row["peer_id"],
            "name": row["name"],
            "binding": json.loads(row["binding"]),
            "state": row["state"],
            "created": row["created"],
            "updated": row["updated"],
        }

    def _peer(self, db: sqlite3.Connection, peer_id: str) -> Record:
        row = db.execute("SELECT * FROM rex_peers WHERE peer_id=?", (peer_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Rex peer ID")
        return self._decode(row)

    @staticmethod
    def _active(peer: Record) -> None:
        if peer["state"] != "active":
            raise ValueError("Rex peer is stale or retired")

    def enroll(self, name: str, binding: Record, request_id: str) -> Record:
        name = _text(name, "name")
        if len(name.encode("utf-8")) > 128:
            raise ValueError("Name exceeds 128 UTF-8 bytes")
        canonical = _binding(binding)
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id
        ):
            raise ValueError("Invalid enrollment request_id")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM rex_peers WHERE request_key=?", (request_id,)
            ).fetchone()
            if row:
                peer = self._decode(row)
                if peer["name"] != name or peer["binding"] != canonical:
                    raise ValueError("request_id already describes a different enrollment")
                return peer
            if (
                db.execute("SELECT COUNT(*) FROM rex_peers WHERE state != 'retired'").fetchone()[0]
                >= 128
            ):
                raise ValueError("Rex peer capacity reached")
            if db.execute(
                "SELECT peer_id FROM rex_peers WHERE endpoint=? AND session_id=? "
                "AND block_id=? AND state != 'retired'",
                (canonical["endpoint"], canonical["session_id"], canonical["block_id"]),
            ).fetchone():
                raise ValueError("Rex terminal already enrolled; retire it explicitly")
            peer_id, now = f"rex:{uuid.uuid4()}", time.time()
            db.execute(
                "INSERT INTO rex_peers VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                (
                    peer_id,
                    request_id,
                    name,
                    json.dumps(canonical),
                    now,
                    now,
                    canonical["endpoint"],
                    canonical["session_id"],
                    canonical["block_id"],
                ),
            )
            return self._peer(db, peer_id)

    def get(self, peer_id: str) -> Record:
        with self.store.connection() as db:
            return self._peer(db, peer_id)

    def peers(self) -> list[Record]:
        with self.store.connection() as db:
            return [
                self._decode(row)
                for row in db.execute(
                    "SELECT * FROM rex_peers WHERE state != 'retired' ORDER BY created LIMIT 128"
                )
            ]

    def retire(self, peer_id: str) -> Record:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            peer = self._peer(db, peer_id)
            if peer["state"] == "retired":
                return peer
            if db.execute(
                f"SELECT id FROM tasks WHERE target=? AND {_UNFINISHED}", (peer_id,)
            ).fetchone():
                raise ValueError("Rex peer has unfinished tasks; reconcile before retiring")
            db.execute(
                "UPDATE rex_peers SET state='retired', updated=? WHERE peer_id=?",
                (time.time(), peer_id),
            )
            return self._peer(db, peer_id)

    def invalidate(self, peer_id: str, detail: str) -> None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._peer(db, peer_id)
            now = time.time()
            db.execute(
                "UPDATE rex_peers SET state='stale', updated=? WHERE peer_id=? AND state='active'",
                (now, peer_id),
            )
            for row in db.execute(
                f"SELECT * FROM tasks WHERE target=? AND {_UNFINISHED}", (peer_id,)
            ).fetchall():
                task = self.store.decode(row, db)
                task["metadata"]["identity_invalidated"] = True
                db.execute(
                    "UPDATE tasks SET status='uncertain', metadata=?, detail=?, updated=? "
                    "WHERE id=?",
                    (json.dumps(task["metadata"]), detail, now, task["id"]),
                )

    def submit(self, peer_id: str, message: str, request_id: str) -> Task:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            peer = self._peer(db, peer_id)
            # Historical retries precede active-state checks and never redeliver work.
            existing = db.execute(
                "SELECT * FROM tasks WHERE request_key=?", (request_id,)
            ).fetchone()
            if existing is None:
                self._active(peer)
            elif (
                self.store.decode(existing, db)["metadata"].get("kind") != "rex"
                or self.store.decode(existing, db)["metadata"].get("peer_id") != peer_id
            ):
                raise ValueError("request_id already describes different work")
            task, _ = self.store.reserve_in_transaction(
                db,
                peer_id,
                message,
                request_id,
                {
                    "kind": "rex",
                    "peer_id": peer_id,
                    "binding": peer["binding"],
                    "completion": "cooperative_result",
                },
                initial_state="queued",
            )
            return public_task(task)

    def inbox(self, peer_id: str) -> Record:
        with self.store.connection() as db:
            db.execute("BEGIN")
            self._active(self._peer(db, peer_id))
            tasks = [
                self.store.decode(row, db)
                for row in db.execute(
                    f"SELECT * FROM tasks WHERE target=? AND {_UNFINISHED} ORDER BY created",
                    (peer_id,),
                )
            ]
            queued = [
                task
                for task in tasks
                if task["status"] == "queued"
                and not task["metadata"].get("acknowledged_by")
                and not task["metadata"].get("identity_invalidated")
            ]
            active = next(
                (public_task(task) for task in tasks if task["metadata"].get("acknowledged_by")),
                None,
            )
            return {"peer_id": peer_id, "tasks": queued, "active_task": active}

    def _task(self, db: sqlite3.Connection, peer_id: str, task_id: str) -> Task:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task ID")
        task = self.store.decode(row, db)
        if (
            task["target"] != peer_id
            or task["metadata"].get("peer_id") != peer_id
            or task["metadata"].get("kind") != "rex"
        ):
            raise ValueError("Task does not belong to this Rex peer")
        return task

    def acknowledge(self, peer_id: str, task_id: str) -> Task:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._active(self._peer(db, peer_id))
            task = self._task(db, peer_id, task_id)
            metadata = task["metadata"]
            if metadata.get("identity_invalidated") or task["status"] not in (
                "queued",
                "submitted",
            ):
                raise ValueError("Task cannot be acknowledged")
            if task["status"] == "submitted" and metadata.get("acknowledged_by") == peer_id:
                return public_task(task)
            if task["status"] != "queued" or metadata.get("acknowledged_by"):
                raise ValueError("Task acknowledgment is incompatible")
            now = time.time()
            metadata.update(acknowledged_by=peer_id, acknowledged_at=now)
            db.execute(
                "UPDATE tasks SET status='submitted', metadata=?, updated=? WHERE id=?",
                (json.dumps(metadata), now, task_id),
            )
            return public_task(self._task(db, peer_id, task_id))

    def complete(self, peer_id: str, task_id: str, result: str) -> Task:
        if not isinstance(result, str) or len(result) > 200000:
            raise ValueError("Result must be a string of at most 200000 characters")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            peer = self._peer(db, peer_id)
            task = self._task(db, peer_id, task_id)
            if task["metadata"].get("acknowledged_by") != peer_id:
                raise ValueError("Completion requires the matching acknowledgment")
            if task["status"] == "completed":
                if not content_matches(task, "result", result):
                    raise ValueError("Task already has a different completed result")
                return public_task(task)
            self._active(peer)
            if (
                task["metadata"].get("identity_invalidated")
                or task["status"] in TERMINAL
                or task["status"] != "submitted"
            ):
                raise ValueError("Task cannot be completed")
            stamped = ended_metadata_text(task["metadata"])
            if stamped is None:
                db.execute(
                    "UPDATE tasks SET status='completed', result=?, detail=NULL, updated=? "
                    "WHERE id=?",
                    (result, time.time(), task_id),
                )
            else:
                db.execute(
                    "UPDATE tasks SET status='completed', result=?, detail=NULL, updated=?, "
                    "metadata=? WHERE id=?",
                    (result, time.time(), stamped, task_id),
                )
            return public_task(self._task(db, peer_id, task_id))
