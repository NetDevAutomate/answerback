"""Transactional actor messages and broker receipts, never execution tasks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..transport import private_directory
from .model import (
    KINDS,
    MAX_BODY_BYTES,
    MAX_RECEIVE,
    ActorAuth,
    ActorEnvelope,
    ActorError,
    bounded_text,
    canonical,
    json_object,
    positive_integer,
    uuid_text,
)

MAX_RUNS = 8
MAX_MESSAGES = 256
# One sender's share of a run's messages, so one actor cannot use up the whole run.
MAX_MESSAGES_PER_SENDER = MAX_MESSAGES // 2
MAX_ROLES = 16


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _limit(value: Any, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")
    return value


class ActorStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError("Actor database must not be a symlink")
        with self.connection() as db:
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS actor_runs (
                    run_id TEXT PRIMARY KEY, project TEXT NOT NULL, roles TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL, state TEXT NOT NULL, roster_digest TEXT,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, sealed_at REAL,
                    finished_at REAL, reason TEXT NOT NULL DEFAULT '', owner_token_digest TEXT);
                CREATE TABLE IF NOT EXISTS actors (
                    run_id TEXT NOT NULL, actor_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    role TEXT NOT NULL, harness TEXT NOT NULL, credential_digest TEXT NOT NULL,
                    state TEXT NOT NULL, native_binding TEXT, binding_digest TEXT,
                    frontend_id TEXT, created_at REAL NOT NULL, activated_at REAL,
                    revoked_at REAL, reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(run_id, actor_id, epoch));
                CREATE UNIQUE INDEX IF NOT EXISTS actor_live_role
                    ON actors(run_id, role) WHERE state != 'revoked';
                CREATE UNIQUE INDEX IF NOT EXISTS actor_live_identity
                    ON actors(run_id, actor_id) WHERE state != 'revoked';
                CREATE UNIQUE INDEX IF NOT EXISTS actor_unique_credential
                    ON actors(credential_digest);
                CREATE TABLE IF NOT EXISTS actor_messages (
                    message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    sender_actor_id TEXT NOT NULL, sender_epoch INTEGER NOT NULL,
                    sender_frontend_id TEXT NOT NULL,
                    recipient_actor_id TEXT NOT NULL, recipient_epoch INTEGER NOT NULL,
                    request_id TEXT NOT NULL, kind TEXT NOT NULL, reply_to TEXT UNIQUE,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL, delivery TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    published_at REAL, delivered_at REAL, offered_at REAL,
                    acknowledged_at REAL, replied_at REAL, detail TEXT,
                    UNIQUE(run_id, sender_actor_id, sender_epoch, request_id));
                CREATE TABLE IF NOT EXISTS actor_outbox (
                    message_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, published_at REAL);
                CREATE TABLE IF NOT EXISTS actor_transport_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
                    kind TEXT NOT NULL, stream TEXT NOT NULL, broker_seq INTEGER NOT NULL,
                    observed_at REAL NOT NULL, duplicate INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS actor_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, epoch INTEGER NOT NULL, frontend_id TEXT NOT NULL,
                    kind TEXT NOT NULL, observed_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS actor_pending_inbox
                    ON actor_messages(run_id, recipient_actor_id, recipient_epoch, delivered_at);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(actor_runs)")}
            if "owner_token_digest" not in columns:
                db.execute("ALTER TABLE actor_runs ADD COLUMN owner_token_digest TEXT")
        self.path.chmod(0o600)

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
    def has_runs(path: Path) -> bool:
        """Probe existing optional state without creating a database or directory."""
        path = Path(path)
        if not path.exists():
            return False
        if path.is_symlink() or not path.is_file():
            raise ValueError("Actor database must be a regular file")
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
        try:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='actor_runs'"
            ).fetchone()
            return bool(exists and db.execute("SELECT 1 FROM actor_runs LIMIT 1").fetchone())
        finally:
            db.close()

    @staticmethod
    def _run(db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        uuid_text(run_id, "run_id")
        row = db.execute("SELECT * FROM actor_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown actor run")
        return row

    @staticmethod
    def _actor(db: sqlite3.Connection, run_id: str, actor_id: str, epoch: int) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM actors WHERE run_id=? AND actor_id=? AND epoch=?",
            (run_id, actor_id, epoch),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown actor epoch")
        return row

    @staticmethod
    def _message(db: sqlite3.Connection, message_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM actor_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown stored actor message")
        return row

    def _live_run(self, row: sqlite3.Row, *, sealed: bool) -> None:
        if row["expires_at"] <= self.clock():
            raise ActorError("run_expired")
        if row["state"] == "finished":
            raise ActorError("run_finished")
        if sealed and row["state"] != "sealed":
            raise ActorError("run_unsealed")

    @staticmethod
    def _credential_matches(stored_digest: str | None, credential: str) -> bool:
        return (
            stored_digest is not None
            and isinstance(credential, str)
            and re.fullmatch(r"[a-f0-9]{64}", credential) is not None
            and hmac.compare_digest(stored_digest, _digest(credential))
        )

    def _auth(
        self, db: sqlite3.Connection, auth: ActorAuth, *, sealed: bool, inspection: bool = False
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not isinstance(auth, ActorAuth):
            raise ValueError("Actor authentication is required")
        run = self._run(db, auth.run_id)
        if not inspection:
            self._live_run(run, sealed=sealed)
        actor = self._actor(db, auth.run_id, auth.actor_id, auth.epoch)
        if not self._credential_matches(actor["credential_digest"], auth.credential):
            raise ActorError("auth_invalid")
        if actor["state"] == "revoked" or (sealed and actor["state"] != "active"):
            raise ActorError("binding_invalid")
        if inspection:
            # Historical inspection proves participation, not model consumption.
            # It must neither claim nor replace the messaging frontend binding.
            return run, actor
        if actor["frontend_id"] is None:
            db.execute(
                "UPDATE actors SET frontend_id=? WHERE run_id=? AND actor_id=? AND epoch=?",
                (auth.frontend_id, auth.run_id, auth.actor_id, auth.epoch),
            )
            actor = self._actor(db, auth.run_id, auth.actor_id, auth.epoch)
        elif actor["frontend_id"] != auth.frontend_id:
            raise ActorError("frontend_mismatch")
        return run, actor

    def _require_run_access(
        self,
        db: sqlite3.Connection,
        run_id: str,
        owner_token: str | None,
        auth: ActorAuth | None,
    ) -> None:
        try:
            if auth is not None:
                if (
                    owner_token is not None
                    or not isinstance(auth, ActorAuth)
                    or auth.run_id != run_id
                ):
                    raise ActorError("auth_invalid")
                self._auth(db, auth, sealed=False, inspection=True)
            elif owner_token is None or not self._credential_matches(
                self._run(db, run_id)["owner_token_digest"], owner_token
            ):
                raise ActorError("auth_invalid")
        except ActorError:
            raise
        except (ValueError, TypeError, KeyError):
            raise ActorError("auth_invalid") from None

    def _bind_frontend(self, auth: ActorAuth) -> None:
        # Authentication pins the first frontend independently of whether its
        # later operation is valid. An operation rollback must not release it.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._auth(db, auth, sealed=False)

    @staticmethod
    def _public_actor(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "run_id",
                "actor_id",
                "epoch",
                "role",
                "harness",
                "state",
                "frontend_id",
                "created_at",
                "activated_at",
                "revoked_at",
                "reason",
            )
        } | {
            "native_binding": json.loads(row["native_binding"]) if row["native_binding"] else None,
            "native_binding_digest": row["binding_digest"],
        }

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "message_id",
                "run_id",
                "sender_actor_id",
                "sender_epoch",
                "sender_frontend_id",
                "recipient_actor_id",
                "recipient_epoch",
                "request_id",
                "kind",
                "reply_to",
                "delivery",
                "created_at",
                "expires_at",
                "published_at",
                "delivered_at",
                "offered_at",
                "acknowledged_at",
                "replied_at",
                "detail",
            )
        }

    def create_run(
        self,
        run_id: str,
        project: str,
        expected_roles: list[str],
        ttl_seconds: int = 3600,
        *,
        owner_token: str | None = None,
        auth: ActorAuth | None = None,
    ) -> dict[str, Any]:
        """Create a run and return its owner token once; retries require run proof."""
        uuid_text(run_id, "run_id")
        if not isinstance(project, str) or not Path(project).is_absolute() or "\0" in project:
            raise ValueError("Actor project must be an absolute path")
        project = str(Path(project).resolve())
        if len(project) > 4096:
            raise ValueError("Actor project path exceeds its bound")
        if not isinstance(expected_roles, list) or not 1 <= len(expected_roles) <= MAX_ROLES:
            raise ValueError("Expected roles exceed the supported bound")
        roles = sorted(bounded_text(role, "role", 64) for role in expected_roles)
        if len(set(roles)) != len(roles):
            raise ValueError("Expected roles must be unique")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("Actor run TTL must be 1–86400 seconds")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM actor_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                self._require_run_access(db, run_id, owner_token, auth)
                if (existing["project"], existing["roles"], existing["ttl_seconds"]) != (
                    project,
                    canonical(roles),
                    ttl_seconds,
                ):
                    raise ValueError("Run ID already has a different immutable definition")
                return self._status_snapshot(db, run_id, self.clock())
            else:
                now = self.clock()
                count = db.execute(
                    "SELECT count(*) FROM actor_runs WHERE state!='finished' AND expires_at>?",
                    (now,),
                ).fetchone()[0]
                if count >= MAX_RUNS:
                    raise ActorError("capacity")
                token = secrets.token_hex(32)
                db.execute(
                    "INSERT INTO actor_runs(run_id,project,roles,ttl_seconds,state,"
                    "created_at,expires_at,owner_token_digest) "
                    "VALUES(?,?,?,?,'staging',?,?,?)",
                    (
                        run_id,
                        project,
                        canonical(roles),
                        ttl_seconds,
                        now,
                        now + ttl_seconds,
                        _digest(token),
                    ),
                )
                return {**self._status_snapshot(db, run_id, now), "owner_token": token}

    def stage_actor(
        self, run_id: str, actor_id: str, role: str, harness: str, credential: str, epoch: int = 1
    ) -> dict[str, Any]:
        # Reuse the authentication scalar validator without storing a plaintext token.
        ActorAuth(run_id, actor_id, epoch, credential, str(uuid4()))
        bounded_text(role, "role", 64)
        bounded_text(harness, "harness", 64)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, run_id)
            self._live_run(run, sealed=False)
            old = db.execute(
                "SELECT * FROM actors WHERE run_id=? AND actor_id=? AND epoch=?",
                (run_id, actor_id, epoch),
            ).fetchone()
            if old is not None:
                if (
                    old["role"] != role
                    or old["harness"] != harness
                    or not hmac.compare_digest(old["credential_digest"], _digest(credential))
                ):
                    raise ValueError("Actor epoch already has a different immutable definition")
                return self._public_actor(old)
            if run["state"] != "staging":
                raise ValueError("Cannot change a sealed actor roster")
            if db.execute(
                "SELECT 1 FROM actors WHERE credential_digest=?", (_digest(credential),)
            ).fetchone():
                raise ActorError("credential_reused")
            if role not in json.loads(run["roles"]):
                raise ValueError("Actor role is not expected by this run")
            if db.execute(
                "SELECT 1 FROM actors WHERE run_id=? AND state!='revoked' "
                "AND (actor_id=? OR role=?)",
                (run_id, actor_id, role),
            ).fetchone():
                raise ValueError("An active actor already owns that identity or role")
            latest = db.execute(
                "SELECT max(epoch) FROM actors WHERE run_id=? AND actor_id=?", (run_id, actor_id)
            ).fetchone()[0]
            if latest is not None and epoch <= latest:
                raise ValueError("Re-enrollment must use a newer actor epoch")
            if (
                db.execute("SELECT count(*) FROM actors WHERE run_id=?", (run_id,)).fetchone()[0]
                >= 64
            ):
                raise ValueError("Actor enrollment history bound reached")
            db.execute(
                "INSERT INTO actors(run_id,actor_id,epoch,role,harness,credential_digest,"
                "state,created_at) "
                "VALUES(?,?,?,?,?,?,'staged',?)",
                (run_id, actor_id, epoch, role, harness, _digest(credential), self.clock()),
            )
            return self._public_actor(self._actor(db, run_id, actor_id, epoch))

    def activate_actor(
        self, run_id: str, actor_id: str, epoch: int, native_binding: dict[str, Any]
    ) -> dict[str, Any]:
        uuid_text(actor_id, "actor_id")
        positive_integer(epoch, "epoch")
        binding = json_object(native_binding, "Native binding", MAX_BODY_BYTES)
        if not native_binding:
            raise ValueError("Native binding must contain supervised evidence")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, run_id)
            self._live_run(run, sealed=False)
            actor = self._actor(db, run_id, actor_id, epoch)
            if actor["native_binding"] is not None:
                if actor["native_binding"] != binding:
                    raise ValueError("Native binding is immutable")
                return self._public_actor(actor)
            if run["state"] != "staging" or actor["state"] != "staged":
                raise ValueError("Only a staged actor can activate before sealing")
            db.execute(
                "UPDATE actors SET state='active',native_binding=?,binding_digest=?,activated_at=? "
                "WHERE run_id=? AND actor_id=? AND epoch=?",
                (binding, _digest(binding), self.clock(), run_id, actor_id, epoch),
            )
            return self._public_actor(self._actor(db, run_id, actor_id, epoch))

    def seal_run(
        self, run_id: str, *, owner_token: str | None = None, auth: ActorAuth | None = None
    ) -> dict[str, Any]:
        """Freeze the complete roster after authenticating access to its status."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_run_access(db, run_id, owner_token, auth)
            run = self._run(db, run_id)
            self._live_run(run, sealed=False)
            if run["state"] == "staging":
                actors = db.execute(
                    "SELECT * FROM actors WHERE run_id=? AND state!='revoked' ORDER BY role",
                    (run_id,),
                ).fetchall()
                if [row["role"] for row in actors] != json.loads(run["roles"]) or any(
                    row["state"] != "active" or not row["binding_digest"] for row in actors
                ):
                    raise ValueError(
                        "Every expected role requires an active complete native binding"
                    )
                roster = [
                    {
                        key: row[key]
                        for key in ("actor_id", "epoch", "role", "harness", "binding_digest")
                    }
                    for row in actors
                ]
                digest = _digest(
                    canonical({"run_id": run_id, "project": run["project"], "actors": roster})
                )
                db.execute(
                    "UPDATE actor_runs SET state='sealed',roster_digest=?,sealed_at=? "
                    "WHERE run_id=?",
                    (digest, self.clock(), run_id),
                )
            return self._status_snapshot(db, run_id, self.clock())

    def revoke_actor(
        self, run_id: str, actor_id: str, epoch: int, reason: str = ""
    ) -> dict[str, Any]:
        uuid_text(actor_id, "actor_id")
        positive_integer(epoch, "epoch")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("Invalid revocation reason")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._run(db, run_id)
            actor = self._actor(db, run_id, actor_id, epoch)
            if actor["state"] != "revoked":
                db.execute(
                    "UPDATE actors SET state='revoked',revoked_at=?,reason=? "
                    "WHERE run_id=? AND actor_id=? AND epoch=?",
                    (self.clock(), reason, run_id, actor_id, epoch),
                )
            return self._public_actor(self._actor(db, run_id, actor_id, epoch))

    def finish_run(
        self,
        run_id: str,
        reason: str = "",
        *,
        owner_token: str | None = None,
        auth: ActorAuth | None = None,
    ) -> dict[str, Any]:
        """Close enrollment/messaging; this is not a native task completion claim."""
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("Invalid run closure reason")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_run_access(db, run_id, owner_token, auth)
            db.execute(
                "UPDATE actor_runs SET state='finished',finished_at=?,reason=? "
                "WHERE run_id=? AND state!='finished'",
                (self.clock(), reason, run_id),
            )
            return self._status_snapshot(db, run_id, self.clock())

    def _insert(
        self,
        db: sqlite3.Connection,
        auth: ActorAuth,
        run: sqlite3.Row,
        recipient: sqlite3.Row,
        request_id: str,
        kind: str,
        body: dict[str, Any],
        reply_to: str | None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id
        ):
            raise ValueError("Invalid actor request_id")
        body_text = json_object(body, "Actor body")
        existing = db.execute(
            "SELECT * FROM actor_messages WHERE run_id=? AND sender_actor_id=? "
            "AND sender_epoch=? AND request_id=?",
            (auth.run_id, auth.actor_id, auth.epoch, request_id),
        ).fetchone()
        if existing is not None:
            old = ActorEnvelope.from_bytes(existing["payload"].encode())
            if (
                old.recipient_actor_id,
                old.recipient_epoch,
                old.kind,
                old.reply_to,
                canonical(old.body),
            ) != (recipient["actor_id"], recipient["epoch"], kind, reply_to, body_text):
                raise ValueError("Actor request_id already describes different work")
            return self._receipt(existing)
        if (
            db.execute(
                "SELECT count(*) FROM actor_messages WHERE run_id=?", (auth.run_id,)
            ).fetchone()[0]
            >= MAX_MESSAGES
        ):
            raise ValueError("Actor message capacity reached (256 per run)")
        if (
            db.execute(
                "SELECT count(*) FROM actor_messages WHERE run_id=? AND sender_actor_id=?",
                (auth.run_id, auth.actor_id),
            ).fetchone()[0]
            >= MAX_MESSAGES_PER_SENDER
        ):
            raise ActorError("capacity")
        now = self.clock()
        envelope = ActorEnvelope(
            1,
            str(uuid4()),
            auth.run_id,
            run["project"],
            auth.actor_id,
            auth.epoch,
            recipient["actor_id"],
            recipient["epoch"],
            run["roster_digest"],
            request_id,
            kind,
            body,
            reply_to,
            now,
            run["expires_at"],
        )
        payload = envelope.to_bytes().decode()
        db.execute(
            "INSERT INTO actor_messages(message_id,run_id,sender_actor_id,sender_epoch,"
            "sender_frontend_id,recipient_actor_id,recipient_epoch,request_id,kind,reply_to,payload,"
            "payload_hash,delivery,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
            (
                envelope.message_id,
                auth.run_id,
                auth.actor_id,
                auth.epoch,
                auth.frontend_id,
                recipient["actor_id"],
                recipient["epoch"],
                request_id,
                kind,
                reply_to,
                payload,
                _digest(payload),
                now,
                run["expires_at"],
            ),
        )
        db.execute(
            "INSERT INTO actor_outbox VALUES(?,?,?,NULL)", (envelope.message_id, payload, now)
        )
        return self._receipt(self._message(db, envelope.message_id))

    def send(
        self, auth: ActorAuth, to_actor: str, request_id: str, kind: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("Invalid actor message kind")
        if kind in {"reply", "review_reply"}:
            raise ValueError("Use reply to derive a correlated recipient")
        uuid_text(to_actor, "recipient actor")
        self._bind_frontend(auth)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run, sender = self._auth(db, auth, sealed=True)
            self._allow_kind(sender, kind)
            recipient = db.execute(
                "SELECT * FROM actors WHERE run_id=? AND actor_id=? AND state='active'",
                (auth.run_id, to_actor),
            ).fetchone()
            if recipient is None or to_actor == auth.actor_id:
                raise ValueError("Recipient must be a different active actor in this run")
            return self._insert(db, auth, run, recipient, request_id, kind, body, None)

    @staticmethod
    def _allow_kind(sender: sqlite3.Row, kind: str) -> None:
        coordinator = sender["role"] == "coordinator"
        if (kind in {"assignment", "correction", "finish"} and not coordinator) or (
            kind in {"review_request", "review_reply"} and coordinator
        ):
            raise ActorError("role_forbidden")

    @staticmethod
    def _nonce(auth: ActorAuth, message_id: str) -> str:
        content = f"actor-offer-v1:{auth.run_id}:{auth.actor_id}:{auth.epoch}:{message_id}"
        return hmac.new(
            bytes.fromhex(auth.credential), content.encode(), hashlib.sha256
        ).hexdigest()

    def receive(self, auth: ActorAuth, limit: int = MAX_RECEIVE) -> dict[str, Any]:
        _limit(limit, MAX_RECEIVE)
        self._bind_frontend(auth)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run, actor = self._auth(db, auth, sealed=False)
            result: dict[str, Any] = {
                "run_id": auth.run_id,
                "actor_id": auth.actor_id,
                "run_state": run["state"],
                "messages": [],
            }
            if run["state"] != "sealed":
                return result
            if actor["state"] != "active":
                raise ValueError("Actor is not active")
            rows = db.execute(
                "SELECT * FROM actor_messages WHERE run_id=? AND recipient_actor_id=? "
                "AND recipient_epoch=? AND delivered_at IS NOT NULL AND acknowledged_at IS NULL "
                "AND expires_at>? ORDER BY delivered_at,message_id LIMIT ?",
                (auth.run_id, auth.actor_id, auth.epoch, self.clock(), limit),
            ).fetchall()
            for row in rows:
                if row["offered_at"] is None:
                    now = self.clock()
                    db.execute(
                        "UPDATE actor_messages SET offered_at=? WHERE message_id=?",
                        (now, row["message_id"]),
                    )
                    self._consumption(db, auth, row["message_id"], "offered", now)
                result["messages"].append(
                    {
                        **json.loads(row["payload"]),
                        "receipt_nonce": self._nonce(auth, row["message_id"]),
                    }
                )
            return result

    @staticmethod
    def _consumption(
        db: sqlite3.Connection, auth: ActorAuth, message_id: str, kind: str, now: float
    ) -> None:
        db.execute(
            "INSERT INTO actor_receipts(message_id,actor_id,epoch,frontend_id,kind,observed_at) "
            "VALUES(?,?,?,?,?,?)",
            (message_id, auth.actor_id, auth.epoch, auth.frontend_id, kind, now),
        )

    @staticmethod
    def _recipient(row: sqlite3.Row, auth: ActorAuth) -> None:
        if (row["run_id"], row["recipient_actor_id"], row["recipient_epoch"]) != (
            auth.run_id,
            auth.actor_id,
            auth.epoch,
        ):
            raise ValueError("Message belongs to a different recipient")

    def acknowledge(self, auth: ActorAuth, message_id: str, receipt_nonce: str) -> dict[str, Any]:
        self._bind_frontend(auth)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._auth(db, auth, sealed=True)
            row = self._message(db, message_id)
            self._recipient(row, auth)
            if row["offered_at"] is None:
                raise ValueError("Message must be offered before acknowledgement")
            if (
                not isinstance(receipt_nonce, str)
                or re.fullmatch(r"[a-f0-9]{64}", receipt_nonce) is None
                or not hmac.compare_digest(self._nonce(auth, message_id), receipt_nonce)
            ):
                raise ValueError("Invalid message receipt nonce")
            if row["acknowledged_at"] is None:
                now = self.clock()
                db.execute(
                    "UPDATE actor_messages SET acknowledged_at=? WHERE message_id=?",
                    (now, message_id),
                )
                self._consumption(db, auth, message_id, "acknowledged", now)
            return self._receipt(self._message(db, message_id))

    def reply(
        self, auth: ActorAuth, reply_to: str, request_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self._bind_frontend(auth)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run, sender = self._auth(db, auth, sealed=True)
            original = self._message(db, reply_to)
            self._recipient(original, auth)
            if original["acknowledged_at"] is None:
                raise ValueError("Reply requires prior acknowledgement")
            if original["reply_to"] is not None:
                raise ValueError("Replies require an original request")
            kind = "review_reply" if original["kind"] == "review_request" else "reply"
            self._allow_kind(sender, kind)
            recipient = self._actor(
                db, auth.run_id, original["sender_actor_id"], original["sender_epoch"]
            )
            if recipient["state"] != "active":
                raise ValueError("Original sender is no longer active")
            existing = db.execute(
                "SELECT * FROM actor_messages WHERE reply_to=?", (reply_to,)
            ).fetchone()
            if existing is not None and existing["request_id"] != request_id:
                raise ValueError("Request already has a different reply")
            result = self._insert(
                db,
                auth,
                run,
                recipient,
                request_id,
                kind,
                body,
                reply_to,
            )
            if original["replied_at"] is None:
                now = self.clock()
                db.execute(
                    "UPDATE actor_messages SET replied_at=? WHERE message_id=?", (now, reply_to)
                )
                self._consumption(db, auth, reply_to, "replied", now)
            return result

    @staticmethod
    def _broker_observation(stream: str, seq: int) -> None:
        bounded_text(stream, "broker stream", 128)
        positive_integer(seq, "broker sequence")

    def _transport_observation(
        self,
        db: sqlite3.Connection,
        message_id: str,
        kind: str,
        stream: str,
        seq: int,
        duplicate: bool,
    ) -> None:
        db.execute(
            "INSERT INTO actor_transport_observations(message_id,kind,stream,broker_seq,"
            "observed_at,duplicate) "
            "VALUES(?,?,?,?,?,?)",
            (message_id, kind, stream, seq, self.clock(), int(duplicate)),
        )

    def _ineligible(self, db: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, str] | None:
        run = self._run(db, row["run_id"])
        if row["expires_at"] <= self.clock() or run["expires_at"] <= self.clock():
            return "expired", "Actor message or run expired"
        if run["state"] != "sealed":
            return "rejected", "Actor run is not sealed or is finished"
        for side in ("sender", "recipient"):
            actor = self._actor(db, row["run_id"], row[f"{side}_actor_id"], row[f"{side}_epoch"])
            if actor["state"] != "active":
                return "rejected", f"Actor {side} epoch is revoked or inactive"
        return None

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit, 100)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT m.* FROM actor_outbox o JOIN actor_messages m USING(message_id) "
                "WHERE o.published_at IS NULL AND m.delivery NOT IN ('expired','rejected') "
                "ORDER BY o.created_at,o.message_id"
            ).fetchall()
            result = []
            for row in rows:
                ineligible = self._ineligible(db, row)
                if ineligible is not None:
                    if row["delivered_at"] is None:
                        db.execute(
                            "UPDATE actor_messages SET delivery=?,detail=? WHERE message_id=?",
                            (*ineligible, row["message_id"]),
                        )
                    continue
                result.append(
                    {
                        "message_id": row["message_id"],
                        "run_id": row["run_id"],
                        "payload": row["payload"].encode(),
                    }
                )
                if len(result) == limit:
                    break
            return result

    def mark_published(self, message_id: str, stream: str, seq: int) -> dict[str, Any]:
        self._broker_observation(stream, seq)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._message(db, message_id)
            duplicate = row["published_at"] is not None
            now = self.clock()
            db.execute(
                "UPDATE actor_outbox SET published_at=COALESCE(published_at,?) WHERE message_id=?",
                (now, message_id),
            )
            db.execute(
                "UPDATE actor_messages SET published_at=COALESCE(published_at,?),"
                "delivery=CASE WHEN delivery='queued' THEN 'published' ELSE delivery END "
                "WHERE message_id=?",
                (now, message_id),
            )
            self._transport_observation(db, message_id, "published", stream, seq, duplicate)
            return self._receipt(self._message(db, message_id))

    def deliver(self, raw: bytes, stream: str, seq: int) -> dict[str, Any]:
        self._broker_observation(stream, seq)
        envelope = ActorEnvelope.from_bytes(raw)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._message(db, envelope.message_id)
            if row["payload"].encode() != raw or _digest(row["payload"]) != row["payload_hash"]:
                raise ValueError("Actor broker payload differs from stored immutable message")
            run = self._run(db, envelope.run_id)
            if (run["project"], run["roster_digest"]) != (envelope.project, envelope.roster_digest):
                raise ValueError("Actor payload no longer matches its frozen run binding")
            duplicate = row["delivered_at"] is not None or bool(
                db.execute(
                    "SELECT 1 FROM actor_transport_observations WHERE message_id=? "
                    "AND kind!='published' LIMIT 1",
                    (envelope.message_id,),
                ).fetchone()
            )
            delivery = row["delivery"]
            if not duplicate:
                ineligible = self._ineligible(db, row)
                if ineligible is not None:
                    delivery, detail = ineligible
                    db.execute(
                        "UPDATE actor_messages SET delivery=?,detail=? WHERE message_id=?",
                        (delivery, detail, row["message_id"]),
                    )
                else:
                    delivery = "delivered"
                    db.execute(
                        "UPDATE actor_messages SET delivery='delivered',delivered_at=? "
                        "WHERE message_id=?",
                        (self.clock(), row["message_id"]),
                    )
            self._transport_observation(db, envelope.message_id, delivery, stream, seq, duplicate)
            return {"message_id": envelope.message_id, "delivery": delivery, "duplicate": duplicate}

    def _status_snapshot(self, db: sqlite3.Connection, run_id: str, now: float) -> dict[str, Any]:
        row = self._run(db, run_id)
        counts = {
            item["delivery"]: item["n"]
            for item in db.execute(
                "SELECT delivery,count(*) AS n FROM actor_messages WHERE run_id=? "
                "GROUP BY delivery",
                (run_id,),
            )
        }
        result = {
            key: row[key]
            for key in (
                "run_id",
                "project",
                "state",
                "roster_digest",
                "created_at",
                "expires_at",
                "sealed_at",
                "finished_at",
                "reason",
            )
        }
        if row["state"] != "finished" and row["expires_at"] <= now:
            result["state"] = "expired"
        return {**result, "expected_roles": json.loads(row["roles"]), "messages": counts}

    def status(
        self, run_id: str, *, owner_token: str | None = None, auth: ActorAuth | None = None
    ) -> dict[str, Any]:
        """Inspect a run with its owner token or a nonrevoked participant credential."""
        with self.connection() as db:
            db.execute("BEGIN")
            self._require_run_access(db, run_id, owner_token, auth)
            return self._status_snapshot(db, run_id, self.clock())

    def _roster_snapshot(self, db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = self._run(db, run_id)
        actors = db.execute(
            "SELECT * FROM actors WHERE run_id=? ORDER BY role,epoch", (run_id,)
        ).fetchall()
        return {
            "run_id": run_id,
            "project": row["project"],
            "roster_digest": row["roster_digest"],
            "actors": [self._public_actor(actor) for actor in actors],
        }

    def roster(
        self, run_id: str, *, owner_token: str | None = None, auth: ActorAuth | None = None
    ) -> dict[str, Any]:
        """Read native bindings after proving run ownership or participation."""
        with self.connection() as db:
            db.execute("BEGIN")
            self._require_run_access(db, run_id, owner_token, auth)
            return self._roster_snapshot(db, run_id)

    def export(
        self, run_id: str, *, owner_token: str | None = None, auth: ActorAuth | None = None
    ) -> dict[str, Any]:
        """Export one authenticated snapshot, including historical run evidence."""
        with self.connection() as db:
            # Read transactions keep every exported projection on the same
            # snapshot, including when WAL permits concurrent message commits.
            db.execute("BEGIN")
            self._require_run_access(db, run_id, owner_token, auth)
            run = self._status_snapshot(db, run_id, self.clock())
            roster = self._roster_snapshot(db, run_id)
            messages = db.execute(
                "SELECT * FROM actor_messages WHERE run_id=? ORDER BY created_at,message_id",
                (run_id,),
            ).fetchall()
            observations = db.execute(
                "SELECT o.* FROM actor_transport_observations o "
                "JOIN actor_messages m USING(message_id) "
                "WHERE m.run_id=? ORDER BY o.id",
                (run_id,),
            ).fetchall()
            receipts = db.execute(
                "SELECT r.* FROM actor_receipts r JOIN actor_messages m USING(message_id) "
                "WHERE m.run_id=? ORDER BY r.id",
                (run_id,),
            ).fetchall()
        return {
            "run": run,
            "roster": roster,
            "messages": [
                {"envelope": json.loads(row["payload"]), **self._receipt(row)} for row in messages
            ],
            "transport_observations": [dict(row) for row in observations],
            "receipts": [dict(row) for row in receipts],
        }
