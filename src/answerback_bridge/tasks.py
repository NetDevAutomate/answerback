"""Durable task receipts. A reservation precedes every external submission."""

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import receipt_version
from .adapters.catalog import definition, definitions
from .digests import canonical
from .errors import AdmissionRefused
from .retention import (
    content_matches,
    decode_task,
    matches_work,
    migrate_subjects,
    migrate_tasks,
    read_record,
)
from .subjects import SUBJECT_REFUSAL, validate_subject

if TYPE_CHECKING:
    from .plugin import StoreHooks

TERMINAL = ("completed", "failed", "abandoned", "cancelled")
STATES = ("pending", "queued", "submitted", "running", "blocked", "uncertain", *TERMINAL)
Task = dict[str, Any]
# How a result was authenticated, recorded once with the completion it describes.
# A remote Core result file proves only that the brief reached the listed machine's
# OS account: any program of that account can write it (docs/RECIPIENTS.md).
TRUSTED_OS_ACCOUNT = "trusted_os_account"
# Keys that never change once a receipt holds them. `result_machine` is set when the
# receipt is reserved; the other two when a remote result file completes it.
RESULT_PROVENANCE_KEYS = ("result_authentication", "result_machine", "result_account")
# Provenance of a result held until delivery is verified, like `provisional_result`.
PROVISIONAL_PROVENANCE = "provisional_provenance"
MAX_PERMISSION_EVENTS = 128
MAX_PERMISSION_TOOL_CALL_ID = 512


def _refuse_provenance_change(metadata: Mapping[str, Any], changes: Mapping[str, Any]) -> None:
    """Raise ValueError if `changes` would alter a provenance key the receipt holds."""
    for key in RESULT_PROVENANCE_KEYS:
        if key in changes and key in metadata and changes[key] != metadata[key]:
            raise ValueError(f"Task result provenance is immutable: {key}")


class StoreHookConflict(RuntimeError):
    """Two plugins' store hooks claim one receipt: a composition error, never a receipt state."""


class StoreHookRegistry:
    """The store hooks of the loaded plugins, one per receipt kind.

    ``build_adapters`` registers each plugin's hooks under its kind (the receipt's
    ``metadata["kind"]``); registering a kind again replaces its hooks, so rebuilding the
    adapters of one store is idempotent. ``claimant`` is the only way a receipt reaches a
    provider's ``reconcile``: exactly one plugin may claim it.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, StoreHooks] = {}

    def register(self, kind: str, hooks: "StoreHooks") -> None:
        if not isinstance(kind, str) or not kind:
            raise ValueError("Store hooks need the receipt kind they serve")
        self._hooks[kind] = hooks

    def kinds(self) -> tuple[str, ...]:
        return tuple(self._hooks)

    def get(self, kind: Any) -> "StoreHooks | None":
        return self._hooks.get(kind) if isinstance(kind, str) else None

    def claimant(self, task: Task) -> "StoreHooks | None":
        """The one hooks object whose ``is_reconcilable`` accepts ``task``, or None.

        Raises ``StoreHookConflict`` when more than one does: the kernel never picks.
        """
        claimants = [kind for kind, hooks in self._hooks.items() if hooks.is_reconcilable(task)]
        if len(claimants) > 1:
            raise StoreHookConflict(
                f"Receipt {task.get('id')} is claimed by the store hooks of "
                f"{', '.join(sorted(claimants))}; exactly one plugin may reconcile a receipt"
            )
        return self._hooks[claimants[0]] if claimants else None

    def is_reconcilable(self, task: Task) -> bool:
        return self.claimant(task) is not None

    def project_of(self, metadata: Mapping[str, Any]) -> str | None:
        """The project the receipt's own provider reads from its metadata; None otherwise.

        A kind without hooks (no plugin loaded for it) has an unknown project.
        """
        hooks = self.get(metadata.get("kind"))
        return None if hooks is None else hooks.project_of(metadata)


def ended_metadata_text(metadata: Any) -> str | None:
    """The stamped metadata text for an ending that did not write the column before.

    None means "leave the column as it is": the stored value is not a JSON object, or the
    stamp could not be prepared. The stamp never stops or changes an ending.
    """
    try:
        if not isinstance(metadata, dict):
            return None
        return json.dumps(receipt_version.stamp_ended(metadata), allow_nan=False)
    except Exception:
        return None


def same_but_ending_version(one: Task, other: Any) -> bool:
    """Equal receipts, the ending version key of their metadata apart."""
    if not isinstance(other, dict):
        return False
    return {**one, "metadata": receipt_version.without_ended(one.get("metadata"))} == {
        **other,
        "metadata": receipt_version.without_ended(other.get("metadata")),
    }


class TaskStore:
    def __init__(self, path: Path) -> None:
        from .transport import private_directory

        self.path = path
        # The store hooks of the loaded plugins; `build_adapters` fills it. A bare store
        # (tests, tools) has none and reconciles nothing through `reconcile`.
        self.hooks = StoreHookRegistry()
        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Task database must not be a symlink")
        with self.connection() as db:
            # Serialize schema inspection and migration across simultaneous clients.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    target TEXT NOT NULL, message TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    metadata TEXT NOT NULL, result TEXT, detail TEXT, turn_id TEXT
                )"""
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_sessions ("
                "alias TEXT PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT NOT NULL)"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(worker_sessions)")}
            if "profile" not in columns:
                db.execute("ALTER TABLE worker_sessions ADD COLUMN profile TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_sessions ("
                "provider TEXT NOT NULL, session_id TEXT NOT NULL, alias TEXT NOT NULL, "
                "cwd TEXT NOT NULL, profile TEXT NOT NULL, "
                "PRIMARY KEY(provider, session_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS crew_creation_receipts ("
                "request_key TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "project TEXT NOT NULL, identity TEXT NOT NULL)"
            )
            # The permanent `hc machine ls` id of each machine at its first verified
            # send (T-226). A later send to that name with another id is refused.
            db.execute(
                "CREATE TABLE IF NOT EXISTS hc_machine_pins ("
                "machine TEXT PRIMARY KEY, id TEXT NOT NULL, pinned REAL NOT NULL)"
            )
            migrate_tasks(db)
            migrate_subjects(db)
        path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def native_session_owner(self, provider: str, session_id: str) -> dict[str, str] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM native_sessions WHERE provider=? AND session_id=?",
                (provider, session_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def claim_native_session(
        self, provider: str, session_id: str, alias: str, cwd: str, profile: str
    ) -> None:
        """Retain ownership independently of removable aliases and response retention."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT alias,cwd,profile FROM native_sessions WHERE provider=? AND session_id=?",
                (provider, session_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (alias, cwd, profile):
                    raise ValueError("Native session already has a different owner or profile")
                return
            db.execute(
                "INSERT INTO native_sessions VALUES (?,?,?,?,?)",
                (provider, session_id, alias, cwd, profile),
            )

    def bind_native_turn(self, task_id: str, session_id: str, turn_id: str) -> None:
        """Keep a validated turn identity even when the final response is later lost."""
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 512:
            raise ValueError("Native turn identity is invalid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            metadata = json.loads(row["metadata"])
            if metadata.get("session_id") != session_id:
                raise ValueError("Native turn belongs to another session")
            previous = metadata.get("native_turn_id")
            if previous is not None and previous != turn_id:
                raise ValueError("Native turn identity changed during the task")
            metadata["native_turn_id"] = turn_id
            db.execute("UPDATE tasks SET metadata=? WHERE id=?", (json.dumps(metadata), task_id))

    @staticmethod
    def decode(row: sqlite3.Row, db: sqlite3.Connection) -> Task:
        return decode_task(row, db)

    def reserve(
        self,
        target: str,
        message: str,
        request_key: str,
        metadata: dict[str, Any] | None = None,
        *,
        subject: str | None = None,
    ) -> tuple[Task, bool]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.reserve_in_transaction(
                db, target, message, request_key, metadata, subject=subject
            )

    def reserve_in_transaction(
        self,
        db: sqlite3.Connection,
        target: str,
        message: str,
        request_key: str,
        metadata: dict[str, Any] | None = None,
        *,
        initial_state: str = "pending",
        subject: str | None = None,
    ) -> tuple[Task, bool]:
        """Reserve in the caller's begun transaction; never commit independently.

        `subject` is the submission's subject (`Submission.subject`), stored on a new
        receipt; None stores none. A repeat with another subject is different work.
        """
        if not db.in_transaction:
            raise ValueError("Task reservation requires a begun transaction")
        if initial_state not in ("pending", "queued"):
            raise ValueError("Invalid task reservation initial state")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key):
            raise ValueError("request_id must be 1–128 letters, digits, dots, colons, _ or -")
        if not message.strip() or len(message) > 100_000:
            raise ValueError("Message must contain 1–100000 characters")
        if receipt_version.names_key(metadata):
            raise ValueError(receipt_version.REFUSAL)
        subject = validate_subject(subject)
        existing = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
        if existing:
            task = self.decode(existing, db)
            if not matches_work(
                task, target, message, request_key, (metadata or {}).get("kind"), subject=subject
            ):
                raise ValueError("request_id already describes different work")
            return task, False
        busy = db.execute(
            "SELECT id FROM tasks WHERE target=? "
            "AND status NOT IN ('completed', 'failed', 'abandoned', 'cancelled')",
            (target,),
        ).fetchone()
        if busy:
            raise AdmissionRefused(busy["id"], target=target)
        task_id = str(uuid.uuid4())
        now = time.time()
        stored = metadata or {}
        if isinstance(stored, dict):
            stored = receipt_version.stamp_created(stored)
        db.execute(
            "INSERT INTO tasks (id,request_key,target,message,status,created,updated,"
            "metadata,result,detail,turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                task_id,
                request_key,
                target,
                message,
                initial_state,
                now,
                now,
                json.dumps(stored),
            ),
        )
        if subject is not None:
            db.execute(
                "INSERT INTO task_subjects (task_id, subject) VALUES (?, ?)", (task_id, subject)
            )
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return self.decode(row, db), True

    def by_request(self, request_key: str) -> Task | None:
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
            return self.decode(row, db) if row is not None else None

    def get(self, task_id: str) -> Task:
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            return self.decode(row, db)

    def recover_pending(self, policies: Mapping[str, tuple[str, ...]] | None = None) -> int:
        """Recover interrupted owners according to registered lifecycle policy."""
        policies = (
            {entry.name: entry.recovery_states for entry in definitions()}
            if policies is None
            else policies
        )
        predicates = ["status='pending'"]
        parameters: list[str] = []
        for provider, states in policies.items():
            if not states:
                continue
            if any(state not in STATES or state in TERMINAL for state in states):
                raise ValueError("Invalid provider recovery policy")
            predicates.append(
                "(json_extract(metadata, '$.kind')=? AND status IN ("
                + ",".join("?" for _ in states)
                + "))"
            )
            parameters.extend((provider, *states))
        with self.connection() as db:
            result = db.execute(
                "UPDATE tasks SET status='uncertain', updated=?, detail=? WHERE "
                + " OR ".join(predicates),
                (
                    time.time(),
                    "Bridge stopped during submission; inspect before abandoning",
                    *parameters,
                ),
            )
            return result.rowcount

    def machine_pin(self, machine: str) -> str | None:
        """The core id pinned for an hc machine name, or None before its first send."""
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM hc_machine_pins WHERE machine=?", (machine,)
            ).fetchone()
            return None if row is None else row["id"]

    def pin_machine(self, machine: str, ident: str) -> str:
        """Pin `ident` for `machine` unless a pin exists; return the pinned id.

        An existing pin is never replaced here. A person clears one by deleting its
        row (docs/HERDR-INTEGRATION.md, "Machine identity pin").
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO hc_machine_pins (machine, id, pinned) VALUES (?, ?, ?)",
                (machine, ident, time.time()),
            )
            row = db.execute(
                "SELECT id FROM hc_machine_pins WHERE machine=?", (machine,)
            ).fetchone()
            return row["id"]

    def counts(self) -> dict[str, int]:
        with self.connection() as db:
            return dict(db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())

    def reconcilable_ids(self, limit: int = 4) -> list[str]:
        """Compatibility query for the original Codex history reader."""
        return self.observation_ids("desktop", "last_history_attempt", limit)

    def observation_ids(
        self,
        provider: str,
        attempt_key: str,
        limit: int = 4,
        *,
        result_machines: frozenset[str] | None = None,
    ) -> list[str]:
        """Unfinished receipts of `provider`, least recently attempted first.

        `result_machines` keeps only Core receipts that a remote result file can
        still complete on one of those machines: a stored token, `submitted` or
        `uncertain` after verified delivery, not invalidated, and not both refused
        (`result_refused`) and classified ended (`terminal_ended`). A refused file
        whose terminal has not been classified ended stays, so that its terminal is
        still checked; `CoreAdapter._unreadable` applies the same rule.
        Receipts outside the filter can then never fill the `limit`.
        """
        if not 1 <= limit <= 100 or not re.fullmatch(r"[a-z_]+", attempt_key):
            raise ValueError("Invalid observation query")
        machines = sorted(result_machines) if result_machines is not None else None
        if machines is not None and not machines:
            return []
        readable = (
            ""
            if machines is None
            else (
                "AND json_type(metadata, '$.result_token')='text' "
                "AND json_extract(metadata, '$.result_machine') IN ("
                + ",".join("?" for _ in machines)
                + ") AND (status='submitted' OR (status='uncertain' "
                "AND json_extract(metadata, '$.delivery_verified')=1)) "
                "AND COALESCE(json_extract(metadata, '$.identity_invalidated'), 0)=0 "
                "AND NOT (COALESCE(json_extract(metadata, '$.terminal_ended'), 0)=1 "
                "AND COALESCE(json_extract(metadata, '$.result_refused'), 0)=1) "
            )
        )
        with self.connection() as db:
            rows = db.execute(
                "SELECT id FROM tasks WHERE "
                "status NOT IN ('completed', 'failed', 'abandoned', 'cancelled') "
                "AND json_extract(metadata, '$.kind')=? "
                + readable
                + "ORDER BY COALESCE(json_extract(metadata, ?), 0), created LIMIT ?",
                (provider, *(machines or ()), "$." + attempt_key, limit),
            ).fetchall()
            return [row["id"] for row in rows]

    def unlisted_result_receipts(
        self, machines: frozenset[str], limit: int = 20
    ) -> list[dict[str, str]]:
        """Open remote receipts whose result machine is not in `machines`.

        These are `submitted` or `uncertain` Core receipts with a result file that
        nothing reads any more, because their machine was removed from
        `hc_send_machines` before they finished. Each entry holds `id`, `machine`
        and `status`, oldest first, at most `limit` of them.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Invalid receipt query")
        listed = sorted(machines)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, status, json_extract(metadata, '$.result_machine') AS machine "
                "FROM tasks WHERE status IN ('submitted', 'uncertain') "
                "AND json_extract(metadata, '$.kind')='hc' "
                "AND json_type(metadata, '$.result_token')='text' "
                "AND json_type(metadata, '$.result_machine')='text' "
                "AND json_extract(metadata, '$.result_machine') NOT IN ("
                + ",".join("?" for _ in listed)
                + ") ORDER BY created LIMIT ?",
                (*listed, limit),
            ).fetchall()
        return [
            {"id": row["id"], "machine": row["machine"], "status": row["status"]} for row in rows
        ]

    def record_permission_round(
        self,
        task_id: str,
        *,
        permission_id: str,
        tool_call_id: str | None,
        option_id: str,
        requested_at: float,
        answered_at: float,
        ordinal: int,
    ) -> Task:
        """Append one resolved permission fact without retaining request content."""
        if (
            not isinstance(permission_id, str)
            or not permission_id
            or not isinstance(tool_call_id, str | None)
            or not isinstance(option_id, str)
            or not option_id
            or type(requested_at) is not float
            or type(answered_at) is not float
            or type(ordinal) is not int
            or ordinal < 1
        ):
            raise ValueError("Permission receipt fact is invalid")
        if tool_call_id is not None and len(tool_call_id) > MAX_PERMISSION_TOOL_CALL_ID:
            tool_call_id = tool_call_id[: MAX_PERMISSION_TOOL_CALL_ID - 1] + "…"
        fact = {
            "permission_id": permission_id,
            "toolCallId": tool_call_id,
            "option_id": option_id,
            "requested_at": requested_at,
            "answered_at": answered_at,
            "ordinal": ordinal,
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            metadata = json.loads(row["metadata"])
            events = metadata.setdefault("permission_events", [])
            if not isinstance(events, list):
                raise ValueError("Permission receipt events are invalid")
            events.append(fact)
            dropped = len(events) - MAX_PERMISSION_EVENTS
            if dropped > 0:
                del events[:dropped]
                metadata["permission_events_truncated"] = (
                    metadata.get("permission_events_truncated", 0) + dropped
                )
            db.execute("UPDATE tasks SET metadata=? WHERE id=?", (json.dumps(metadata), task_id))
        return self.get(task_id)

    def metadata(self, task_id: str, changes: dict[str, Any]) -> Task:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            metadata = json.loads(row["metadata"])
            # Every form dict.update accepts (a mapping or pairs) is checked by its keys.
            changes = dict(changes)
            if receipt_version.names_key(changes):
                raise ValueError(receipt_version.REFUSAL)
            if "kind" in changes and "kind" in metadata and changes["kind"] != metadata["kind"]:
                raise ValueError("Task provider identity is immutable")
            _refuse_provenance_change(metadata, changes)
            metadata.update(changes)
            db.execute("UPDATE tasks SET metadata=? WHERE id=?", (json.dumps(metadata), task_id))
        return self.get(task_id)

    def invalidate_submission(self, task_id: str, detail: str) -> Task:
        """A mismatched delivery receipt invalidates even a fast worker completion claim."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            metadata = json.loads(row["metadata"])
            metadata["identity_invalidated"] = True
            retention = read_record(db, "tasks", task_id)
            if retention is not None:
                retention.update(projection=None, current_result_present=False)
                db.execute(
                    "UPDATE retention_records SET record=? WHERE entity='tasks' AND row_id=?",
                    (canonical(retention), task_id),
                )
            db.execute(
                "UPDATE tasks SET status='uncertain', result=NULL, detail=?, updated=?, "
                "metadata=? WHERE id=?",
                (detail, time.time(), json.dumps(metadata), task_id),
            )
        return self.get(task_id)

    def complete_worker(
        self, task_id: str, result: str, *, provenance: Mapping[str, str] | None = None
    ) -> Task:
        """Complete a worker-reported receipt, recording `provenance` with the result.

        `provenance` holds keys of RESULT_PROVENANCE_KEYS. It is written by the same
        statement that completes the receipt, or held with a provisional result until
        delivery is verified, so no completed receipt lacks the label of its result.
        """
        provenance = dict(provenance or {})
        if any(key not in RESULT_PROVENANCE_KEYS for key in provenance):
            raise ValueError("Unknown result provenance key")
        # Checked before any write: a non-string held as a provisional result would make the
        # later delivery confirmation fail, and the receipt could never complete.
        if not isinstance(result, str):
            raise ValueError("Result must be a string")
        if len(result) > 200_000:
            raise ValueError("Result exceeds 200000 characters")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            task = self.decode(row, db)
            policy = definition(task["metadata"].get("kind", ""))
            if policy is None or not policy.worker_reports:
                raise ValueError(
                    "Desktop results require a verified turn; manual completion refused"
                )
            if task["metadata"].get("identity_invalidated"):
                raise ValueError("Delivery identity was invalidated; worker completion is refused")
            if task["status"] == "completed":
                if not content_matches(task, "result", result):
                    raise ValueError("Task already has a different completed result")
                return task
            if task["status"] in TERMINAL:
                raise ValueError("Task is no longer active")
            if policy.verified_delivery_first and not task["metadata"].get("delivery_verified"):
                metadata = task["metadata"]
                if "provisional_result" in metadata and metadata["provisional_result"] != result:
                    raise ValueError("Task already has a different reported result")
                metadata["provisional_result"] = result
                metadata["completion_received"] = True
                if provenance:
                    metadata[PROVISIONAL_PROVENANCE] = provenance
                db.execute(
                    "UPDATE tasks SET metadata=?, updated=? WHERE id=?",
                    (json.dumps(metadata), time.time(), task_id),
                )
            else:
                if provenance:
                    _refuse_provenance_change(task["metadata"], provenance)
                    task["metadata"].update(provenance)
                # The stamp is taken from the metadata that now holds the provenance;
                # without a stamp the provenance is still written with the completion.
                stamped = ended_metadata_text(task["metadata"])
                if stamped is None and provenance:
                    stamped = json.dumps(task["metadata"])
                if stamped is None:
                    db.execute(
                        "UPDATE tasks SET status='completed', result=?, detail=NULL, "
                        "updated=? WHERE id=?",
                        (result, time.time(), task_id),
                    )
                else:
                    db.execute(
                        "UPDATE tasks SET status='completed', result=?, detail=NULL, "
                        "updated=?, metadata=? WHERE id=?",
                        (result, time.time(), stamped, task_id),
                    )
        return self.get(task_id)

    def confirm_core_delivery(self, task_id: str, delivery: dict[str, Any]) -> Task:
        return self.confirm_delivery(task_id, delivery, expected_kind="hc")

    def confirm_core_typed_while_working(
        self,
        task_id: str,
        delivery: dict[str, Any],
        *,
        turn_before: int | None,
        turn_after: int | None,
    ) -> Task:
        """Record a `done` send that landed in a turn the agent had begun on its own.

        The identity check after the send found the same agent, so the delivery is
        verified and the receipt is `uncertain` until the worker's `task_complete`
        with this task's ID completes it. A result the worker reported before this
        call completes the receipt here, as `confirm_delivery` does.
        """
        turns = (
            f"turn {turn_before} before the send, {turn_after} after"
            if turn_before is not None and turn_after is not None
            else "turn counter not reported"
        )
        detail = (
            "Core typed the assignment into a turn the agent had already started "
            f"(receipt status {delivery.get('status')!r}; {turns}); the same agent is "
            "there, so its task_complete for this task ID completes the receipt"
        )
        return self.confirm_delivery(
            task_id,
            delivery,
            expected_kind="hc",
            changes={
                "typed_while_working": True,
                "turn_before": turn_before,
                "turn_after": turn_after,
            },
            uncertain_detail=detail,
        )

    def confirm_delivery(
        self,
        task_id: str,
        delivery: dict[str, Any],
        *,
        expected_kind: str,
        changes: dict[str, Any] | None = None,
        uncertain_detail: str | None = None,
    ) -> Task:
        """Publish an early core result only after the send identity has been verified.

        `changes` are further metadata keys written with the delivery. With
        `uncertain_detail`, a receipt that has no early result is `uncertain` with
        that detail whatever the receipt's `started` says.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            task = self.decode(row, db)
            metadata = task["metadata"]
            if metadata.get("kind") != expected_kind or metadata.get("identity_invalidated"):
                raise ValueError("Core delivery identity is invalidated or unavailable")
            if task["status"] in TERMINAL:
                return task
            metadata.update(delivery=delivery, delivery_verified=True, **(changes or {}))
            result = metadata.pop("provisional_result", None)
            provenance = metadata.pop(PROVISIONAL_PROVENANCE, None)
            if result is not None and isinstance(provenance, dict):
                _refuse_provenance_change(metadata, provenance)
                metadata.update(provenance)
            if result is not None:
                status, detail = "completed", None
            elif uncertain_detail is not None:
                status, detail = "uncertain", uncertain_detail
            elif delivery.get("started") is not True:
                status, detail = (
                    "uncertain",
                    "Core accepted text but did not observe the turn start",
                )
            else:
                status, detail = "submitted", None
            if status == "completed":
                metadata = receipt_version.stamp_ended(metadata)
            db.execute(
                "UPDATE tasks SET status=?, result=?, detail=?, metadata=?, updated=? WHERE id=?",
                (status, result, detail, json.dumps(metadata), time.time(), task_id),
            )
        return self.get(task_id)

    def update(
        self,
        task_id: str,
        status: str,
        *,
        result: str | None = None,
        detail: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        if status not in STATES:
            raise ValueError("Unknown task status")
        if status == "completed" and (result is None or len(result) > 200_000):
            raise ValueError("Completion requires a result of at most 200000 characters")
        with self.connection() as db:
            stamped = None
            if status in TERMINAL:
                # The lock comes before the read, so no other writer changes the metadata
                # between the read and the write of the stamped text.
                db.execute("BEGIN IMMEDIATE")
                stamped = self._ended_metadata(db, task_id)
            if stamped is None:
                db.execute(
                    "UPDATE tasks SET status=?, updated=?, result=?, detail=?, turn_id=? "
                    "WHERE id=? AND status NOT IN "
                    "('completed', 'failed', 'abandoned', 'cancelled')",
                    (status, time.time(), result, detail, turn_id, task_id),
                )
            else:
                db.execute(
                    "UPDATE tasks SET status=?, updated=?, result=?, detail=?, turn_id=?, "
                    "metadata=? WHERE id=? AND status NOT IN "
                    "('completed', 'failed', 'abandoned', 'cancelled')",
                    (status, time.time(), result, detail, turn_id, stamped, task_id),
                )
        return self.get(task_id)

    @staticmethod
    def _ended_metadata(db: sqlite3.Connection, task_id: str) -> str | None:
        """The stamped metadata text of a receipt that is about to end, or None.

        None leaves the column to today's statement: no such open receipt, a column that
        cannot be read or decoded, a value that is not a JSON object, a stamp that failed.
        """
        try:
            row = db.execute(
                "SELECT metadata FROM tasks "
                "WHERE id=? AND status NOT IN ('completed', 'failed', 'abandoned', 'cancelled')",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return ended_metadata_text(json.loads(row["metadata"]))
        except Exception:
            return None

    def reconcile(self, expected: Task, *, result: str, evidence: Mapping[str, Any]) -> Task:
        """Correct one receipt through the store hooks of the plugin that claims it.

        `expected` is the snapshot the caller verified; exactly one loaded plugin's hooks may
        claim it (`StoreHooks.is_reconcilable`). That plugin checks `evidence` against the
        snapshot's durable metadata and writes the correction under a compare-and-swap, so
        a receipt that changed meanwhile is refused. No claimant refuses here; two raise
        `StoreHookConflict`.
        """
        hooks = self.hooks.claimant(expected)
        if hooks is None:
            raise ValueError("Receipt is not reconcilable")
        return hooks.reconcile(self, expected, result=result, evidence=evidence)

    def abandon(self, expected: Task, reason: str) -> Task:
        """Audited reservation release; the caller must first refuse owned live workers."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValueError("An abandonment reason must contain 1–4000 characters")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (expected["id"],)).fetchone()
            if row is None:
                raise ValueError("Unknown task ID")
            current = self.decode(row, db)
            audit = current["metadata"].get("abandonment")
            if current["status"] == "abandoned" and isinstance(audit, dict):
                # A retry can supply either the audited terminal row or the exact
                # pre-transition snapshot. Concurrent metadata changes still fail CAS.
                metadata = dict(current["metadata"])
                metadata.pop("abandonment")
                previous = {
                    **current,
                    "metadata": metadata,
                    "status": audit.get("previous_status"),
                    "result": audit.get("previous_result"),
                    "detail": audit.get("previous_detail"),
                    "updated": audit.get("previous_updated"),
                    "turn_id": audit.get("previous_turn_id"),
                }
                # The abandonment wrote the ending version, so the snapshot from before it
                # is compared without that one key; the audit does not keep the old value.
                if audit.get("reason") == reason and (
                    current == expected or same_but_ending_version(previous, expected)
                ):
                    return current
            if current != expected:
                raise ValueError("Receipt changed during abandonment; inspect it again")
            if current["status"] in TERMINAL:
                raise ValueError("Cannot abandon a terminal receipt")
            metadata = dict(current["metadata"])
            now = time.time()
            metadata["abandonment"] = {
                "reason": reason,
                "previous_status": current["status"],
                "previous_result": current["result"],
                "previous_detail": current["detail"],
                "previous_updated": current["updated"],
                "previous_turn_id": current["turn_id"],
                "abandoned_at": now,
            }
            metadata = receipt_version.stamp_ended(metadata)
            db.execute(
                "UPDATE tasks SET status='abandoned', result=NULL, detail=?, metadata=?, "
                "updated=? WHERE id=?",
                (reason, json.dumps(metadata), now, current["id"]),
            )
        return self.get(expected["id"])

    def worker_session(self, alias: str) -> dict[str, str | None] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM worker_sessions WHERE alias=?", (alias,)).fetchone()
        return dict(row) if row else None

    def save_worker_session(
        self, alias: str, session_id: str, cwd: str, *, profile: str | None = None
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO worker_sessions (alias, session_id, cwd, profile) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET session_id=excluded.session_id, "
                "cwd=excluded.cwd, profile=excluded.profile",
                (alias, session_id, cwd, profile),
            )

    def recent(self, limit: int = 30) -> list[Task]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connection() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self.decode(row, db) for row in rows]

    def by_subject(self, subject: str, *, limit: int = 100) -> tuple[list[Task], int]:
        """Receipts with exactly this subject, newest first, from the whole store.

        Returns at most `limit` receipts and the count of all receipts with the subject.
        """
        checked = validate_subject(subject)
        if checked is None:
            raise ValueError(SUBJECT_REFUSAL)
        subject = checked
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connection() as db:
            db.execute("BEGIN")
            total = db.execute(
                "SELECT COUNT(*) FROM task_subjects WHERE subject=?", (subject,)
            ).fetchone()
            rows = db.execute(
                "SELECT tasks.* FROM task_subjects JOIN tasks ON tasks.id=task_subjects.task_id "
                "WHERE task_subjects.subject=? ORDER BY tasks.created DESC, tasks.id LIMIT ?",
                (subject, limit),
            ).fetchall()
            return [self.decode(row, db) for row in rows], total[0]

    def complete_herdr(self, task_id: str, result: str) -> Task:
        """Compatibility alias for the original Python companion API."""
        return self.complete_worker(task_id, result)

    @contextmanager
    def _snapshot_connection(self, seconds: float) -> Iterator[sqlite3.Connection]:
        """Open one existing store read-only, with a bounded wait for its lock."""
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(
                self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=max(0, seconds)
            )
            db.row_factory = sqlite3.Row
            yield db
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise TimeoutError from None
            raise
        finally:
            if db is not None:
                db.close()

    def snapshot_read(self, *, deadline: float) -> dict[str, Any]:
        """Read all three sections in one transaction before a monotonic deadline."""
        sections: dict[str, Any] = {"errors": []}
        with self._snapshot_connection(deadline - time.monotonic()) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("BEGIN")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            db.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}")
            # Read the header without parsing schema SQL, pinning the shared lock
            # before a query could wait once for preparation and again for execution.
            db.execute("PRAGMA schema_version").fetchone()

            def rows(sql: str) -> list[dict[str, Any]]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                db.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}")
                result = [dict(row) for row in db.execute(sql).fetchall()]
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return result

            columns = "SELECT id,request_key,target,status,created,updated,detail FROM tasks "
            for section, operation in (
                (
                    "counts",
                    lambda: {
                        row["status"]: row["count"]
                        for row in rows(
                            "SELECT status,COUNT(*) AS count FROM tasks GROUP BY status"
                        )
                    },
                ),
                (
                    "needs_person",
                    lambda: (
                        rows(
                            columns + "WHERE status IN ('uncertain','blocked') "
                            "ORDER BY updated DESC LIMIT 200"
                        ),
                        rows(
                            "SELECT COUNT(*) AS total FROM tasks "
                            "WHERE status IN ('uncertain','blocked')"
                        )[0]["total"],
                    ),
                ),
                ("recent", lambda: rows(columns + "ORDER BY created DESC LIMIT 20")),
            ):
                try:
                    sections[section] = operation()
                except TimeoutError:
                    raise
                except Exception as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or (
                        code == sqlite3.SQLITE_INTERRUPT and time.monotonic() >= deadline
                    ):
                        raise TimeoutError from None
                    sections[section] = None
                    sections["errors"].append({"section": section, "error": "store read failed"})
        return sections
