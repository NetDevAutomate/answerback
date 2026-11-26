"""Assignment store: slots and attempts over ordinary receipts (plan row F2).

An assignment has slots, a slot has attempts, and each attempt points at one ordinary
receipt by its request key and, once known, its task ID. A fan-out group is an
assignment with several slots. A failover or a move is a new attempt in the same slot.

Status is computed from the receipts on every read and never stored. The one stored
outcome is a write-once winner per slot. An assignment reads ``satisfied``; only a
receipt reads ``completed``.

Status answers three separate questions. Q1 asks what each attempt's own receipt
proves about a terminal that might wait for a person; its classification is:

================ ================================================================
Class            Receipt evidence
================ ================================================================
``none``         Absent lookup, no task ID, admission ``planned`` or ``not_sent``.
``ended``        Valid ``completed``, ``failed`` or ``cancelled``; certain admission;
                 not invalidated.
``may_hold``     Everything else: live, blocked, uncertain, unknown, unreadable,
                 conflicting or missing bound receipts; ``abandoned`` with any
                 admission; every uncertain-admission final; any invalidated receipt.
================ ================================================================

``ended`` relies on the adapters writing a final state other than ``abandoned`` only
when nothing was sent or the recipient's protocol reported the end of its turn.
An adapter that cannot establish that writes ``uncertain``.

Q1 folds every attempt without position: an affirmative Core block or a blocked
receipt always blocks; a true Core fact on ``none``/``ended`` is also a contradiction,
as is ``core_blocked: false`` against a ``blocked`` receipt, for every attempt.
Missing or stale Core evidence contributes unknownness exactly for ``may_hold``.
Q2 asks whether any valid completion satisfies the slot, retaining a stored winner
whose completion is no longer valid as ``needs_decision``. Q3 reads only the latest
attempt's execution state, stall, verdict, candidates, eligibility, deadline and cap.
Composition applies blocking, then contradictions, then the status. The
``unfinished_attempts`` list contains exactly Q1's ``may_hold`` attempts.

Planned, then submit. The caller writes the attempt row first (``create`` or
``plan_attempt``), asks ``plan_attempt`` for that attempt, sends only when the result has
``sendable`` true, calling the unchanged adapter with the attempt's ``member_key`` as its
request key, then records what happened (``record_submitted`` or ``record_raised``). A crash
in between is found again by looking the key up in the receipt store, because every local
adapter reserves its receipt under that key before it sends anything.

A receipt belongs to an attempt only when its request key, resolved target, provider
and message identity all match what the attempt names (``Recipient``). Every receipt
read has one of four outcomes: ``absent`` (the lookup succeeded and found nothing),
``valid``, ``conflicting`` (a readable receipt for other work) and ``unavailable`` (the
lookup failed or the receipt cannot be checked). Only ``absent`` can support "nothing
was reserved", and only for a local recipient. ``Recipient`` fields come from the
adapter-derived resolution only, never from a caller of an RPC method or a tool.

Sending. ``admission`` in any view (``get``, ``create``, ``record_submitted``,
``record_raised``) is a stored fact about the adapter call, not a permission: a view can
show ``planned`` for an attempt that may no longer be sent (the slot has won, its key
now holds other work, a read failed, the deadline passed). Every attempt in a view has
``sendable`` false. Only ``plan_attempt`` returns ``sendable`` true, and only for a
``planned`` attempt for which it has just, inside its write transaction, read the
receipt under that key and under every earlier key of the slot, found the slot neither
won nor unresolved, and checked the deadline. That is a statement about the receipt
store at the moment of those reads. The receipts live in another database, so a
receipt written there after the read (for example a late completion of an earlier
attempt by another process) is not fenced; the wiring rows must revalidate at
reservation and delivery (hand-back, "For rows F3, F4 and F6").

Reading without facts (review 4, D1). A plain ``get`` without an ``assessment`` cannot
read ``active``: a live receipt needs ``core_blocked`` and ``stalled`` known false. Its
``needs_decision`` there means insufficient observation, not evidence that the work
stalled; ``assessment_absent`` names what was missing. Uncertain admission (review 4,
D3): an attempt whose adapter call raised after its receipt was reserved stays
``uncertain`` even when a later receipt carries ``delivery: not_attempted``; which
evidence may resolve it is open for the plan's author.

Deadline. ``deadline`` is a cutoff for successors (attempt 2 and later), not for the
assignment: attempt 1 is exempt, bound attempts can be read after it, and nothing
running is stopped. See ``AssignmentStore.create``.

This store is not the authorization boundary for a second attempt. It refuses the
absolute cases (an unsettled, uncertain or blocked predecessor, a predecessor whose
receipt carries ``blocked_seen``, the mark that row F1 writes (no adapter writes it
before F1), a satisfied slot, a gap or a cap). Whether a successor may follow is row
F3's verdict, which row F6 must check against fresh evidence; the stored ``verdict``
is a label.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, NotRequired, Protocol, Required, TypedDict

from .digests import KEY, PEER, canonical, digest
from .retention import content_identity
from .transport import private_directory

FILENAME = "assignments.sqlite"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x41425346  # "ABSF": marks a file as an assignment store
KINDS = ("single", "fanout")
ROUTES = ("direct", "bus")
FAILOVER_MODES = ("off", "never_started")
ADMISSIONS = ("planned", "submitted", "not_sent", "uncertain")
LOOKUPS = ("absent", "valid", "conflicting", "unavailable")
SLOT_STATES = (
    "planned",
    "not_sent",
    "active",
    "blocked",
    "needs_decision",
    "exhausted",
    "satisfied",
)
ASSIGNMENT_STATES = (
    "admitting",
    "blocked",
    "satisfied",
    "active",
    "needs_decision",
    "unsatisfied",
)
MAX_MEMBERS = 8
ATTEMPT_CAP = 3
# Receipt states (tasks.STATES) as this store reads them.
UNFINISHED = ("pending", "queued", "submitted", "running")
SETTLED_UNSUCCESSFUL = ("failed", "cancelled", "abandoned")
TERMINAL_RECEIPT = ("completed", *SETTLED_UNSUCCESSFUL)
# The statuses of ``answerback_bridge.adapters.contracts.STATUSES``, stated here; the test
# ``test_the_statuses_of_the_module_are_those_of_the_receipt_contract`` compares the two.
RECEIPT_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "submitted",
        "running",
        "blocked",
        "uncertain",
        *TERMINAL_RECEIPT,
    }
)
# Receipt reads that a store-level lookup failure can raise.
READ_ERRORS = (ValueError, OSError, sqlite3.DatabaseError)
_READ_TRIES = 5

_GROUP_ID = re.compile(r"[A-Za-z0-9_.:-]{1,96}")
_SINGLE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_VERDICT = re.compile(r"[a-z_+]{1,32}")
_SLOT = re.compile(r"0|[a-f0-9]{16}")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_TARGET = re.compile(r"[^\x00-\x1f\x7f]{1,512}")
# The code points that UTF-8 cannot encode: the surrogates, U+D800 to U+DFFF.
_SURROGATE = re.compile("[\ud800-\udfff]")
_UTF8_TEXT = "The assignment store binds no text that UTF-8 cannot encode"

_DDL = (
    """CREATE TABLE assignments (
        assignment_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('single','fanout')),
        identity TEXT NOT NULL, rule TEXT NOT NULL, route TEXT NOT NULL,
        failover TEXT NOT NULL, max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 3),
        independent_of TEXT, message_identity TEXT NOT NULL, created REAL NOT NULL,
        deadline REAL)""",
    """CREATE TABLE assignment_members (
        assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
        slot TEXT NOT NULL, ordinal INTEGER NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 3),
        peer_id TEXT NOT NULL, target TEXT NOT NULL, provider TEXT NOT NULL,
        local INTEGER NOT NULL CHECK (local IN (0, 1)),
        member_key TEXT UNIQUE NOT NULL, message_identity TEXT NOT NULL,
        admission TEXT NOT NULL
            CHECK (admission IN ('planned','submitted','not_sent','uncertain')),
        task_id TEXT UNIQUE, parent_task_id TEXT, reason TEXT, verdict TEXT,
        evidence TEXT, detail TEXT,
        winner INTEGER NOT NULL DEFAULT 0 CHECK (winner IN (0, 1)),
        created REAL NOT NULL, updated REAL NOT NULL,
        CONSTRAINT assignment_admission_task CHECK (
            (admission IN ('planned','not_sent') AND task_id IS NULL)
            OR (admission='submitted' AND task_id IS NOT NULL)
            OR admission='uncertain'),
        PRIMARY KEY (assignment_id, slot, attempt))""",
    "CREATE UNIQUE INDEX assignment_one_winner "
    "ON assignment_members(assignment_id, slot) WHERE winner=1",
    """CREATE TRIGGER assignment_winner_write_once
        BEFORE UPDATE OF winner ON assignment_members
        WHEN OLD.winner=1 AND NEW.winner IS NOT 1
        BEGIN SELECT RAISE(ABORT, 'assignment winner is write-once'); END""",
    """CREATE TRIGGER assignment_head_write_once
        BEFORE UPDATE ON assignments
        BEGIN SELECT RAISE(ABORT, 'an assignment is write-once'); END""",
    """CREATE TRIGGER assignment_task_write_once
        BEFORE UPDATE OF task_id ON assignment_members
        WHEN OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id
        BEGIN SELECT RAISE(ABORT, 'assignment attempt receipt is write-once'); END""",
)


class ReceiptLookup(Protocol):
    """The two receipt lookups the store needs; ``TaskStore`` satisfies it.

    A receipt is a mapping with ``id``, ``request_key``, ``target``, ``status`` and
    ``metadata`` (with ``kind``), plus either ``message`` or the retention record
    ``_retention`` (``TaskStore.decode``). ``public_task`` projections carry neither, so
    they read as ``unavailable`` and can never bind an attempt. A lookup that raises
    ``ValueError``, ``OSError`` or ``sqlite3.DatabaseError`` reads as ``unavailable``.
    """

    def by_request(self, request_key: str) -> Mapping[str, Any] | None:
        """Return the receipt reserved under ``request_key``, or ``None``."""
        ...

    def get(self, task_id: str) -> Mapping[str, Any]:
        """Return the receipt ``task_id``; raise ``ValueError`` when it is unknown."""
        ...


class Recipient(NamedTuple):
    """One attempt's recipient, as the caller resolved it before anything is sent.

    ``peer_id`` is the canonical peer ID (``hc:t1``). ``target`` is the receipt target
    the adapter will reserve under (``hc:default:t1``, built by the adapter's own rule,
    for example ``CoreTasks.target_key``). ``provider`` is the receipt's
    ``metadata.kind`` (``hc``). ``local`` is true only when the adapter reserves the
    receipt in this bridge's own store before it sends anything **and** the recipient
    runs on this machine; only then can a key lookup that finds nothing prove that nothing
    was sent (plan section 6, proof 1).

    Trust boundary. The store checks the shape of these fields, not their truth: a
    ``Recipient`` naming ``hc:t1`` with t3's target binds t3's receipt. So every field
    must come from the adapter-derived resolution of the peer (the adapter's own target
    rule and its knowledge of where the peer runs), never from a caller of an RPC method
    or an agent tool. A local reservation does not prove that the recipient is local: a
    Herdr terminal on a machine in ``hc_send_machines`` is reserved in this store and is
    still remote, so it needs ``local=False``.
    """

    peer_id: str
    target: str
    provider: str
    local: bool


class Assessment(TypedDict, total=False):
    """Current facts about one attempt that F2 cannot read itself; ``get`` reads them.

    The caller (rows F1, F3, F7 and whoever wires them) supplies one per member key. F2
    defines only this shape and never infers a fact from receipt metadata.

    ``task_id`` is required and must equal the attempt's bound receipt (``None`` for an
    attempt with no receipt); an assessment about another receipt is stale and is
    ignored. ``core_blocked``: Herdr core reports the terminal ``blocked`` now.
    ``stalled``: the attempt is stalled (a read-only status, never a trigger).
    ``no_candidate``: no recipient is left within policy for a successor. ``verdict``:
    F3's current verdict for the ended attempt (``auto`` and the others of failover lane
    section 4.1). A fact that is missing is unknown, never ``False``.
    """

    task_id: Required[str | None]
    core_blocked: NotRequired[bool]
    stalled: NotRequired[bool]
    no_candidate: NotRequired[bool]
    verdict: NotRequired[str]


_FACTS = ("core_blocked", "stalled", "no_candidate", "verdict")


def _status_word(status: Any) -> str:
    """What a refusal says for a receipt's status: the status when it is exactly ``str``
    and one of ``RECEIPT_STATUSES``, and otherwise the words ``another status``."""
    return status if type(status) is str and status in RECEIPT_STATUSES else "another status"


def _encodable(text: str) -> bool:
    """Whether UTF-8 can encode ``text``: it holds no surrogate (U+D800 to U+DFFF)."""
    return _SURROGATE.search(text) is None


def _check_assessment(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or "task_id" not in value:
        raise ValueError(f"Assessment for {key} must be a mapping with task_id")
    if not all(_encodable(name) for name in value if isinstance(name, str)):
        raise ValueError("Assessment fact names must be text that UTF-8 can encode")
    unknown = set(value) - {"task_id", *_FACTS}
    if unknown:
        raise ValueError(f"Assessment for {key} has unknown facts: {sorted(unknown)}")
    if value["task_id"] is not None and not isinstance(value["task_id"], str):
        raise ValueError(f"Assessment task_id for {key} must be text or null")
    for name in ("core_blocked", "stalled", "no_candidate"):
        if name in value and type(value[name]) is not bool:
            raise ValueError(f"Assessment {name} for {key} must be true or false")
    if "verdict" in value and (
        not isinstance(value["verdict"], str) or not _VERDICT.fullmatch(value["verdict"])
    ):
        raise ValueError(f"Assessment verdict for {key} must be 1–32 lowercase letters, _ or +")
    return dict(value)


def _outer(assessment: Any) -> Mapping[str, Any]:
    """``get``'s ``assessment``: ``None`` (no assessment) or a mapping with text keys."""
    if assessment is None:
        return {}
    if not isinstance(assessment, Mapping):
        raise ValueError("Assessment must be a mapping of member keys to facts, or null")
    if not all(isinstance(key, str) for key in assessment):
        raise ValueError("Assessment member keys must be text")
    if not all(_encodable(key) for key in assessment):
        raise ValueError("Assessment member keys must be text that UTF-8 can encode")
    return assessment


@dataclass(frozen=True, slots=True)
class _Seen:
    lookup: str
    receipt: Mapping[str, Any] | None = None
    reason: str | None = None
    # The receipt's ``id``, read once from the copy: exactly ``str`` whenever ``lookup`` is
    # ``valid`` or ``conflicting``, ``None`` otherwise.
    task_id: str | None = None


def _identity_shape(value: Any) -> dict[str, Any] | None:
    if (
        isinstance(value, Mapping)
        and set(value) == {"present", "bytes", "sha256"}
        and type(value["present"]) is bool
        and type(value["bytes"]) is int
        and isinstance(value["sha256"], str)
        and re.fullmatch("[a-f0-9]{64}", value["sha256"]) is not None
    ):
        return dict(value)
    return None


def _message_identity(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    """Content identity of a receipt's message, also after retention pruned the text."""
    retention = receipt.get("_retention")
    if retention is not None:
        fields = retention.get("fields") if isinstance(retention, Mapping) else None
        return _identity_shape(fields.get("message")) if isinstance(fields, Mapping) else None
    message = receipt.get("message")
    if not isinstance(message, str) or not _encodable(message):
        return None
    return content_identity(message)


def _classify(receipt: Any, key: str, expected: Mapping[str, Any]) -> _Seen:
    """Sort a receipt into valid, conflicting or unavailable for one attempt.

    The lookup's object is read here. Its top level is copied once into a plain ``dict``
    and its ``metadata``, when that is a mapping, once into another; the message identity
    is taken in the same step. A receipt with a ``_retention`` has the identity retained
    there and its ``message`` is not read; otherwise the ``message`` is the source of the
    identity, and one that UTF-8 cannot encode has none (the receipt is ``unavailable``,
    with the reason of an identity that cannot be read). What ``_Seen`` holds is the copy,
    so a field of the lookup's top level or of its ``metadata`` that is replaced afterwards
    is not seen (``test_a_receipt_changed_after_it_was_returned_changes_nothing`` and
    ``test_a_metadata_mapping_changed_after_it_was_returned``, for the ``id``, the
    ``status`` and the two flags). A value inside the copy is shared with the lookup's
    object and taken as it is: a change inside such a value is seen. An exception of a
    class in ``READ_ERRORS`` while the copy is taken makes the receipt ``unavailable``, as
    the same exception out of the lookup does; ``RuntimeError`` leaves the method, as it
    does out of the lookup (``test_a_receipt_that_raises_while_it_is_copied``).

    The ``id`` is read once, from the copy, and kept in ``_Seen.task_id``, the value that
    the statements get. A receipt whose ``id`` is not exactly ``str`` is read as a receipt
    without an ``id``
    (``test_a_receipt_whose_id_is_not_exactly_text_is_a_receipt_without_an_id``). An ``id``
    that is a ``str`` and does not pass ``_is_task_id`` (longer than 128 characters, or
    UTF-8 cannot encode it) makes the receipt ``unavailable`` as well, with one reason for
    both causes. The receipt's other texts (``request_key``, ``target``, ``status``, the
    provider) are compared and not encoded.
    """
    if not isinstance(receipt, Mapping):
        return _Seen("unavailable", reason="lookup returned no receipt mapping")
    try:
        copy = dict(receipt)
        if isinstance(copy.get("metadata"), Mapping):
            copy["metadata"] = dict(copy["metadata"])
        identity = _message_identity(copy)
    except READ_ERRORS as error:
        return _Seen("unavailable", reason=f"receipt copy failed: {type(error).__name__}")
    task_id = copy.get("id")
    if type(task_id) is not str or not task_id:
        return _Seen("unavailable", reason="receipt has no task ID")
    if not _is_task_id(task_id):
        return _Seen("unavailable", reason="receipt task ID is outside the rule for a task ID")
    metadata = copy.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(copy.get("target"), str):
        return _Seen("unavailable", reason="receipt has no target or metadata")
    if identity is None:
        return _Seen("unavailable", reason="receipt message identity cannot be read")
    if copy.get("request_key") != key:
        return _Seen("conflicting", copy, "receipt has another request key", task_id)
    if not isinstance(metadata.get("kind"), str):
        return _Seen("unavailable", reason="receipt has no provider")
    if copy["target"] != expected["target"]:
        return _Seen("conflicting", copy, "receipt has another recipient", task_id)
    if metadata["kind"] != expected["provider"]:
        return _Seen("conflicting", copy, "receipt has another provider", task_id)
    if identity != json.loads(expected["message_identity"]):
        return _Seen("conflicting", copy, "receipt describes other work", task_id)
    return _Seen("valid", copy, task_id=task_id)


def _metadata_flag(receipt: Mapping[str, Any] | None, name: str) -> bool:
    metadata = None if receipt is None else receipt.get("metadata")
    return isinstance(metadata, Mapping) and bool(metadata.get(name))


def _member_key(kind: str, assignment_id: str, slot: str, attempt: int) -> str:
    base = assignment_id if kind == "single" else f"g:{assignment_id}:{slot}"
    key = base if attempt == 1 else f"{base}.a{attempt}"
    if not KEY.fullmatch(key):  # unreachable with the caps above; kept as a guard
        raise ValueError("Derived member key breaks the request key rule")
    return key


def _slot_id(kind: str, peer: str) -> str:
    return "0" if kind == "single" else hashlib.sha256(peer.encode()).hexdigest()[:16]


def _check_message(message: str) -> dict[str, Any]:
    if not isinstance(message, str) or not message.strip() or len(message) > 100_000:
        raise ValueError("Message must contain 1–100000 characters")
    if not _encodable(message):
        raise ValueError("Message must be text that UTF-8 can encode")
    return content_identity(message)


def _check_peer(peer: Any, name: str = "peer") -> str:
    if not isinstance(peer, str) or not PEER.fullmatch(peer):
        raise ValueError(f"{name} must be a canonical peer ID such as hc:t1")
    return peer


def _check_id(value: Any) -> str:
    """Return ``value`` if it is an assignment ID: the rule of ``get``, ``plan_attempt``
    and ``describe``. ``create`` checks its ID against the limit of its kind.

    Text means exactly ``str``: an instance of a subclass can hand ``sqlite3`` another
    value to bind (``__conform__``), so it could pass here as one text and reach a
    statement as another. A subclass with ``__str__``, ``__eq__`` and ``__hash__`` alone
    was bound as its own text. The refusal is a fixed text.
    """
    if type(value) is not str or not _SINGLE_ID.fullmatch(value):
        raise ValueError("assignment_id must be 1–120 letters, digits, dots, colons, _ or -")
    return value


def _is_task_id(value: Any) -> bool:
    """The one rule for a task ID, and for a member key: exactly ``str``, 1–128 characters,
    and UTF-8 can encode it (``_encodable``).

    ``record_submitted``, ``record_raised`` and ``locate`` ask it at entry and ``_classify``
    asks it for a receipt's ``id``. In the sequences that
    ``test_nothing_is_stored_that_cannot_be_named`` runs, the member table held no task ID
    that fails it, so ``record_submitted`` and ``locate`` could name each one stored.
    """
    return type(value) is str and 1 <= len(value) <= 128 and _encodable(value)


def _check_key(value: Any, name: str) -> str:
    """Return ``value`` if it passes ``_is_task_id``, the rule for a member key and a task ID.

    ``name`` is ``member_key`` or ``task_id`` and stands in the refusal's text.
    """
    if not _is_task_id(value):
        raise ValueError(f"{name} must be 1–128 characters")
    return value


def _check_recipient(value: Any) -> Recipient:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError("A recipient is Recipient(peer_id, target, provider, local)")
    recipient = Recipient(*value)
    _check_peer(recipient.peer_id)
    if not isinstance(recipient.target, str) or not _TARGET.fullmatch(recipient.target):
        raise ValueError("Recipient target must be 1–512 printable characters")
    if not _encodable(recipient.target):
        raise ValueError("Recipient target must be text that UTF-8 can encode")
    if not isinstance(recipient.provider, str) or not _PROVIDER.fullmatch(recipient.provider):
        raise ValueError("Recipient provider must be a provider name such as hc")
    if type(recipient.local) is not bool:
        raise ValueError("Recipient local must be true or false")
    if recipient.local and recipient.peer_id.startswith("node:"):
        raise ValueError("A node: recipient is never local")
    return recipient


def _check_text(value: str | None, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be 1–{limit} characters")
    return value


_PLAIN = (str, int, float, type(None))
_PLAIN_TEXT = "The assignment store binds only a tuple of str, int, float or None"


class _PlainConnection(sqlite3.Connection):
    """The connection of the store: the parameters of a statement are a tuple of plain
    values, or the statement does not run.

    ``sqlite3`` lets a value hand it another value to bind (``__conform__``). The
    statements that ``test_the_module_runs_a_statement_in_one_way_only`` finds in the
    module's source pass ``execute`` here (its docstring says what it cannot see): the
    parameters must be a ``tuple`` of values that are exactly ``str``, ``int``, ``float``
    or ``None``. A ``bool``, a member of an enum and an
    instance of a subclass are refused with one fixed ``ValueError`` before the
    statement runs, and a ``str`` that UTF-8 cannot encode (``_encodable``) with another.

    A statement runs on a store's connection without passing ``execute``: the ``COMMIT``
    that ``sqlite3`` itself issues when the ``with`` block of a connection ends, or its
    ``ROLLBACK`` when the block ends with an exception. In the calls that
    ``test_only_transaction_control_runs_outside_execute`` makes of the public methods, a
    trace callback on the connection showed these two outside ``execute`` (and it named
    an ``UPDATE`` twice for one call of ``execute``).

    The class checks the type of a value it is given, not what ``sqlite3`` then binds,
    and it does not see the adapters of the process: with an adapter registered for
    ``str``, ``int`` or ``float`` (``sqlite3.register_adapter``), a connection with this
    same check stored ``adapted:a`` for the text ``a``, 1005 for 5 and 2.0 for 1.5.
    """

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if type(parameters) is not tuple or not all(
            any(type(value) is plain for plain in _PLAIN) for value in parameters
        ):
            raise ValueError(_PLAIN_TEXT)
        if not all(_encodable(value) for value in parameters if type(value) is str):
            raise ValueError(_UTF8_TEXT)
        return super().execute(sql, parameters)


def _is_file_name(path: Path) -> bool:
    """Whether the file system encoding can encode ``path`` (``os.fsencode``). A path that
    Python decoded from bytes that are not UTF-8 can be encoded and stays accepted."""
    try:
        os.fsencode(path)
    except UnicodeEncodeError:
        return False
    return True


def _schema(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """Every schema object, SQLite's own autoindexes included: nothing is filtered out."""
    return [
        tuple(row)
        for row in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        )
    ]


# Tables that SQLite itself creates: ANALYZE writes the statistics tables, AUTOINCREMENT
# writes sqlite_sequence. They are allowed in a marked store by exact name and nothing else.
SQLITE_OWN_TABLES = frozenset(
    {"sqlite_stat1", "sqlite_stat2", "sqlite_stat3", "sqlite_stat4", "sqlite_sequence"}
)


def _application_schema(objects: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """The schema rows without SQLite's own tables, matched by exact type and name."""
    return [
        row
        for row in objects
        if not (row[0] == "table" and row[1] in SQLITE_OWN_TABLES and row[2] == row[1])
    ]


def _expected_schema() -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as db:
        for statement in _DDL:
            db.execute(statement)
        return _schema(db)


_EXPECTED_SCHEMA = _expected_schema()


def _fingerprint(rows: Sequence[sqlite3.Row]) -> list[tuple[Any, ...]]:
    return [(r["member_key"], r["task_id"], r["admission"], r["winner"]) for r in rows]


class AssignmentStore:
    """The shared record for fan-out, failover and moves, in ``assignments.sqlite``."""

    def __init__(
        self, path: Path, receipts: ReceiptLookup, *, clock: Callable[[], float] = time.time
    ) -> None:
        """Open or create the store at ``path``; ``receipts`` answers every status read.

        A new or empty file becomes an assignment store. Any other file must carry the
        assignment store's marker and exact schema; a foreign file is refused unchanged.
        The only other objects allowed are SQLite's own tables (``SQLITE_OWN_TABLES``:
        the ``ANALYZE`` statistics and ``sqlite_sequence``), by exact name.

        ``clock`` must return a Unix time that is exactly a ``float`` or an ``int``. A
        ``bool`` or an instance of a subclass is not stored: the statement that would
        bind it raises ``ValueError``, as it does for a text that UTF-8 cannot encode.
        Whether the value is a time is not checked.
        """
        self.path, self.receipts, self.clock = Path(path), receipts, clock
        if not _is_file_name(self.path):
            raise ValueError("The assignment store path cannot be encoded as a file name")
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError("Assignment database must not be a symlink")
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            application = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            objects = _schema(db)
            if application == APPLICATION_ID:
                if version != SCHEMA_VERSION:
                    raise ValueError(
                        f"Assignment database has schema version {version}; "
                        f"this bridge reads version {SCHEMA_VERSION}"
                    )
                if _application_schema(objects) != _EXPECTED_SCHEMA:
                    raise ValueError("Assignment database schema does not match this bridge")
            elif application == 0 and version == 0 and not objects:
                for statement in _DDL:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                raise ValueError("The file is not an assignment store; refusing to change it")
        self.path.chmod(0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10, factory=_PlainConnection)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    # ------------------------------------------------------------------ receipt reads

    def _by_key(self, key: str, expected: Mapping[str, Any]) -> _Seen:
        try:
            receipt = self.receipts.by_request(key)
        except READ_ERRORS as error:
            return _Seen("unavailable", reason=f"lookup by key failed: {type(error).__name__}")
        return _Seen("absent") if receipt is None else _classify(receipt, key, expected)

    def _by_task(self, task_id: str, expected: Mapping[str, Any]) -> _Seen:
        try:
            receipt = self.receipts.get(task_id)
        except READ_ERRORS as error:
            return _Seen("unavailable", reason=f"lookup by task ID failed: {type(error).__name__}")
        seen = _classify(receipt, expected["member_key"], expected)
        if seen.lookup != "unavailable" and seen.receipt is not None:
            if seen.task_id != task_id:
                return _Seen("unavailable", reason="lookup returned another task ID")
        return seen

    def _observe(self, row: sqlite3.Row) -> _Seen:
        if row["task_id"] is None:
            return self._by_key(row["member_key"], dict(row))
        return self._by_task(row["task_id"], dict(row))

    # ------------------------------------------------------------------ writes

    def create(
        self,
        assignment_id: str,
        *,
        kind: str,
        recipients: Sequence[Recipient],
        message: str,
        quorum: int | None = None,
        route: str = "direct",
        failover: str = "off",
        max_attempts: int = 2,
        independent_of: str | None = None,
        deadline: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store an assignment and a ``planned`` attempt 1 per slot, in one transaction.

        ``deadline`` is an optional Unix time, stored once and part of the identity. A new
        assignment needs a deadline in the future. It is a cutoff for successors only:
        once it has passed, ``plan_attempt`` refuses every attempt after the first, new or
        re-planned. Attempt 1 is exempt, bound attempts can still be read and returned,
        and nothing running is stopped. The cutoff is wall-clock time compared at each
        call; an expiry seen earlier in the same call is kept, but nothing persists it
        across calls (a clock that steps back can reopen it). Rows F3 and F6 own the rest.

        ``recipients`` are resolved by the caller (``Recipient``). The same ID with the same
        work (recipients in any order) returns the stored assignment and ``created=False``;
        the same ID with other work, including another resolution of a peer, is refused.
        A receipt already under a member key is adopted only when it is valid for that
        member; a conflicting or unreadable one refuses the call. Nothing is sent.
        """
        if kind not in KINDS:
            raise ValueError("kind must be single or fanout")
        pattern = _SINGLE_ID if kind == "single" else _GROUP_ID
        if type(assignment_id) is not str or not pattern.fullmatch(assignment_id):
            cap = 120 if kind == "single" else 96
            raise ValueError(f"assignment_id must be 1–{cap} letters, digits, dots, colons, _ or -")
        members = [_check_recipient(recipient) for recipient in recipients]
        peers = [member.peer_id for member in members]
        if len(set(peers)) != len(peers) or len({m.target for m in members}) != len(members):
            raise ValueError("A peer may appear only once in an assignment (duplicate peer)")
        if kind == "single" and len(members) != 1:
            raise ValueError("A single assignment has exactly one peer")
        if kind == "fanout" and not 2 <= len(members) <= MAX_MEMBERS:
            raise ValueError(f"A fan-out group has 2 to {MAX_MEMBERS} peers")
        if quorum is not None and (
            kind == "single" or type(quorum) is not int or not 1 <= quorum <= len(members)
        ):
            raise ValueError("quorum must be an integer from 1 to the number of peers (fan-out)")
        if route not in ROUTES:
            raise ValueError("route must be direct or bus")
        if failover not in FAILOVER_MODES:
            raise ValueError("failover must be off or never_started")
        if type(max_attempts) is not int or not 1 <= max_attempts <= ATTEMPT_CAP:
            raise ValueError(f"max_attempts must be an integer from 1 to {ATTEMPT_CAP}")
        if independent_of is not None:
            _check_peer(independent_of, "independent_of")
            if independent_of in peers:
                raise ValueError("independent_of names a member of the assignment")
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0
        ):
            raise ValueError("deadline must be a finite, positive Unix time")
        message_identity = _check_message(message)
        stored_identity = canonical(message_identity)
        rule = "all" if quorum is None else f"quorum:{quorum}"
        identity = digest(
            {
                "kind": kind,
                "message": message_identity,
                "members": sorted([list(member) for member in members]),
                "rule": rule,
                "route": route,
                "failover": failover,
                "max_attempts": max_attempts,
                "independent_of": independent_of,
                "deadline": None if deadline is None else float(deadline),
            }
        )
        planned = []
        for member in members:
            slot = _slot_id(kind, member.peer_id)
            planned.append((slot, member, _member_key(kind, assignment_id, slot, 1)))
        if len({slot for slot, _, _ in planned}) != len(planned):
            raise ValueError("Two peers derive the same slot; choose another peer ID")
        for _, member, key in planned:
            expected = {
                "member_key": key,
                "target": member.target,
                "provider": member.provider,
                "message_identity": stored_identity,
            }
            seen = self._by_key(key, expected)
            if seen.lookup == "conflicting":
                raise ValueError(f"request key {key} already describes different work")
            if seen.lookup == "unavailable":
                raise ValueError(f"The receipt under request key {key} cannot be read")
        if deadline is not None and deadline <= self.clock():
            with self._connection() as db:  # fast refusal before the lock, for a new ID only
                db.execute("BEGIN")
                known = db.execute(
                    "SELECT 1 FROM assignments WHERE assignment_id=?", (assignment_id,)
                ).fetchone()
            if known is None:
                raise ValueError("deadline has already passed")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()  # taken under the write lock, after any wait for it
            existing = db.execute(
                "SELECT identity FROM assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if existing is not None:
                if existing["identity"] != identity:
                    raise ValueError("assignment_id already describes different work")
                created = False
            else:
                if deadline is not None and deadline <= now:
                    raise ValueError("deadline has already passed")
                db.execute(
                    "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        assignment_id,
                        kind,
                        identity,
                        rule,
                        route,
                        failover,
                        max_attempts,
                        independent_of,
                        stored_identity,
                        now,
                        None if deadline is None else float(deadline),
                    ),
                )
                try:
                    for ordinal, (slot, member, key) in enumerate(planned):
                        db.execute(
                            "INSERT INTO assignment_members (assignment_id, slot, ordinal,"
                            " attempt, peer_id, target, provider, local, member_key,"
                            " message_identity, admission, created, updated)"
                            " VALUES (?,?,?,1,?,?,?,?,?,?,'planned',?,?)",
                            (
                                assignment_id,
                                slot,
                                ordinal,
                                member.peer_id,
                                member.target,
                                member.provider,
                                int(member.local),
                                key,
                                stored_identity,
                                now,
                                now,
                            ),
                        )
                except sqlite3.IntegrityError as error:
                    raise ValueError(
                        "A member key already belongs to another assignment"
                    ) from error
                created = True
        return self.get(assignment_id), created

    def plan_attempt(
        self,
        assignment_id: str,
        slot: str,
        attempt: int,
        recipient: Recipient,
        *,
        message: str | None = None,
        reason: str | None = None,
        verdict: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Write attempt ``attempt`` of ``slot`` as ``planned`` before anything is reserved.

        Name the attempt number explicitly: two callers that both ask for attempt 2 get
        one row, and the second gets ``created=False`` (or a refusal if it names another
        recipient or message). ``message`` defaults to the assignment's message for a new
        attempt; for an existing attempt an omitted ``message`` matches what is stored.

        Only a result with ``sendable`` true may be sent; it is always ``planned``, and
        every path that returns one first reads the receipt store under that attempt's key
        and every earlier receipt of the slot. Any other result, including every view
        from ``get``, has ``sendable`` false whatever its ``admission`` says. A repeat of an
        existing attempt returns a bound attempt as it is. For an unbound ``planned`` or
        ``not_sent`` attempt it looks the key up: absent plans it (again) under the same
        key, a valid receipt binds it (and it is no longer sendable), and a conflicting or
        unreadable receipt is refused. A slot that has a winner or a valid completed
        receipt, or an earlier attempt whose receipt is not settled (conflicting,
        unreadable, gone, live, blocked or uncertain) or that is recorded ``not_sent`` and
        holds a receipt, yields no sendable attempt, whether or not a ``get`` has read
        that receipt first.

        A new successor is refused unless the previous attempt is ``not_sent`` with no
        receipt under its key, or its valid receipt settled without success (``failed``,
        ``cancelled``, ``abandoned``) and never waited for permission. An ``uncertain``
        admission, a satisfied slot, a gap, the attempt limit or a passed ``deadline`` also
        refuse it, and so does a conflicting or unreadable receipt under its own key; a
        valid one is bound as ``uncertain``. These are the store's absolute rules only:
        this method does not check that the work never started. Row F6 must hold a fresh
        F3 verdict; ``verdict`` is stored as a label and never read.

        An ``assignment_id`` that is not exactly ``str`` or not 1–120 of the ID characters
        raises ``ValueError`` with a fixed text before the file is opened.
        """
        assignment_id = _check_id(assignment_id)
        member = _check_recipient(recipient)
        if type(attempt) is not int or not 1 <= attempt <= ATTEMPT_CAP:
            raise ValueError(f"attempt must be an integer from 1 to {ATTEMPT_CAP}")
        if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
            raise ValueError("Unknown slot")
        reason = _check_text(reason, "reason", 200)
        if verdict is not None and (
            not isinstance(verdict, str) or not _VERDICT.fullmatch(verdict)
        ):
            raise ValueError("verdict must be 1–32 lowercase letters, _ or +")
        evidence_text = None
        if evidence is not None:
            evidence_text = canonical(dict(evidence))
            if len(evidence_text) > 4096:
                raise ValueError("evidence must encode to at most 4096 characters")
        given = None if message is None else _check_message(message)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._head(db, assignment_id)
            rows = db.execute(
                "SELECT * FROM assignment_members WHERE assignment_id=? AND slot=? "
                "ORDER BY attempt",
                (assignment_id, slot),
            ).fetchall()
            if not rows:
                raise ValueError("Unknown slot")
            seen = {row["member_key"]: self._observe(row) for row in rows}
            won = any(row["winner"] for row in rows) or any(
                _valid_completion(item) for item in seen.values()
            )
            expired = head["deadline"] is not None and self.clock() >= head["deadline"]
            for row in rows:
                if row["attempt"] == attempt:
                    others = [other for other in rows if other["attempt"] != attempt]
                    view = self._replan(
                        db,
                        row,
                        rows[-1],
                        member,
                        given,
                        seen[row["member_key"]],
                        won,
                        _unresolved(others, seen),
                        head["deadline"],
                        expired,
                    )
                    return view, False
            latest = rows[-1]
            if attempt != latest["attempt"] + 1:
                raise ValueError(f"The next attempt in this slot is {latest['attempt'] + 1}")
            limit = 1 if head["failover"] == "off" else head["max_attempts"]
            if attempt > limit:
                raise ValueError("This slot has no attempts left")
            if expired:
                raise ValueError("The assignment's deadline has passed; no successor")
            if member.peer_id == head["independent_of"]:
                raise ValueError("peer_id is the assignment's independent_of peer")
            sibling = db.execute(
                "SELECT 1 FROM assignment_members WHERE assignment_id=? AND slot!=? "
                "AND (peer_id=? OR target=?)",
                (assignment_id, slot, member.peer_id, member.target),
            ).fetchone()
            if sibling is not None:
                raise ValueError("peer_id already serves another slot of this assignment")
            if won:
                raise ValueError("The slot is satisfied; no successor")
            older = _unresolved(rows[:-1], seen)
            if older:
                raise ValueError(f"An earlier attempt is unresolved ({older[0]}); no successor")
            parent = self._settled_parent(latest, seen[latest["member_key"]])
            identity = canonical(json.loads(head["message_identity"]) if given is None else given)
            key = _member_key(head["kind"], assignment_id, slot, attempt)
            own = self._by_key(
                key,
                {
                    "member_key": key,
                    "target": member.target,
                    "provider": member.provider,
                    "message_identity": identity,
                },
            )
            if own.lookup in ("conflicting", "unavailable"):
                raise ValueError(
                    f"The receipt under {key} is {own.lookup}; the attempt cannot be planned"
                )
            bound = own.task_id if own.lookup == "valid" else None
            now = self.clock()  # after every lookup, in the transaction that writes the row
            if head["deadline"] is not None and now >= head["deadline"]:
                raise ValueError("The assignment's deadline has passed; no successor")
            try:
                db.execute(
                    "INSERT INTO assignment_members (assignment_id, slot, ordinal, attempt,"
                    " peer_id, target, provider, local, member_key, message_identity, admission,"
                    " task_id, parent_task_id, reason, verdict, evidence, detail, created,"
                    " updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        assignment_id,
                        slot,
                        latest["ordinal"],
                        attempt,
                        member.peer_id,
                        member.target,
                        member.provider,
                        int(member.local),
                        key,
                        identity,
                        "planned" if bound is None else "uncertain",
                        bound,
                        parent,
                        reason,
                        verdict,
                        evidence_text,
                        None if bound is None else "a receipt existed before the attempt row",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("A member key already belongs to another assignment") from error
            return self._attempt_view(self._member(db, key), own, sendable=bound is None), True

    def _replan(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        latest: sqlite3.Row,
        member: Recipient,
        given: dict[str, Any] | None,
        seen: _Seen,
        won: bool,
        older: Sequence[str],
        deadline: float | None,
        expired: bool,
    ) -> dict[str, Any]:
        """The repeat of an existing attempt: same recipient, same message, or refused."""
        stored = (row["peer_id"], row["target"], row["provider"], bool(row["local"]))
        if stored != tuple(member) or (
            given is not None and json.loads(row["message_identity"]) != given
        ):
            raise ValueError(f"attempt {row['attempt']} already describes different work")
        key = row["member_key"]
        sendable = row["admission"] == "planned" or (
            row["admission"] == "not_sent" and row["attempt"] == latest["attempt"]
        )
        if row["task_id"] is not None or not sendable:
            return self._attempt_view(row, seen)  # bound, uncertain, or not the latest
        if seen.lookup == "valid":
            assert seen.receipt is not None
            planned = row["admission"] == "planned"
            db.execute(
                "UPDATE assignment_members SET task_id=?, admission=?, detail=?, updated=? "
                "WHERE member_key=?",
                (
                    seen.task_id,
                    "submitted" if planned else "uncertain",
                    row["detail"]
                    if planned
                    else "a receipt appeared for an attempt recorded as not sent",
                    self.clock(),
                    key,
                ),
            )
            return self._attempt_view(self._member(db, key), seen)
        if seen.lookup != "absent":
            raise ValueError(
                f"The receipt under {key} is {seen.lookup}; the attempt cannot be planned again"
            )
        if won:
            raise ValueError("The slot is satisfied; the attempt cannot be planned again")
        if older:
            raise ValueError(
                f"An earlier attempt is unresolved ({older[0]}); "
                "the attempt cannot be planned again"
            )
        now = self.clock()  # after every lookup, in the transaction that writes the row
        # An expiry seen earlier in this call is kept when the clock steps back (review 4,
        # D2); the final reading still decides when time moved forward (N4).
        if row["attempt"] > 1 and (expired or (deadline is not None and now >= deadline)):
            raise ValueError("The assignment's deadline has passed; no successor")
        if row["admission"] == "not_sent":
            db.execute(
                "UPDATE assignment_members SET admission='planned', updated=? WHERE member_key=?",
                (now, key),
            )
        return self._attempt_view(self._member(db, key), seen, sendable=True)

    def _settled_parent(self, latest: sqlite3.Row, seen: _Seen) -> str | None:
        """Task ID of the attempt a successor follows; refuse when it has not settled."""
        if latest["admission"] == "uncertain":
            raise ValueError("The previous attempt is uncertain; no successor")
        if seen.lookup == "absent":
            if latest["admission"] == "not_sent" and latest["task_id"] is None:
                return None
            raise ValueError("The previous attempt is still planned; no successor")
        if seen.lookup != "valid" or seen.receipt is None:
            raise ValueError(f"The previous attempt's receipt is {seen.lookup}; no successor")
        if latest["admission"] == "not_sent":
            raise ValueError("A receipt exists for the attempt recorded as not sent; no successor")
        receipt = seen.receipt
        if _metadata_flag(receipt, "blocked_seen"):
            raise ValueError("The previous attempt waited for permission; no successor")
        status = receipt.get("status")
        if status not in SETTLED_UNSUCCESSFUL:
            raise ValueError(f"The previous attempt is {_status_word(status)}; no successor")
        return seen.task_id

    def record_submitted(self, member_key: str, task_id: str) -> dict[str, Any]:
        """Bind an attempt to the receipt its adapter call returned.

        The receipt must be valid for this attempt: same key, target, provider and
        message. Binding the same receipt again is a no-op; a different receipt is refused.
        An attempt already recorded as ``uncertain`` stays ``uncertain``.

        A ``member_key`` or a ``task_id`` that is not exactly ``str``, not 1–128 characters
        or not text that UTF-8 can encode raises ``ValueError`` with a fixed text before the
        file is opened and before the lookup is called; the member key is checked first.
        The same rule holds for the receipt: one whose ``id`` is longer than 128 characters
        is not valid.
        """
        member_key = _check_key(member_key, "member_key")
        task_id = _check_key(task_id, "task_id")
        with self._connection() as db:
            db.execute("BEGIN")
            row = self._member(db, member_key)
        seen = self._by_task(task_id, dict(row))
        if seen.lookup == "conflicting":
            raise ValueError(f"The receipt does not belong to this attempt ({seen.reason})")
        if seen.lookup != "valid":
            raise ValueError(f"The receipt cannot be read ({seen.reason})")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_key)
            if row["task_id"] is None:
                db.execute(
                    "UPDATE assignment_members SET task_id=?, updated=?, admission=CASE "
                    "WHEN admission IN ('uncertain','not_sent') THEN 'uncertain' "
                    "ELSE 'submitted' END WHERE member_key=?",
                    (task_id, self.clock(), member_key),
                )
            elif row["task_id"] != task_id:
                raise ValueError("The receipt does not belong to this attempt")
            return self._attempt_view(self._member(db, member_key), seen)

    def record_raised(self, member_key: str, detail: str) -> dict[str, Any]:
        """Record that the adapter call for ``member_key`` raised.

        The key is looked up after the call. Only a successful lookup that finds nothing,
        for a local recipient, makes the attempt ``not_sent`` (a repeat may fill it).
        Every other outcome makes it ``uncertain``: a valid receipt is bound, and an
        earlier binding by a concurrent read does not hide the failure. ``detail`` is a
        short label such as the exception class, never message or result text.

        A ``member_key`` that is not exactly ``str`` or not 1–128 characters raises
        ``ValueError`` with a fixed text, before ``detail`` is read and before the file is
        opened.
        """
        member_key = _check_key(member_key, "member_key")
        label = (detail if isinstance(detail, str) and detail.strip() else "error")[:200]
        with self._connection() as db:
            db.execute("BEGIN")
            row = self._member(db, member_key)
        seen = self._by_key(member_key, dict(row))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._member(db, member_key)
            now = self.clock()
            if seen.lookup == "absent" and row["task_id"] is None and row["local"]:
                db.execute(
                    "UPDATE assignment_members SET admission='not_sent', detail=?, "
                    "updated=? WHERE member_key=?",
                    (label, now, member_key),
                )
            else:
                if seen.lookup == "absent" and not row["local"]:
                    label = ("remote recipient: a lookup miss is not proof; " + label)[:200]
                elif seen.lookup != "valid":
                    label = (f"receipt {seen.lookup}: {seen.reason}; " + label)[:200]
                bind = (
                    seen.task_id
                    if seen.lookup == "valid" and seen.receipt is not None and not row["task_id"]
                    else row["task_id"]
                )
                db.execute(
                    "UPDATE assignment_members SET task_id=?, admission='uncertain', "
                    "detail=?, updated=? WHERE member_key=?",
                    (bind, label, now, member_key),
                )
            return self._attempt_view(self._member(db, member_key), seen)

    # ------------------------------------------------------------------ reads

    def get(
        self, assignment_id: str, *, assessment: Mapping[str, Assessment] | None = None
    ) -> dict[str, Any]:
        """Return the assignment with every slot and attempt, its status computed now.

        Three questions: Q1 folds each attempt's possible terminal hold; Q2 checks
        completion across the slot and retains a winner whose completion lost validity;
        Q3 reads execution, stall, verdict, candidates, eligibility, deadline and cap
        from the latest attempt. Blocking precedes contradictions, then ordinary status.

        Q1's receipt evidence table:

        - ``none``: absent lookup, no task ID, admission ``planned``/``not_sent``.
        - ``ended``: valid ``completed``/``failed``/``cancelled``, admission other than
          ``uncertain``; not invalidated.
        - ``may_hold``: everything else, including ``abandoned`` with any admission,
          every uncertain-admission final and every invalidated receipt.

        ``assessment`` maps member keys to current facts bound to their own task IDs.
        It is ``None`` (as is an empty mapping: no assessment) or a mapping whose keys
        are text; any other value raises ``ValueError`` before a receipt is read.
        Supply ``core_blocked`` for every attempt in ``unfinished_attempts`` (exactly
        ``may_hold``). A true Core fact on any other attempt also blocks and is named
        as a contradiction. Missing or stale Core evidence is unknown for ``may_hold``;
        a known block takes precedence, while absence diagnostics retain every missing
        observation. ``stalled``, ``verdict`` and ``no_candidate`` concern only the latest.

        ``assessment_absent``, ``assessment_stale`` and ``assessment_contradictions``
        retain their latest-attempt meanings. ``assessment_stale_attempts`` lists all
        mismatched task bindings. ``assessment_absent_attempts`` and
        ``assessment_contradiction_attempts`` add one entry per affected attempt:
        ``{attempt, member_key, facts}``, ordered by member key. They include Q1 Core
        facts and the latest attempt's missing facts or stall contradiction.

        Reading binds attempts whose valid receipt is found by key (crash recovery) and
        writes a slot's winner the first time a valid ``completed`` attempt is seen.
        Neither ever changes a receipt. The view is one coherent snapshot. Before it
        writes a bind or a winner, the read checks that the assignment is unchanged and
        otherwise starts again; a read that writes nothing returns the snapshot it took
        before it read the receipts.
        Result text is not included; read it per receipt.

        An ``assignment_id`` that is not exactly ``str`` or not 1–120 of the ID characters
        raises ``ValueError`` with a fixed text, before ``assessment`` is checked and before
        the file is opened.
        """
        assignment_id = _check_id(assignment_id)
        facts = {key: _check_assessment(value, key) for key, value in _outer(assessment).items()}
        for _ in range(_READ_TRIES):
            with self._connection() as db:
                db.execute("BEGIN")
                head = self._head(db, assignment_id)
                rows = self._rows(db, assignment_id)
            unknown = set(facts) - {row["member_key"] for row in rows}
            if unknown:
                raise ValueError(f"Assessment names unknown member keys: {sorted(unknown)}")
            seen = {row["member_key"]: self._observe(row) for row in rows}
            binds = [
                (seen[row["member_key"]].task_id, row["member_key"])
                for row in rows
                if row["task_id"] is None and seen[row["member_key"]].lookup == "valid"
            ]
            winners = self._new_winners(rows, seen)
            if not binds and not winners:
                return self._view(head, rows, seen, facts)
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if _fingerprint(self._rows(db, assignment_id)) != _fingerprint(rows):
                    continue
                now = self.clock()
                for task_id, key in binds:
                    db.execute(
                        "UPDATE assignment_members SET task_id=?, updated=?, admission=CASE "
                        "WHEN admission IN ('uncertain','not_sent') THEN 'uncertain' "
                        "ELSE 'submitted' END WHERE member_key=? AND task_id IS NULL",
                        (task_id, now, key),
                    )
                for key in winners:
                    # Compare-and-set: only when no attempt of the slot has won yet.
                    db.execute(
                        "UPDATE assignment_members SET winner=1, updated=? WHERE member_key=? "
                        "AND NOT EXISTS (SELECT 1 FROM assignment_members AS other WHERE "
                        "other.assignment_id=assignment_members.assignment_id AND "
                        "other.slot=assignment_members.slot AND other.winner=1)",
                        (now, key),
                    )
                rows = self._rows(db, assignment_id)
            return self._view(head, rows, seen, facts)
        raise ValueError("The assignment changed during every read; read it again")

    def locate(
        self, *, task_id: str | None = None, member_key: str | None = None
    ) -> dict[str, Any] | None:
        """Find the attempt that holds ``task_id`` or ``member_key``; ``None`` if none.

        Give exactly one of the two. Returns ``assignment_id``, ``slot``, ``attempt``,
        ``member_key`` and ``task_id``; read the rest with ``get``. Only bound task IDs
        are found: an attempt whose receipt is not yet bound is found by its key. So a
        ``None`` for a task ID does not prove that no assignment refers to the receipt;
        also look its ``request_key`` up as a member key.

        The one that is given must be exactly ``str`` (no subclass) of 1–128 characters
        that UTF-8 can encode; otherwise ``ValueError`` with a fixed text is raised. The
        rule is the one a receipt's ``id`` passes before it is
        stored, so ``locate`` can be asked for a task ID that the store has bound.
        """
        if (task_id is None) == (member_key is None):
            raise ValueError("Give exactly one of task_id and member_key")
        column, value = ("task_id", task_id) if task_id is not None else ("member_key", member_key)
        value = _check_key(value, column)
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute(
                f"SELECT assignment_id, slot, attempt, member_key, task_id "
                f"FROM assignment_members WHERE {column}=?",
                (value,),
            ).fetchone()
        return None if row is None else dict(row)

    @contextmanager
    def _existing(self) -> Iterator[sqlite3.Connection]:
        """Open the store's file for ``describe``; a file that is not there is never made.

        ``mode=rw`` and not ``mode=ro``: through a read-only connection the first read of a
        store that needs recovery after an interrupted write raises, because SQLite cannot
        run the rollback. ``as_uri()`` escapes every character of the path that a URI
        gives a meaning (``?``, ``#``, ``%``).
        """
        uri = f"{self.path.absolute().as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=10, factory=_PlainConnection)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def describe(self, assignment_id: str) -> dict[str, Any] | None:
        """Return the arguments of the ``create()`` call that stored ``assignment_id``.

        ``None`` for an ID that no assignment has. Otherwise a new mapping with exactly
        the keys ``kind``, ``recipients``, ``quorum``, ``route``, ``failover``,
        ``max_attempts``, ``independent_of`` and ``deadline``: the keyword arguments of
        that call with the message left out. The message is not stored and is not
        returned in any form. ``quorum`` is ``None`` or the integer that was given, and
        ``deadline`` is ``None`` or a float.

        What it is for: ``create(assignment_id, message=m, **describe(assignment_id))``
        returns the stored assignment with ``created=False`` when ``m`` is the message of
        the first call, and is refused by ``create()`` as other work for any other
        message. So a caller that is given an ID again can ask ``create()`` whether it is
        the same work, and nothing but ``create()`` computes the identity. The condition:
        that repeat runs ``create()``'s own receipt checks first, and they refuse the
        message of the first call too when a receipt under a member key cannot be read
        or holds other work.

        ``recipients`` is a list of the ``Recipient`` of attempt 1 of each slot, in the
        order in which ``create()`` stored them. The recipient of a later attempt (a
        successor planned for another peer) is never among them.

        It reads the two tables in one read transaction, calls no method of the receipt
        lookup and changes no row: no bind and no winner, unlike ``get``. It is no status
        read, and its answer does not show whether anything was sent.

        It never makes the store's file: a missing file or folder raises
        ``sqlite3.OperationalError`` and nothing is made. A store that needs no recovery
        is read without any change to a file, also when this user may not write it. When
        SQLite finds that the store needs recovery after an interrupted write, the first
        read runs SQLite's own rollback, as the first read of ``get`` does. That
        rollback is the only way in which ``describe`` can change a file. It can fail
        with an ``sqlite3.Error`` when the file or its folder may not be written, and
        the store's files may then already be partly restored.

        An ID that neither kind of assignment can have (not exactly ``str``, so also an
        instance of a subclass of it, or not 1–120 of the ID characters) raises
        ``ValueError`` with a fixed text before the file is opened.
        A store that cannot be read raises ``sqlite3.Error`` as every other read of this
        store does, after the connection's wait of 10 seconds for a lock, with no retry.
        """
        assignment_id = _check_id(assignment_id)
        with self._existing() as db:
            db.execute("BEGIN")
            head = db.execute(
                "SELECT kind, rule, route, failover, max_attempts, independent_of, deadline "
                "FROM assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if head is None:
                return None
            rows = db.execute(
                "SELECT peer_id, target, provider, local FROM assignment_members "
                "WHERE assignment_id=? AND attempt=1 ORDER BY ordinal",
                (assignment_id,),
            ).fetchall()
        return {
            "kind": head["kind"],
            "recipients": [
                Recipient(row["peer_id"], row["target"], row["provider"], bool(row["local"]))
                for row in rows
            ],
            "quorum": None if head["rule"] == "all" else int(head["rule"].split(":", 1)[1]),
            "route": head["route"],
            "failover": head["failover"],
            "max_attempts": head["max_attempts"],
            "independent_of": head["independent_of"],
            "deadline": head["deadline"],
        }

    @staticmethod
    def _rows(db: sqlite3.Connection, assignment_id: str) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM assignment_members WHERE assignment_id=? ORDER BY ordinal, attempt",
            (assignment_id,),
        ).fetchall()

    @staticmethod
    def _new_winners(rows: Sequence[sqlite3.Row], seen: Mapping[str, _Seen]) -> list[str]:
        by_slot: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_slot.setdefault(row["slot"], []).append(row)
        winners = []
        for slot_rows in by_slot.values():
            if any(row["winner"] for row in slot_rows):
                continue
            for row in slot_rows:
                if _valid_completion(seen[row["member_key"]]):
                    winners.append(row["member_key"])
                    break
        return winners

    def _view(
        self,
        head: sqlite3.Row,
        rows: Sequence[sqlite3.Row],
        seen: Mapping[str, _Seen],
        facts: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        by_slot: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_slot.setdefault(row["slot"], []).append(row)
        limit = 1 if head["failover"] == "off" else head["max_attempts"]
        expired = head["deadline"] is not None and self.clock() >= head["deadline"]
        slots = [
            self._slot_view(slot_rows, seen, facts, limit, expired)
            for slot_rows in by_slot.values()
        ]
        quorum = len(slots) if head["rule"] == "all" else int(head["rule"].split(":", 1)[1])
        counts = {state: 0 for state in SLOT_STATES}
        for slot in slots:
            counts[slot["status"]] += 1
        return {
            "assignment_id": head["assignment_id"],
            "kind": head["kind"],
            "rule": head["rule"],
            "quorum": quorum,
            "route": head["route"],
            "failover": head["failover"],
            "max_attempts": head["max_attempts"],
            "independent_of": head["independent_of"],
            "created": head["created"],
            "deadline": head["deadline"],
            "deadline_passed": expired,
            "status": _assignment_status(
                counts, quorum, any(slot["blocked_unknown"] for slot in slots)
            ),
            "counts": counts,
            # Slots that have not met their part of the rule (neither satisfied nor exhausted).
            "outstanding": [
                slot["slot"] for slot in slots if slot["status"] not in ("satisfied", "exhausted")
            ],
            # Q1: every attempt whose own evidence still permits a terminal hold.
            "unfinished_attempts": [
                {
                    "slot": item["slot"],
                    "attempt": item["attempt"],
                    "task_id": item["task_id"],
                    "lookup": item["lookup"],
                    "receipt_status": item["receipt_status"],
                }
                for slot in slots
                for item in slot["attempts"]
                if _recipient_evidence(item) == "may_hold"
            ],
            "slots": slots,
        }

    @staticmethod
    def _head(db: sqlite3.Connection, assignment_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM assignments WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown assignment ID")
        return row

    @staticmethod
    def _member(db: sqlite3.Connection, member_key: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM assignment_members WHERE member_key=?", (member_key,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown member key")
        return row

    @staticmethod
    def _attempt_view(
        row: sqlite3.Row, seen: _Seen | None, *, sendable: bool = False
    ) -> dict[str, Any]:
        valid = seen is not None and seen.lookup == "valid" and seen.receipt is not None
        receipt = seen.receipt if valid and seen is not None else None
        return {
            "assignment_id": row["assignment_id"],
            "slot": row["slot"],
            "attempt": row["attempt"],
            "peer_id": row["peer_id"],
            "target": row["target"],
            "provider": row["provider"],
            "local": bool(row["local"]),
            "member_key": row["member_key"],
            # Stored fact about the adapter call; never a permission to send.
            "admission": row["admission"],
            # True only in a plan_attempt result that may be sent now (module docstring).
            "sendable": sendable,
            "task_id": row["task_id"],
            # None when this call did not read the receipt.
            "lookup": None if seen is None else seen.lookup,
            "lookup_detail": None if seen is None else seen.reason,
            "receipt_status": None if receipt is None else receipt.get("status"),
            "invalidated": _metadata_flag(receipt, "identity_invalidated"),
            "parent_task_id": row["parent_task_id"],
            "reason": row["reason"],
            "verdict": row["verdict"],
            "evidence": None if row["evidence"] is None else json.loads(row["evidence"]),
            "detail": row["detail"],
            "winner": bool(row["winner"]),
        }

    def _slot_view(
        self,
        rows: Sequence[sqlite3.Row],
        seen: Mapping[str, _Seen],
        facts: Mapping[str, Mapping[str, Any]],
        limit: int,
        expired: bool,
    ) -> dict[str, Any]:
        attempts = [self._attempt_view(row, seen[row["member_key"]]) for row in rows]
        winner = next((item["attempt"] for item in attempts if item["winner"]), None)
        # Each supplied assessment is bound to its own attempt's task ID; a mismatch is
        # stale and ignored. Every attempt's current facts are kept (review 5, R5-1).
        per_attempt = []
        stale_attempts = []
        for item in attempts:
            given = facts.get(item["member_key"])
            stale_one = given is not None and given["task_id"] != item["task_id"]
            if stale_one:
                stale_attempts.append(item["attempt"])
            per_attempt.append({} if given is None or stale_one else given)
        stale = attempts[-1]["attempt"] in stale_attempts
        current = per_attempt[-1]
        reading = _slot_status(attempts, limit, expired, per_attempt)
        return {
            "slot": rows[0]["slot"],
            "ordinal": rows[0]["ordinal"],
            "peer_id": rows[0]["peer_id"],
            "status": reading.status,
            "assessment_absent": reading.absent,
            "assessment_stale": stale,
            # Attempts whose supplied assessment named another receipt (ignored).
            "assessment_stale_attempts": stale_attempts,
            "assessment_contradictions": reading.contradictions,
            "assessment_absent_attempts": list(reading.absent_attempts),
            "assessment_contradiction_attempts": list(reading.contradiction_attempts),
            # Supplied verdict or candidate facts that do not apply to this attempt; ignored.
            "assessment_inapplicable": _inapplicable(attempts[-1], current),
            # True when a may_hold attempt lacks Core evidence and no known block wins.
            "blocked_unknown": reading.blocked_unknown,
            # F3 says auto and F7 says a candidate is left; nothing has been planned yet.
            "retry_eligible": reading.retry_eligible,
            "winner": winner,
            "late_results": [
                item["attempt"]
                for item in attempts
                if _completed(item) and item["attempt"] != winner
            ],
            "attempts": attempts,
        }


def _unresolved(rows: Sequence[sqlite3.Row], seen: Mapping[str, _Seen]) -> list[str]:
    """Attempts whose receipt evidence, read in this call, does not show a settled end.

    Resolved means: never reserved (``not_sent``, no receipt under the key), or a valid
    receipt that is ``completed`` or settled without success, with an admission other
    than ``uncertain`` or ``not_sent`` and no permission wait. Everything else (a
    conflicting or unreadable receipt, a bound receipt that is gone, a live, blocked or
    ``uncertain`` receipt, any receipt under an attempt recorded as ``not_sent``) vetoes
    every sendable result in the slot. A valid receipt under an attempt recorded
    ``not_sent`` contradicts that record: ``get`` binds such an attempt as ``uncertain``,
    and this guard refuses it before that read too. A conflicting or unreadable receipt
    under such an attempt is not bound by ``get``, and the guard refuses it with and
    without that read (review 7, M-2).

    Unlike Q1's terminal-hold question, this planning guard treats abandonment as a
    resolved reservation; an invalidated final receipt passes this guard too; F3 still
    owes the never-started proof before any successor.
    """
    problems = []
    for row in rows:
        item = seen[row["member_key"]]
        if item.lookup == "absent" and row["task_id"] is None and row["admission"] == "not_sent":
            continue
        receipt = item.receipt
        if (
            item.lookup == "valid"
            and receipt is not None
            and receipt.get("status") in TERMINAL_RECEIPT
            and row["admission"] not in ("uncertain", "not_sent")
            and not _metadata_flag(receipt, "blocked_seen")
        ):
            continue
        if item.lookup != "valid" or receipt is None:
            state = item.lookup
        else:
            state = _status_word(receipt.get("status"))
        problems.append(f"attempt {row['attempt']} {state}")
    return problems


def _valid_completion(seen: _Seen) -> bool:
    """A valid ``completed`` receipt that nobody has invalidated."""
    return (
        seen.lookup == "valid"
        and seen.receipt is not None
        and seen.receipt.get("status") == "completed"
        and not _metadata_flag(seen.receipt, "identity_invalidated")
    )


def _completed(item: Mapping[str, Any]) -> bool:
    return (
        item["lookup"] == "valid"
        and item["receipt_status"] == "completed"
        and not item["invalidated"]
    )


def _recipient_evidence(item: Mapping[str, Any]) -> str:
    """Q1: what this attempt's own receipt proves about a possible terminal hold."""
    if (
        item["lookup"] == "absent"
        and item["task_id"] is None
        and item["admission"] in ("planned", "not_sent")
    ):
        return "none"
    if (
        item["lookup"] == "valid"
        and item["receipt_status"] in ("completed", "failed", "cancelled")
        and item["admission"] != "uncertain"
        and not item["invalidated"]
    ):
        return "ended"
    return "may_hold"


class _Blocking(NamedTuple):
    blocked: bool
    unknown: bool
    absent: list[dict[str, Any]]
    contradictions: list[dict[str, Any]]


def _attempt_facts(item: Mapping[str, Any], names: list[str]) -> dict[str, Any]:
    return {"attempt": item["attempt"], "member_key": item.get("member_key"), "facts": names}


def _core_contradictions(
    evidence: str, receipt_blocked: bool, facts: Mapping[str, Any]
) -> list[str]:
    if (facts.get("core_blocked") is True and evidence != "may_hold") or (
        receipt_blocked and facts.get("core_blocked") is False
    ):
        return ["core_blocked"]
    return []


def _blocking(
    attempts: Sequence[Mapping[str, Any]], per_attempt: Sequence[Mapping[str, Any]]
) -> _Blocking:
    """Q1 fold: every affirmative block survives; only may_hold needs a Core fact.

    There is no execution position here. Diagnostics retain their attempt identity,
    ordered by member key so even their representation is invariant under permutation.
    """
    blocked = False
    absent = []
    contradictions = []
    for item, facts in zip(attempts, per_attempt, strict=True):
        evidence = _recipient_evidence(item)
        receipt_blocked = item["lookup"] == "valid" and item["receipt_status"] == "blocked"
        if receipt_blocked or facts.get("core_blocked") is True:
            blocked = True
        if evidence == "may_hold" and "core_blocked" not in facts:
            absent.append(_attempt_facts(item, ["core_blocked"]))
        names = _core_contradictions(evidence, receipt_blocked, facts)
        if names:
            contradictions.append(_attempt_facts(item, names))
    absent.sort(key=lambda entry: entry["member_key"] or "")
    contradictions.sort(key=lambda entry: entry["member_key"] or "")
    return _Blocking(blocked, bool(absent) and not blocked, absent, contradictions)


class _Reading(NamedTuple):
    status: str
    absent: list[str]
    contradictions: list[str]
    blocked_unknown: bool
    retry_eligible: bool = False
    absent_attempts: tuple[dict[str, Any], ...] = ()
    contradiction_attempts: tuple[dict[str, Any], ...] = ()


def _latest_stall_applies(latest: Mapping[str, Any]) -> bool:
    """Q3: can a stall apply to the latest attempt now?

    Keep the existing stall rule: not to unreserved planned/not_sent attempts or final
    receipts with certain admission; to everything else, including unreadable receipts.
    This is separate from Q1: certain abandonment can still hold a terminal while a
    supplied stall remains a contradiction under the latest execution rule.
    """
    if latest["lookup"] == "absent" and latest["task_id"] is None:
        return latest["admission"] not in ("planned", "not_sent")
    if latest["lookup"] != "valid":
        return True
    return latest["receipt_status"] not in TERMINAL_RECEIPT or latest["admission"] == "uncertain"


def _ended_facts_apply(latest: Mapping[str, Any]) -> bool:
    """Verdict and candidates apply to an ended attempt and to a ``not_sent`` one."""
    if latest["lookup"] == "absent" and latest["task_id"] is None:
        return latest["admission"] == "not_sent"
    return latest["lookup"] == "valid" and latest["receipt_status"] in SETTLED_UNSUCCESSFUL


def _contradictions(latest: Mapping[str, Any], facts: Mapping[str, Any]) -> list[str]:
    """Latest compatibility diagnostic: Q1 Core contradictions plus Q3 stall."""
    receipt_blocked = latest["lookup"] == "valid" and latest["receipt_status"] == "blocked"
    found = _core_contradictions(_recipient_evidence(latest), receipt_blocked, facts)
    if facts.get("stalled") is True and not _latest_stall_applies(latest):
        found.append("stalled")
    return found


def _inapplicable(latest: Mapping[str, Any], facts: Mapping[str, Any]) -> list[str]:
    """Supplied verdict or candidate facts that the latest attempt gives no use for."""
    if _ended_facts_apply(latest):
        return []
    return [name for name in ("verdict", "no_candidate") if name in facts]


def _completion_status(attempts: Sequence[Mapping[str, Any]]) -> str | None:
    """Q2: a valid completion satisfies; a lost stored completion needs a decision."""
    if any(_completed(item) for item in attempts):
        return "satisfied"
    if any(item["winner"] for item in attempts):
        return "needs_decision"
    return None


class _Execution(NamedTuple):
    status: str
    absent: list[str]
    eligible: bool = False


def _latest_execution(
    latest: Mapping[str, Any], facts: Mapping[str, Any], limit: int, expired: bool
) -> _Execution:
    """Q3: the existing latest-attempt state, stall, verdict, candidate and cutoff rules."""

    def reading(status: str, absent: list[str] | None = None, eligible: bool = False) -> _Execution:
        return _Execution(status, absent or [], eligible)

    lookup, status = latest["lookup"], latest["receipt_status"]
    # Eligibility, decided in one place. A deadline that has passed is the same cutoff for
    # every successor (review 5, R5-2): it also ends the eligibility of a ``not_sent``
    # attempt 2 or later, whose refill plan_attempt refuses. Attempt 1 is exempt.
    successor = latest["attempt"] > 1 or latest["admission"] != "not_sent"
    eligible = (
        facts.get("verdict") == "auto"
        and facts.get("no_candidate") is False
        and not (expired and successor)
    )
    if lookup == "absent" and latest["task_id"] is None:
        if latest["admission"] == "not_sent":
            return reading("not_sent", eligible=eligible)
        if latest["admission"] == "planned":
            return reading("planned")
        return reading("needs_decision")
    if lookup != "valid":
        return reading("needs_decision")  # conflicting, unavailable, or bound and gone
    if status in UNFINISHED:
        # Draft: "Core reports blocked" -> blocked (above); "stalled" -> needs_decision.
        absent = [name for name in ("core_blocked", "stalled") if name not in facts]
        if latest["admission"] == "uncertain" or facts.get("stalled") is True or absent:
            return reading("needs_decision", absent)
        return reading("active")
    if latest["admission"] == "uncertain":
        return reading("needs_decision")
    if status in SETTLED_UNSUCCESSFUL:
        if latest["attempt"] >= limit or expired:
            return reading("exhausted")
        if facts.get("no_candidate") is True:
            return reading("exhausted")  # draft: "no candidate left within policy"
        # An auto verdict with a candidate makes a successor eligible; it does not make
        # one exist. The slot stays needs_decision until F6 plans attempt n+1, which then
        # reads planned (draft diagram: "auto verdict, attempt n+1 in the same slot").
        absent = [name for name in ("verdict", "no_candidate") if name not in facts]
        return reading("needs_decision", absent, eligible)
    return reading("needs_decision")  # uncertain, blocked, or a state this store does not know


def _slot_status(
    attempts: list[dict[str, Any]],
    limit: int,
    expired: bool,
    per_attempt: Sequence[Mapping[str, Any]],
) -> _Reading:
    """Compose Q1 (every attempt), Q2 (whole slot), Q3 (latest execution).

    Priority stays blocked, then contradictions, then the receipt-derived status.
    Unknown Core evidence never authorizes satisfaction at assignment aggregation.
    Compatibility diagnostics describe the latest attempt; additive diagnostics name
    every attempt whose Core fact was missing or contradicted its receipt evidence.
    """
    latest, facts = attempts[-1], per_attempt[-1]
    blocking = _blocking(attempts, per_attempt)
    contradictions = _contradictions(latest, facts)
    contradiction_attempts = [
        dict(entry, facts=list(entry["facts"])) for entry in blocking.contradictions
    ]
    if "stalled" in contradictions:
        for entry in contradiction_attempts:
            if entry["attempt"] == latest["attempt"]:
                entry["facts"].append("stalled")
                break
        else:
            contradiction_attempts.append(_attempt_facts(latest, ["stalled"]))
        contradiction_attempts.sort(key=lambda entry: entry["member_key"] or "")
    if blocking.blocked:
        execution = _Execution("blocked", [])
    elif contradictions:
        execution = _Execution("needs_decision", [])
    else:
        completion = _completion_status(attempts)
        execution = (
            _Execution(completion, [])
            if completion is not None
            else _latest_execution(latest, facts, limit, expired)
        )
    absent_attempts = [dict(entry, facts=list(entry["facts"])) for entry in blocking.absent]
    if execution.absent:
        for entry in absent_attempts:
            if entry["attempt"] == latest["attempt"]:
                entry["facts"] = list(dict.fromkeys([*entry["facts"], *execution.absent]))
                break
        else:
            absent_attempts.append(_attempt_facts(latest, execution.absent))
        absent_attempts.sort(key=lambda entry: entry["member_key"] or "")
    return _Reading(
        execution.status,
        execution.absent,
        contradictions,
        blocking.unknown,
        execution.eligible,
        tuple(absent_attempts),
        tuple(contradiction_attempts),
    )


def _assignment_status(counts: Mapping[str, int], quorum: int, blocked_unknown: bool) -> str:
    """Assignment status in the draft's order (section 3, second table).

    A slot whose Core state is unknown might be blocked, and ``blocked`` comes before
    every rule below it, so such a slot makes the assignment ``needs_decision``.
    """
    if counts["planned"]:
        return "admitting"
    if counts["blocked"]:
        return "blocked"
    if blocked_unknown:
        return "needs_decision"
    satisfied = counts["satisfied"]
    if satisfied >= quorum:
        return "satisfied"
    if satisfied + counts["active"] >= quorum:
        return "active"
    if satisfied + counts["active"] + counts["needs_decision"] + counts["not_sent"] >= quorum:
        return "needs_decision"
    return "unsatisfied"
