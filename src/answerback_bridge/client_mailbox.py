"""Durable, addressed notifications. Polling is not execution or acknowledgement."""

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import CLIENT_NAMES
from .retention import content_identity, migrate_content, read_record
from .transport import private_directory

MAX_PENDING_PER_CLIENT = 150
MAX_PENDING_NOTIFICATIONS = 1000


class ClientMailbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Client mailbox database must not be a symlink")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, "
                "request_id TEXT NOT NULL, topic TEXT NOT NULL, message TEXT NOT NULL, "
                "created REAL NOT NULL, acknowledged REAL, UNIQUE(client,request_id))"
            )
            migrate_content(db, "messages")
        path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def notify(
        self, client: str, message: str, request_id: str, topic: str = "notification"
    ) -> dict[str, Any]:
        if client not in CLIENT_NAMES:
            raise ValueError("Unknown client")
        if not isinstance(message, str) or not 1 <= len(message) <= 16000:
            raise ValueError("Notification must contain 1–16000 characters")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Notification requires a request ID of at most 128 characters")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", topic):
            raise ValueError("Invalid notification topic")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM messages WHERE client=? AND request_id=?", (client, request_id)
            ).fetchone()
            if existing is not None:
                retained = read_record(db, "messages", existing["id"])
                matches = (
                    existing["message"] == message
                    if retained is None
                    else retained["fields"]["message"] == content_identity(message)
                )
                if not matches or existing["topic"] != topic:
                    raise ValueError("Request ID already belongs to a different notification")
                return {
                    "id": existing["id"],
                    "client": client,
                    "status": "acknowledged" if existing["acknowledged"] is not None else "queued",
                }
            if (
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE acknowledged IS NULL AND client=?",
                    (client,),
                ).fetchone()[0]
                >= MAX_PENDING_PER_CLIENT
            ):
                raise ValueError("Notification inbox capacity reached for this client")
            if (
                db.execute("SELECT COUNT(*) FROM messages WHERE acknowledged IS NULL").fetchone()[0]
                >= MAX_PENDING_NOTIFICATIONS
            ):
                raise ValueError("Notification inbox capacity reached")
            cursor = db.execute(
                "INSERT INTO messages(client,request_id,topic,message,created) VALUES(?,?,?,?,?)",
                (client, request_id, topic, message, time.time()),
            )
            return {"id": cursor.lastrowid, "client": client, "status": "queued"}

    def pending(self, client: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id,topic,message,created FROM messages "
                "WHERE client=? AND acknowledged IS NULL ORDER BY id LIMIT 101",
                (client,),
            ).fetchall()
        return {"messages": [dict(row) for row in rows[:100]], "has_more": len(rows) > 100}

    def status(self, client: str, request_id: str) -> dict[str, Any]:
        """Project one stored notice without fetching content or changing its state."""
        if client not in CLIENT_NAMES:
            raise ValueError("Unknown client")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Notification requires a request ID of at most 128 characters")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id,client,request_id,topic,created,acknowledged FROM messages "
                "WHERE client=? AND request_id=?",
                (client, request_id),
            ).fetchone()
        common = {
            "client": client,
            "request_id": request_id,
            "delivery_mode": "passive_inbox",
            "wakes_agent": False,
            "acknowledgement_scope": "client_inbox",
            "task_id": None,
        }
        if row is None:
            return {**common, "found": False, "status": "not_found"}
        return {
            **common,
            "found": True,
            "id": row["id"],
            "topic": row["topic"],
            "created_at": row["created"],
            "client_acknowledged_at": row["acknowledged"],
            "status": "acknowledged" if row["acknowledged"] is not None else "queued",
        }

    def acknowledge(self, client: str, ids: list[int]) -> dict[str, Any]:
        if len(ids) > 100 or any(type(value) is not int or value < 1 for value in ids):
            raise ValueError("Acknowledge at most 100 valid notification IDs")
        if not ids:
            return {"acknowledged": []}
        with closing(self._connect()) as db, db:
            placeholders = ",".join("?" for _ in ids)
            owned = [
                row["id"]
                for row in db.execute(
                    f"SELECT id FROM messages WHERE client=? AND id IN ({placeholders})",
                    (client, *ids),
                )
            ]
            db.executemany(
                "UPDATE messages SET acknowledged=? WHERE client=? AND id=? "
                "AND acknowledged IS NULL",
                [(time.time(), client, ident) for ident in owned],
            )
        return {"acknowledged": owned}
