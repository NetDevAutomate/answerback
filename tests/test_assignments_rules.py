"""What the assignment store refuses (fan-out row F4-S, board rows T-142 and T-143).

Part A: the schema refuses a pair of ``admission`` and ``task_id`` that no call of the store
writes. Part B: a caller's assignment ID, member key or task ID is checked before the store's
file is opened.

Labels. ``red_at_pretip`` means the test fails at ``3069b1bb``, the commit this row started
from; ``pins_existing`` means it passes there on purpose. Where a parametrized test holds
both, its docstring says which cases are which.

Where the inputs come from. Assignment IDs and recipients have the forms that ``create()``
accepts (``assignments.py``: ``_SINGLE_ID``, ``_GROUP_ID``, ``_check_recipient``); targets
follow ``CoreTasks.target_key`` (``hc:<session>:<terminal>``). Member keys are the ones the
store derives (``_member_key``). Receipts have the form that ``TaskStore.decode`` returns:
``id``, ``request_key``, ``target``, ``message``, ``status`` and a ``metadata`` mapping with
``kind``. The values that are not text are what a caller of a Python method can pass.
"""

import ast
import enum
import hashlib
import inspect
import itertools
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge import assignments
from answerback_bridge.adapters.contracts import STATUSES
from answerback_bridge.assignments import AssignmentStore, Recipient

MESSAGE = "Review the change on branch akirem/f4s-impl"
T1 = Recipient("hc:t1", "hc:default:t1", "hc", True)
MISMATCH = r"^Assignment database schema does not match this bridge$"
NAMED = r"^CHECK constraint failed: assignment_admission_task$"
WRITE_ONCE = r"^assignment attempt receipt is write-once$"


class Receipts:
    """Receipts in the form ``TaskStore.decode`` returns; every lookup is counted."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    def add(self, task_id: str, request_key: str, status: str = "running") -> str:
        self.rows[task_id] = {
            "id": task_id,
            "request_key": request_key,
            "target": T1.target,
            "message": MESSAGE,
            "status": status,
            "metadata": {"kind": "hc"},
        }
        return task_id

    def by_request(self, request_key: str) -> dict[str, Any] | None:
        self.calls.append(("by_request", request_key))
        for row in self.rows.values():
            if row["request_key"] == request_key:
                return dict(row, metadata=dict(row["metadata"]))
        return None

    def get(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("get", task_id))
        if task_id not in self.rows:
            raise ValueError("Unknown task ID")
        row = self.rows[task_id]
        return dict(row, metadata=dict(row["metadata"]))


def make(folder: Path) -> tuple[AssignmentStore, Receipts]:
    receipts = Receipts()
    return AssignmentStore(folder / "assignments.sqlite", receipts, clock=lambda: 1000.0), receipts


def members(path: Path) -> list[tuple[Any, ...]]:
    """Every row of the member table, read with a plain ``sqlite3`` connection."""
    with closing(sqlite3.connect(path)) as db:
        return [
            tuple(row) for row in db.execute("SELECT * FROM assignment_members ORDER BY member_key")
        ]


# --------------------------------------------------------------------------- part A

ADMISSIONS = ("planned", "not_sent", "submitted", "uncertain")
# The rule of part A, written out: (admission, has a task ID) -> allowed.
PAIRS = {
    ("planned", False): True,
    ("planned", True): False,
    ("not_sent", False): True,
    ("not_sent", True): False,
    ("submitted", False): False,
    ("submitted", True): True,
    ("uncertain", False): True,
    ("uncertain", True): True,
}
INSERT_ATTEMPT_2 = (
    "INSERT INTO assignment_members (assignment_id, slot, ordinal, attempt, peer_id, target,"
    " provider, local, member_key, message_identity, admission, task_id, created, updated)"
    " VALUES ('review','0',0,2,'hc:t2','hc:default:t2','hc',1,'review.a2','{}',?,?,1.0,1.0)"
)


def with_attempt_1(folder: Path) -> Path:
    """A fresh store that holds one single assignment, made by ``create()``."""
    store, _ = make(folder)
    store.create("review", kind="single", recipients=[T1], message=MESSAGE)
    return store.path


@pytest.mark.parametrize(("admission", "has_task"), list(PAIRS))
def test_the_eight_pairs_by_insert(tmp_path: Path, admission: str, has_task: bool) -> None:
    """The refused pairs are ``red_at_pretip`` (the schema took them there); the accepted
    pairs are ``pins_existing``.

    Each pair is written as attempt 2 by INSERT through an ordinary connection into a store
    that ``create()`` made. The expected answers are ``PAIRS``, as probe A measured them
    (``F4-S-A-sqlite.json``, ``insert_pairs``).
    """
    path = with_attempt_1(tmp_path)
    before = members(path)
    task = "T1" if has_task else None
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        if PAIRS[admission, has_task]:
            db.execute(INSERT_ATTEMPT_2, (admission, task))
        else:
            with pytest.raises(sqlite3.IntegrityError, match=NAMED):
                db.execute(INSERT_ATTEMPT_2, (admission, task))
    after = members(path)
    if PAIRS[admission, has_task]:
        assert len(after) == 2
        assert (admission, task) in [(row[10], row[11]) for row in after]
    else:
        assert after == before


# Every change of a pair by one UPDATE, as probe A measured it on SQLite 3.53.1
# (``F4-S-A-sqlite.json``, ``update_changes``): the start pair, the target pair and what
# happened. The target's task ID is ``none``, the ``same`` ID as the start row's, or an
# ``other`` one. ``trigger`` is the refusal of ``assignment_task_write_once``; where a change
# breaks both, the trigger answers (it runs before the row is checked).
CHANGES = """
planned/none -> planned/none accepted
planned/none -> planned/other constraint
planned/none -> not_sent/none accepted
planned/none -> not_sent/other constraint
planned/none -> submitted/none constraint
planned/none -> submitted/other accepted
planned/none -> uncertain/none accepted
planned/none -> uncertain/other accepted
not_sent/none -> planned/none accepted
not_sent/none -> planned/other constraint
not_sent/none -> not_sent/none accepted
not_sent/none -> not_sent/other constraint
not_sent/none -> submitted/none constraint
not_sent/none -> submitted/other accepted
not_sent/none -> uncertain/none accepted
not_sent/none -> uncertain/other accepted
submitted/id -> planned/none trigger
submitted/id -> planned/same constraint
submitted/id -> planned/other trigger
submitted/id -> not_sent/none trigger
submitted/id -> not_sent/same constraint
submitted/id -> not_sent/other trigger
submitted/id -> submitted/none trigger
submitted/id -> submitted/same accepted
submitted/id -> submitted/other trigger
submitted/id -> uncertain/none trigger
submitted/id -> uncertain/same accepted
submitted/id -> uncertain/other trigger
uncertain/none -> planned/none accepted
uncertain/none -> planned/other constraint
uncertain/none -> not_sent/none accepted
uncertain/none -> not_sent/other constraint
uncertain/none -> submitted/none constraint
uncertain/none -> submitted/other accepted
uncertain/none -> uncertain/none accepted
uncertain/none -> uncertain/other accepted
uncertain/id -> planned/none trigger
uncertain/id -> planned/same constraint
uncertain/id -> planned/other trigger
uncertain/id -> not_sent/none trigger
uncertain/id -> not_sent/same constraint
uncertain/id -> not_sent/other trigger
uncertain/id -> submitted/none trigger
uncertain/id -> submitted/same accepted
uncertain/id -> submitted/other trigger
uncertain/id -> uncertain/none trigger
uncertain/id -> uncertain/same accepted
uncertain/id -> uncertain/other trigger
"""
CHANGE_ROWS = [
    (start, target, outcome)
    for start, _, target, outcome in (line.split() for line in CHANGES.strip().splitlines())
]


def test_the_change_table_names_every_change_once() -> None:
    """pins_existing (it reads no store): each allowed start pair of ``PAIRS``, to each
    admission without a task ID and with another ID; a start row with a task ID tries the same
    ID too."""
    expected = set()
    for (admission, has_task), allowed in PAIRS.items():
        if not allowed:
            continue
        start = f"{admission}/{'id' if has_task else 'none'}"
        for target in ADMISSIONS:
            expected.add((start, f"{target}/none"))
            expected.add((start, f"{target}/other"))
            if has_task:
                expected.add((start, f"{target}/same"))
    assert len(CHANGE_ROWS) == 48
    assert {(start, target) for start, target, _ in CHANGE_ROWS} == expected


@pytest.mark.parametrize(("start", "target", "outcome"), CHANGE_ROWS)
def test_the_change_table_follows_the_rule(start: str, target: str, outcome: str) -> None:
    """pins_existing (it reads no store): the measured table agrees with the rule. A change
    is accepted exactly when the target pair is allowed and the trigger does not object; the
    trigger objects when the start row holds a task ID and the target does not hold the same
    one, and it answers before the constraint."""
    admission, task = target.split("/")
    trigger_objects = start.endswith("/id") and task != "same"
    allowed = PAIRS[admission, task != "none"]
    assert (outcome == "accepted") == (allowed and not trigger_objects)
    assert (outcome == "trigger") == trigger_objects
    assert (outcome == "constraint") == (not allowed and not trigger_objects)


@pytest.mark.parametrize(("start", "target", "outcome"), CHANGE_ROWS)
def test_every_change_of_a_pair_by_update(
    tmp_path: Path, start: str, target: str, outcome: str
) -> None:
    """The rows that read ``constraint`` are ``red_at_pretip`` (accepted there); the others
    are ``pins_existing``.

    The start row is attempt 2, written by INSERT through an ordinary connection; the change
    is one UPDATE of both columns through another ordinary connection.
    """
    path = with_attempt_1(tmp_path)
    start_admission, start_task = start.split("/")
    admission, task = target.split("/")
    held = "T1" if start_task == "id" else None
    wanted = {"none": None, "same": held, "other": "T2"}[task]
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute(INSERT_ATTEMPT_2, (start_admission, held))
    before = members(path)
    change = "UPDATE assignment_members SET admission=?, task_id=? WHERE member_key='review.a2'"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        if outcome == "accepted":
            assert db.execute(change, (admission, wanted)).rowcount == 1
        else:
            text = NAMED if outcome == "constraint" else WRITE_ONCE
            with pytest.raises(sqlite3.IntegrityError, match=text):
                db.execute(change, (admission, wanted))
    after = members(path)
    if outcome == "accepted":
        assert [(row[10], row[11]) for row in after if row[8] == "review.a2"] == [
            (admission, wanted)
        ]
    else:
        assert after == before


# The schema statements of the assignment store before row F4-S: a literal copy of
# ``_DDL`` at ``3069b1bb``. It is the schema before F4-S and is never updated.
DDL_BEFORE_F4S = (
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


def test_a_store_from_before_this_row_is_refused_and_left_as_it_is(tmp_path: Path) -> None:
    """red_at_pretip: at ``3069b1bb`` this file is the store's own schema and opens.

    The file is built from ``DDL_BEFORE_F4S``, the schema before F4-S (a literal that is
    never updated), with the store's marker and version 1, as a development build before
    this row made it. Opening compares the schema, refuses, and changes no byte.
    """
    path = tmp_path / "assignments.sqlite"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        for statement in DDL_BEFORE_F4S:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={assignments.APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    with pytest.raises(ValueError, match=MISMATCH):
        AssignmentStore(path, Receipts())
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["assignments.sqlite"]


def test_a_fresh_store_opens_again_and_has_version_1(tmp_path: Path) -> None:
    """pins_existing: the control of the test above, and ruling A2 (the version stays 1)."""
    path = with_attempt_1(tmp_path)
    before = path.read_bytes()
    again = AssignmentStore(path, Receipts())
    assert again.describe("review") is not None
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert assignments.SCHEMA_VERSION == 1


def test_the_new_schema_differs_from_the_old_one_in_the_member_table_only() -> None:
    """red_at_pretip: at ``3069b1bb`` the two schemas are the same."""
    with closing(sqlite3.connect(":memory:")) as db:
        for statement in DDL_BEFORE_F4S:
            db.execute(statement)
        old = {row[1]: row[3] for row in assignments._schema(db)}
    new = {row[1]: row[3] for row in assignments._EXPECTED_SCHEMA}
    assert old.keys() == new.keys()
    assert [name for name in new if new[name] != old[name]] == ["assignment_members"]
    assert "CONSTRAINT assignment_admission_task CHECK" in new["assignment_members"]


REFUSED = [pair for pair, allowed in PAIRS.items() if not allowed]


@pytest.mark.parametrize(("admission", "has_task"), REFUSED)
def test_a_pair_written_with_enforcement_off_opens_and_reads(
    tmp_path: Path, admission: str, has_task: bool
) -> None:
    """red_at_pretip (there the file has no constraint and ``integrity_check`` reads ``ok``).

    Ruling A5, as probe A measured it: a connection with ``PRAGMA
    ignore_check_constraints=ON`` writes a refused pair. Opening the store compares the
    schema and reads no row, so the file opens, ``get`` reads the row as it is stored, and
    SQLite's ``PRAGMA integrity_check`` names the table.
    """
    path = with_attempt_1(tmp_path)
    task = "T1" if has_task else None
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "UPDATE assignment_members SET admission=?, task_id=? WHERE member_key='review'",
            (admission, task),
        )
    store = AssignmentStore(path, Receipts())
    attempt = store.get("review")["slots"][0]["attempts"][0]
    assert (attempt["admission"], attempt["task_id"]) == (admission, task)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        assert [row[0] for row in db.execute("PRAGMA integrity_check")] == [
            "CHECK constraint failed in assignment_members"
        ]
        # A column that the constraint does not name can still be written (probe A).
        assert (
            db.execute(
                "UPDATE assignment_members SET updated=2.0 WHERE member_key='review'"
            ).rowcount
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError, match=NAMED):
            db.execute(
                "UPDATE assignment_members SET admission=? WHERE member_key='review'",
                (admission,),
            )


def test_the_pairs_are_all_eight() -> None:
    """pins_existing (it reads no store): ``PAIRS`` covers the four admissions twice."""
    assert set(PAIRS) == set(itertools.product(assignments.ADMISSIONS, (False, True)))
    assert tuple(sorted(ADMISSIONS)) == tuple(sorted(assignments.ADMISSIONS))


# --------------------------------------------------------------------------- part B

ID_TEXT = "assignment_id must be 1–120 letters, digits, dots, colons, _ or -"
MEMBER_TEXT = "member_key must be 1–128 characters"
TASK_TEXT = "task_id must be 1–128 characters"
ONE_OF_TEXT = "Give exactly one of task_id and member_key"
STORED = ("5", "5.0", "1", "0", "a")


class EqFive(str):
    """A text whose ``__str__``, ``__eq__`` and ``__hash__`` answer for the text ``5``."""

    def __str__(self) -> str:
        return "5"

    def __eq__(self, other: object) -> bool:
        return other == "5" if type(other) is str else NotImplemented

    def __hash__(self) -> int:
        return hash("5")


class ConformText(str):
    """A text for which ``sqlite3`` binds the text ``5`` (it asks ``__conform__`` first)."""

    def __conform__(self, protocol: Any) -> Any:
        return "5"


class ConformInt(str):
    """A text for which ``sqlite3`` binds the integer 5."""

    def __conform__(self, protocol: Any) -> Any:
        return 5


# Every value of probe B (``F4-S-B-before.json``), under the label the tables below use.
VALUES: dict[str, Any] = {
    "int": 5,
    "float": 5.0,
    "true": True,
    "false": False,
    "none": None,
    "bytes": b"5",
    "list": ["5"],
    "tuple": ("5",),
    "dict": {"5": 1},
    "object": object(),
    "empty": "",
    "121-chars": "a" * 121,
    "129-chars": "a" * 129,
    "5000-chars": "a" * 5000,
    "control-char": "a\x1fb",
    "line-feed": "5\n",
    "non-ascii-digit": "٥",
    "non-ascii-letter": "é",
    "subclass-eq": EqFive("a"),
    "subclass-conform-text": ConformText("a"),
    "subclass-conform-int": ConformInt("a"),
}
# The ID rule refuses every one of them. The key rule (exactly ``str``, 1 to 128 characters
# that UTF-8 can encode) refuses these; the others are keys of a right shape (see
# ``test_a_key_of_a_right_shape_that_is_not_stored``).
KEY_REFUSED = (
    "int",
    "float",
    "true",
    "false",
    "none",
    "bytes",
    "list",
    "tuple",
    "dict",
    "object",
    "empty",
    "129-chars",
    "5000-chars",
    "subclass-eq",
    "subclass-conform-text",
    "subclass-conform-int",
)
KEY_RIGHT_SHAPE = ("121-chars", "control-char", "line-feed", "non-ascii-digit", "non-ascii-letter")
assert set(KEY_REFUSED) | set(KEY_RIGHT_SHAPE) == set(VALUES)

# name: (the call, its fixed text, the labels it refuses, one of the five or a control)
CALLS: dict[str, tuple[Any, str, tuple[str, ...], bool]] = {
    "get.assignment_id": (lambda s, v: s.get(v), ID_TEXT, tuple(VALUES), True),
    "plan_attempt.assignment_id": (
        lambda s, v: s.plan_attempt(v, "0", 1, T1),
        ID_TEXT,
        tuple(VALUES),
        True,
    ),
    "record_submitted.member_key": (
        lambda s, v: s.record_submitted(v, "5"),
        MEMBER_TEXT,
        KEY_REFUSED,
        True,
    ),
    "record_submitted.task_id": (
        lambda s, v: s.record_submitted("a", v),
        TASK_TEXT,
        KEY_REFUSED,
        True,
    ),
    "record_raised.member_key": (
        lambda s, v: s.record_raised(v, "RuntimeError"),
        MEMBER_TEXT,
        KEY_REFUSED,
        True,
    ),
    "describe.assignment_id": (lambda s, v: s.describe(v), ID_TEXT, tuple(VALUES), False),
    "locate.member_key": (lambda s, v: s.locate(member_key=v), MEMBER_TEXT, KEY_REFUSED, False),
    "locate.task_id": (lambda s, v: s.locate(task_id=v), TASK_TEXT, KEY_REFUSED, False),
}
REFUSALS = [(name, label) for name, call in CALLS.items() for label in call[2]]
FIVE = [name for name, call in CALLS.items() if call[3]]


class RefusingLookup:
    """A receipt lookup whose every method raises; a call is counted first."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def by_request(self, request_key: Any) -> Any:
        self.calls.append("by_request")
        raise AssertionError("the receipt lookup was called")

    def get(self, task_id: Any) -> Any:
        self.calls.append("get")
        raise AssertionError("the receipt lookup was called")


def stocked(folder: Path) -> tuple[Path, Receipts]:
    """Probe B's store: five single assignments for the same recipient, made by ``create()``,
    each with a valid receipt under its member key whose task ID is the same text."""
    store, receipts = make(folder)
    for key in STORED:
        store.create(key, kind="single", recipients=[T1], message=MESSAGE)
    for key in STORED:
        receipts.add(key, key)
    return store.path, receipts


def files(folder: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in sorted(folder.iterdir())}


def refused(call: Any, store: AssignmentStore, value: Any, text: str) -> None:
    """The call raises ``ValueError`` with exactly ``text`` and nothing of the caller."""
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
        call(store, value)
    message = str(caught.value)
    assert message == text
    assert caught.value.args == (text,)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    shown = str.__str__(value) if isinstance(value, str) else repr(value)
    if shown not in ("", "a"):  # the letter a is part of every fixed text
        assert shown not in message


@pytest.mark.parametrize(("name", "label"), REFUSALS)
def test_a_value_that_is_not_an_id_or_a_key_is_refused(
    tmp_path: Path, name: str, label: str
) -> None:
    """One table through the five parameters and through ``describe`` and ``locate``.

    ``red_at_pretip``, as probe B measured at ``3069b1bb``: for the five parameters every
    case (none of them had a check). The values that reached a row there: ``int``,
    ``float``, ``true``, ``false`` and the two ``subclass-conform`` values reached and (but
    for a task ID) changed the row of another text (``5``, ``5.0``, ``1``, ``0``);
    ``subclass-eq`` reached the row of its own text ``a``; as a task ID the two
    ``subclass-conform`` values were stored as ``5`` on the attempt whose receipt is ``a``.
    For the controls ``describe`` and ``locate`` the three subclasses are ``red_at_pretip``
    (they passed the check and read a row) and the other values are ``pins_existing``.
    ``locate`` answers ``None`` for both of its parameters with its own text, before and
    after.
    """
    call, text, _, _ = CALLS[name]
    value = VALUES[label]
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup, clock=lambda: 2000.0)
    before = files(tmp_path)
    if name.startswith("locate.") and value is None:
        text = ONE_OF_TEXT
    refused(call, store, value, text)
    assert files(tmp_path) == before
    assert lookup.calls == []


@pytest.mark.parametrize("name", FIVE)
@pytest.mark.parametrize("label", ["int", "list", "empty", "subclass-conform-text"])
def test_the_refusal_comes_before_the_file_is_opened(tmp_path: Path, name: str, label: str) -> None:
    """red_at_pretip: with the store's file removed after the store was made, each of the
    five refuses in the same way and the file is still not there."""
    call, text, _, _ = CALLS[name]
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup)
    for item in tmp_path.iterdir():
        item.unlink()
    refused(call, store, VALUES[label], text)
    assert list(tmp_path.iterdir()) == []
    assert lookup.calls == []


class Raising(str):
    """A ``detail`` whose ``strip()`` raises: the next statement of ``record_raised``."""

    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("detail was read before the member key was checked")


ORDER = {
    "plan_attempt: a wrong ID and a wrong recipient": (
        lambda s: s.plan_attempt(5, "0", 1, "hc:t1"),
        ID_TEXT,
    ),
    "plan_attempt: a wrong ID and a wrong attempt": (
        lambda s: s.plan_attempt(5, "0", 9, T1),
        ID_TEXT,
    ),
    "get: a wrong ID and a wrong assessment": (lambda s: s.get(5, assessment=5), ID_TEXT),
    "record_submitted: a wrong member key and a wrong task ID": (
        lambda s: s.record_submitted(5, 6),
        MEMBER_TEXT,
    ),
    "record_submitted: a right member key and a wrong task ID": (
        lambda s: s.record_submitted("a", 6),
        TASK_TEXT,
    ),
    "record_submitted: an unknown member key and a wrong task ID": (
        lambda s: s.record_submitted("zz", 6),
        TASK_TEXT,
    ),
    "record_raised: a wrong member key and a detail that raises": (
        lambda s: s.record_raised(5, Raising("x")),
        MEMBER_TEXT,
    ),
}


@pytest.mark.parametrize("case", list(ORDER))
def test_the_check_of_the_id_or_key_comes_first(tmp_path: Path, case: str) -> None:
    """red_at_pretip (ruling B3): the ID or key is checked before every other argument, and
    in ``record_submitted`` the member key before the task ID and the task ID before the
    member key is looked up."""
    call, text = ORDER[case]
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup)
    before = files(tmp_path)
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
        call(store)
    assert str(caught.value) == text
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert files(tmp_path) == before
    assert lookup.calls == []


@pytest.mark.parametrize("value", ["zz", "a" * 120, "A.b:c_d-9"])
def test_an_id_of_the_right_shape_that_is_not_stored(tmp_path: Path, value: str) -> None:
    """pins_existing: ``Unknown assignment ID`` as before, and ``None`` from ``describe``."""
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup)
    before = files(tmp_path)
    with pytest.raises(ValueError, match=r"^Unknown assignment ID$"):
        store.get(value)
    with pytest.raises(ValueError, match=r"^Unknown assignment ID$"):
        store.plan_attempt(value, "0", 1, T1)
    assert store.describe(value) is None
    assert files(tmp_path) == before
    assert lookup.calls == []


RIGHT_SHAPE = {label: VALUES[label] for label in KEY_RIGHT_SHAPE} | {
    "not-stored": "zz",
    "one-char": "z",
    "128-chars": "a" * 128,
}


@pytest.mark.parametrize("label", list(RIGHT_SHAPE))
def test_a_key_of_a_right_shape_that_is_not_stored(tmp_path: Path, label: str) -> None:
    """pins_existing: the key rule asks for exactly ``str`` of 1 to 128 characters that UTF-8
    can encode. Each key of ``RIGHT_SHAPE`` passes it and names no attempt: it reads
    ``Unknown member key``, ``locate`` answers ``None``, and as a task ID it goes to the
    receipt lookup, which does not know it. Among these values are probe B's (a text of 121
    characters, one with a control character, a valid ID followed by a line feed, a
    non-ASCII digit and letter): the ID rule refuses them, the key rule does not."""
    value = RIGHT_SHAPE[label]
    path, receipts = stocked(tmp_path)
    store = AssignmentStore(path, receipts)
    receipts.calls.clear()
    before = files(tmp_path)
    with pytest.raises(ValueError, match=r"^Unknown member key$"):
        store.record_submitted(value, "5")
    with pytest.raises(ValueError, match=r"^Unknown member key$"):
        store.record_raised(value, "RuntimeError")
    assert store.locate(member_key=value) is None
    assert store.locate(task_id=value) is None
    assert receipts.calls == []
    with pytest.raises(ValueError, match=r"^The receipt cannot be read \("):
        store.record_submitted("a", value)
    assert receipts.calls == [("get", value)]
    assert files(tmp_path) == before


def test_a_stored_id_and_key_of_plain_text_still_work(tmp_path: Path) -> None:
    """pins_existing: the control of the table. Plain text that names a stored assignment
    reads it, and a key of one character and its receipt are bound."""
    path, receipts = stocked(tmp_path)
    store = AssignmentStore(path, receipts)
    assert store.describe("5") is not None
    assert store.locate(member_key="5") == {
        "assignment_id": "5",
        "slot": "0",
        "attempt": 1,
        "member_key": "5",
        "task_id": None,
    }
    assert store.record_submitted("a", "a")["task_id"] == "a"
    assert store.record_raised("1", "RuntimeError")["admission"] == "uncertain"
    assert store.get("5")["assignment_id"] == "5"
    view, created = store.plan_attempt("0", "0", 1, T1)
    assert (view["member_key"], created) == ("0", False)
    assert store.locate(task_id="a") is not None


@pytest.mark.parametrize("label", ["subclass-eq", "subclass-conform-text", "subclass-conform-int"])
@pytest.mark.parametrize(("kind", "cap"), [("single", 120), ("fanout", 96)])
def test_create_takes_an_id_that_is_exactly_text(
    tmp_path: Path, label: str, kind: str, cap: int
) -> None:
    """red_at_pretip (ruling B2): ``create`` took a subclass of ``str`` there; with the two
    ``subclass-conform`` values it answered for the stored assignment ``5``. The texts of
    ``create`` stay: they name the limit of the kind."""
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup)
    before = files(tmp_path)
    recipients = [T1] if kind == "single" else [T1, Recipient("hc:t2", "hc:default:t2", "hc", True)]
    refused(
        lambda s, v: s.create(v, kind=kind, recipients=recipients, message=MESSAGE),
        store,
        VALUES[label],
        f"assignment_id must be 1–{cap} letters, digits, dots, colons, _ or -",
    )
    assert files(tmp_path) == before
    assert lookup.calls == []


# --------------------------------------------------------------------------- round 2 (B5)
#
# Ruling B5: the store binds only plain values. Both connections of the store are of a class
# whose ``execute`` refuses a statement, before it runs, unless its parameters are a tuple and
# each one is exactly ``str``, ``int``, ``float`` or ``None`` (the set measured over the four
# assignment test files at ``892698e8``, ``F4-S-B5-before.json``). In this section
# ``red_at_pretip`` means: fails at ``892698e8``, the tip of round 1.

PLAIN_TEXT = "The assignment store binds only a tuple of str, int, float or None"


class Number(enum.IntEnum):
    ONE = 1
    TWO = 2
    LATE = 2_000_000_000


class Word(enum.StrEnum):
    SINGLE = "single"


class IntChild(int):
    pass


class FloatChild(float):
    pass


class BytesChild(bytes):
    pass


class _AlwaysEqual(type):
    """A metaclass whose classes claim to equal every other class."""

    def __eq__(cls, other: object) -> bool:
        return True

    def __hash__(cls) -> int:
        return hash(str)


class ClassLiar(str, metaclass=_AlwaysEqual):
    pass


def conform(base: str, other: Any) -> str:
    """A ``str`` subclass instance with the text ``base`` for which ``sqlite3`` binds ``other``."""

    class Conform(str):
        def __conform__(self, protocol: Any) -> Any:
            return other

    return Conform(base)


class Liar(str):
    """Its own text is ``base``; ``__eq__`` and ``__hash__`` answer for the text ``claim``."""

    claim: str

    def __new__(cls, base: str, claim: str) -> "Liar":
        self = super().__new__(cls, base)
        self.claim = claim
        return self

    def __eq__(self, other: object) -> bool:
        return other == self.claim if type(other) is str else NotImplemented

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.claim)


NOT_PLAIN: dict[str, Any] = {
    "bool": True,
    "IntEnum": Number.ONE,
    "StrEnum": Word.SINGLE,
    "int-subclass": IntChild(1),
    "float-subclass": FloatChild(1.0),
    "bytes": b"a",
    "bytes-subclass": BytesChild(b"a"),
    "subclass-eq": EqFive("a"),
    "subclass-conform-text": ConformText("a"),
    "subclass-conform-int": ConformInt("a"),
    "subclass-class-claims-equality": ClassLiar("a"),
    "list": ["a"],
    "object": object(),
}
WRITES = "UPDATE assignment_members SET detail='written' WHERE member_key=? OR 1"


def peer(number: int) -> Recipient:
    return Recipient(f"hc:t{number}", f"hc:default:t{number}", "hc", True)


def tables(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(path)) as db:
        return {
            "assignments": [tuple(r) for r in db.execute("SELECT * FROM assignments ORDER BY 1")],
            "members": members(path),
        }


def group(folder: Path, clock: Any = None) -> tuple[AssignmentStore, Receipts, dict[str, Any]]:
    """A fan-out group ``grp`` for ``hc:t2`` and ``hc:t3`` (failover on, three attempts) whose
    attempt 1 for ``hc:t2`` is ``not_sent``, so that an attempt 2 can be planned there.

    The last item maps ``slot2``, ``slot3``, ``key2`` and ``key3`` to the slots and member keys.
    """
    receipts = Receipts()
    store = AssignmentStore(
        folder / "assignments.sqlite", receipts, clock=clock or (lambda: 1000.0)
    )
    store.create(
        "grp",
        kind="fanout",
        recipients=[peer(2), peer(3)],
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    names = {}
    for slot in store.get("grp")["slots"]:
        attempt = slot["attempts"][0]
        names["slot" + attempt["peer_id"][-1]] = slot["slot"]
        names["key" + attempt["peer_id"][-1]] = attempt["member_key"]
    store.record_raised(names["key2"], "RuntimeError")
    return store, receipts, names


def new_group(store: AssignmentStore, **change: Any) -> Any:
    """``create`` of the new fan-out group ``new-1`` for ``hc:t7`` and ``hc:t8``."""
    arguments: dict[str, Any] = {"kind": "fanout", "recipients": [peer(7), peer(8)]}
    if "first" in change:
        arguments["recipients"] = [Recipient(*change.pop("first")), peer(8)]
    arguments.update(change)
    return store.create("new-1", message=arguments.pop("message", MESSAGE), **arguments)


def second(store: AssignmentStore, names: dict[str, Any], **change: Any) -> Any:
    """``plan_attempt`` of attempt 2 in the slot of ``hc:t2``, for ``hc:t4``."""
    arguments: dict[str, Any] = {
        "slot": names["slot2"],
        "attempt": 2,
        "recipient": peer(4),
        "reason": "probe",
        "verdict": "never_started",
    }
    arguments.update(change)
    slot, attempt, recipient = (arguments.pop(name) for name in ("slot", "attempt", "recipient"))
    return store.plan_attempt("grp", slot, attempt, recipient, **arguments)


def guarded(call: Any, path: Path) -> None:
    """``call`` raises the guard's ``ValueError`` and neither table changes."""
    before = tables(path)
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
        call()
    assert str(caught.value) == PLAIN_TEXT
    assert caught.value.args == (PLAIN_TEXT,)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert tables(path) == before


# ---- 1. the guard alone


@pytest.mark.parametrize("label", list(NOT_PLAIN))
def test_the_connection_refuses_a_parameter_that_is_not_plain(tmp_path: Path, label: str) -> None:
    """red_at_pretip: the statement ran there for every value that ``sqlite3`` itself binds
    (all but ``list`` and ``object``, which raised ``sqlite3.ProgrammingError``). The statement
    is one that writes every row; no row is written for a refused parameter."""
    store, _, _ = group(tmp_path)
    with store._connection() as db:
        guarded(lambda: db.execute(WRITES, (NOT_PLAIN[label],)), store.path)
        guarded(lambda: db.execute(WRITES + " OR ?", ("a", NOT_PLAIN[label])), store.path)
        assert (
            db.execute("SELECT count(*) FROM assignment_members WHERE detail='written'").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize(
    "parameters", [["a"], {"key": "a"}, "a", None], ids=["list", "dict", "text", "none"]
)
def test_the_connection_refuses_parameters_that_are_not_a_tuple(
    tmp_path: Path, parameters: Any
) -> None:
    """red_at_pretip. A tuple cannot change between the check and the statement; a list or an
    object that acts as a sequence could. The store itself always gives a tuple."""
    store, _, _ = group(tmp_path)
    statement = WRITES.replace("member_key=?", "member_key=:key")
    with store._connection() as db:
        guarded(lambda: db.execute(statement, parameters), store.path)


@pytest.mark.parametrize("value", ["a", 5, 5.5, None], ids=["str", "int", "float", "none"])
def test_the_connection_takes_each_plain_type(tmp_path: Path, value: Any) -> None:
    """pins_existing: the measured set. Each value comes back as it went in, and a statement
    that writes does write."""
    store, _, _ = group(tmp_path)
    with store._connection() as db:
        back = db.execute("SELECT ?", (value,)).fetchone()[0]
        assert (type(back), back) == (type(value), value)
        db.execute(WRITES, (value,))
        assert (
            db.execute("SELECT count(*) FROM assignment_members WHERE detail='written'").fetchone()[
                0
            ]
            == 2
        )
        assert db.execute("SELECT 1").fetchone()[0] == 1  # no parameters at all


def test_both_connections_of_the_store_refuse(tmp_path: Path) -> None:
    """red_at_pretip: the connection for writes and the one ``describe`` reads through."""
    store, _, _ = group(tmp_path)
    with store._connection() as db:
        guarded(lambda: db.execute("SELECT ?", (True,)), store.path)
    with store._existing() as db:
        guarded(lambda: db.execute("SELECT ?", (True,)), store.path)


def _calls() -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(assignments))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def test_the_module_runs_a_statement_in_one_way_only() -> None:
    """pins_existing (step 0 b): ``execute`` on a connection with no parameters or a tuple
    written at the call. The guard stands in that way only, so this test fails when another
    way appears: ``executemany``, ``executescript``, a cursor, named or computed parameters.
    The one other ``execute`` is the guard's own, which hands on the tuple it checked.

    What this test cannot see: it looks for calls written as ``<name>.<method>(...)``. A method
    reached under another name, such as ``run = getattr(db, "executemany")`` and then
    ``run(...)``, is invisible to it, as is a statement that ``sqlite3`` issues itself (the
    ``COMMIT`` or ``ROLLBACK`` at the end of a ``with`` block of a connection)."""
    names = {call.func.attr for call in _calls() if isinstance(call.func, ast.Attribute)}
    assert not names & {"executemany", "executescript", "cursor"}
    executes = [
        call
        for call in _calls()
        if isinstance(call.func, ast.Attribute) and call.func.attr == "execute"
    ]
    handed_on = [call for call in executes if ast.unparse(call.func) == "super().execute"]
    assert [ast.unparse(call) for call in handed_on] in (
        [],  # before the guard
        ["super().execute(sql, parameters)"],  # the guard hands on what it checked
    )
    executes = [call for call in executes if call not in handed_on]
    assert len(executes) >= 40
    assert {ast.unparse(call.func) for call in executes} == {"db.execute"}
    for call in executes:
        assert not call.keywords
        assert len(call.args) in (1, 2)
        assert len(call.args) == 1 or isinstance(call.args[1], ast.Tuple), ast.unparse(call)


def test_every_connection_to_the_file_is_of_the_guarding_class() -> None:
    """red_at_pretip: the module connects three times; the two that open the store's file name
    the class, and the third is the in-memory one that reads the expected schema at import.

    What this test cannot see: a connection made through another name, such as
    ``connect = getattr(sqlite3, "connect")`` and then ``connect(...)``."""
    connects = [
        call
        for call in _calls()
        if isinstance(call.func, ast.Attribute) and call.func.attr == "connect"
    ]
    assert len(connects) == 3
    plain = [call for call in connects if not any(k.arg == "factory" for k in call.keywords)]
    assert [ast.unparse(call) for call in plain] == ["sqlite3.connect(':memory:')"]


# ---- 2. through every public method

# How each argument of a public method is answered when it is not plain. ``entry``: a check at
# the start of the method, with its own text. ``guard``: the argument reaches a statement as
# the object the caller gave, and the guard refuses it there. ``not bound``: the store binds a
# conversion, a slice or a hash of it, or nothing, so the guard does not see the object.
ARGUMENTS = {
    "__init__.path": "not bound",
    "__init__.receipts": "not bound",  # what a receipt it returns does: item 3 below
    "__init__.clock": "not bound",  # what its value does: item 5 below
    "create.assignment_id": "entry",
    "create.kind": "guard",
    "create.recipients": "see Recipient",
    "create.message": "not bound",
    "create.quorum": "entry",
    "create.route": "guard",
    "create.failover": "guard",
    "create.max_attempts": "entry",
    "create.independent_of": "guard",
    "create.deadline": "entry",
    "plan_attempt.assignment_id": "entry",
    "plan_attempt.slot": "guard",
    "plan_attempt.attempt": "entry",
    "plan_attempt.recipient": "see Recipient",
    "plan_attempt.message": "not bound",
    "plan_attempt.reason": "guard",
    "plan_attempt.verdict": "guard",
    "plan_attempt.evidence": "not bound",
    "record_submitted.member_key": "entry",
    "record_submitted.task_id": "entry",
    "record_raised.member_key": "entry",
    "record_raised.detail": "not bound",
    "get.assignment_id": "entry",
    "get.assessment": "not bound",
    "locate.task_id": "entry",
    "locate.member_key": "entry",
    "describe.assignment_id": "entry",
    "Recipient.peer_id": "guard",
    "Recipient.target": "guard",
    "Recipient.provider": "guard",
    "Recipient.local": "entry",
}

# Each case: a ``str`` subclass built on a valid value whose ``__conform__`` returns another
# valid value (for ``kind`` also a member of a ``StrEnum``).
GUARDED: dict[str, tuple[str, Any]] = {
    "create kind": ("create.kind", lambda s, n: new_group(s, kind=conform("fanout", "single"))),
    "create kind StrEnum": (
        "create.kind",
        lambda s, n: s.create("new-1", kind=Word.SINGLE, recipients=[peer(7)], message=MESSAGE),
    ),
    "create route": ("create.route", lambda s, n: new_group(s, route=conform("direct", "bus"))),
    "create failover": (
        "create.failover",
        lambda s, n: new_group(s, failover=conform("off", "never_started")),
    ),
    "create independent_of": (
        "create.independent_of",
        lambda s, n: new_group(s, independent_of=conform("hc:t5", "hc:t6")),
    ),
    "create peer_id": (
        "Recipient.peer_id",
        lambda s, n: new_group(s, first=(conform("hc:t7", "hc:t9"), "hc:default:t7", "hc", True)),
    ),
    "create target": (
        "Recipient.target",
        lambda s, n: new_group(
            s, first=("hc:t7", conform("hc:default:t7", "hc:default:t9"), "hc", True)
        ),
    ),
    "create provider": (
        "Recipient.provider",
        lambda s, n: new_group(s, first=("hc:t7", "hc:default:t7", conform("hc", "codex"), True)),
    ),
    "plan_attempt slot": (
        "plan_attempt.slot",
        lambda s, n: second(s, n, slot=conform(n["slot2"], n["slot3"])),
    ),
    "plan_attempt peer_id": (
        "Recipient.peer_id",
        lambda s, n: second(
            s, n, recipient=Recipient(conform("hc:t4", "hc:t9"), "hc:default:t4", "hc", True)
        ),
    ),
    "plan_attempt target": (
        "Recipient.target",
        lambda s, n: second(
            s,
            n,
            recipient=Recipient("hc:t4", conform("hc:default:t4", "hc:default:t9"), "hc", True),
        ),
    ),
    "plan_attempt provider": (
        "Recipient.provider",
        lambda s, n: second(
            s, n, recipient=Recipient("hc:t4", "hc:default:t4", conform("hc", "codex"), True)
        ),
    ),
    "plan_attempt reason": (
        "plan_attempt.reason",
        lambda s, n: second(s, n, reason=conform("probe", "another reason")),
    ),
    "plan_attempt verdict": (
        "plan_attempt.verdict",
        lambda s, n: second(s, n, verdict=conform("never_started", "other")),
    ),
}


@pytest.mark.parametrize("case", list(GUARDED))
def test_an_argument_that_reaches_a_statement_must_be_plain(tmp_path: Path, case: str) -> None:
    """red_at_pretip: at ``892698e8`` every one of these calls returned and wrote a row, with
    the other value in it where ``__conform__`` gave one; ``plan_attempt slot`` read the other
    slot's rows. Now each raises the guard's text and neither table changes."""
    store, _, names = group(tmp_path)
    guarded(lambda: GUARDED[case][1](store, names), store.path)


def test_the_plain_calls_of_the_guarded_cases_work(tmp_path: Path) -> None:
    """pins_existing: the two calls the cases above are built on, with plain values."""
    store, _, names = group(tmp_path)
    assert new_group(store)[1] is True
    view, created = second(store, names)
    assert (view["sendable"], view["peer_id"], created) == (True, "hc:t4", True)


ENTRY: dict[str, tuple[str, Any, str]] = {
    f"{name} {label}": (argument, lambda s, n, call=call, value=value: call(s, n, value), text)
    for name, argument, call, text in (
        (
            "create quorum",
            "create.quorum",
            lambda s, n, v: new_group(s, quorum=v),
            "quorum must be an integer from 1 to the number of peers (fan-out)",
        ),
        (
            "create max_attempts",
            "create.max_attempts",
            lambda s, n, v: new_group(s, max_attempts=v),
            "max_attempts must be an integer from 1 to 3",
        ),
        (
            "create deadline",
            "create.deadline",
            lambda s, n, v: new_group(s, deadline=Number.LATE if v is Number.TWO else v),
            "deadline must be a finite, positive Unix time",
        ),
        (
            "plan_attempt attempt",
            "plan_attempt.attempt",
            lambda s, n, v: second(s, n, attempt=v),
            "attempt must be an integer from 1 to 3",
        ),
    )
    for label, value in (("bool", True), ("IntEnum", Number.TWO))
} | {
    "create local int": (
        "Recipient.local",
        lambda s, n: new_group(s, first=("hc:t7", "hc:default:t7", "hc", 1)),
        "Recipient local must be true or false",
    ),
    "plan_attempt local int": (
        "Recipient.local",
        lambda s, n: second(s, n, recipient=Recipient("hc:t4", "hc:default:t4", "hc", 1)),
        "Recipient local must be true or false",
    ),
    "create deadline float-subclass": (
        "create.deadline",
        lambda s, n: new_group(s, deadline=FloatChild(2e9)),
        "deadline must be a finite, positive Unix time",
    ),
}


@pytest.mark.parametrize("case", list(ENTRY))
def test_a_number_that_is_not_exactly_a_number_is_refused_at_entry(
    tmp_path: Path, case: str
) -> None:
    """pins_existing: where a number is expected, a ``bool`` and a member of an ``IntEnum`` are
    refused by the method's own check, with its own text, before the file is read."""
    store, _, names = group(tmp_path)
    before = tables(store.path)
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
        ENTRY[case][1](store, names)
    assert str(caught.value) == ENTRY[case][2]
    assert tables(store.path) == before


NOT_BOUND: dict[str, tuple[str, Any, Any]] = {
    # name: (argument, the call with a subclass, the same call with the plain value)
    "create message": (
        "create.message",
        lambda s, n: new_group(s, message=conform(MESSAGE, "another message")),
        lambda s, n: new_group(s),
    ),
    "plan_attempt message": (
        "plan_attempt.message",
        lambda s, n: second(s, n, message=conform(MESSAGE, "another message")),
        lambda s, n: second(s, n, message=MESSAGE),
    ),
    "plan_attempt evidence": (
        "plan_attempt.evidence",
        lambda s, n: second(s, n, evidence={"k": conform("v", "w")}),
        lambda s, n: second(s, n, evidence={"k": "v"}),
    ),
    "record_raised detail": (
        "record_raised.detail",
        lambda s, n: s.record_raised(n["key3"], conform("RuntimeError", "OtherError")),
        lambda s, n: s.record_raised(n["key3"], "RuntimeError"),
    ),
    "get assessment": (
        "get.assessment",
        lambda s, n: s.get("grp", assessment={conform("zz", n["key3"]): {"task_id": "T"}}),
        lambda s, n: s.get("grp", assessment={"zz": {"task_id": "T"}}),
    ),
    # A repeat binds nothing of the argument: the stored row answers.
    "repeat create kind": (
        "create.kind",
        lambda s, n: s.create(
            "grp",
            kind=conform("fanout", "single"),
            recipients=[peer(2), peer(3)],
            message=MESSAGE,
            failover="never_started",
            max_attempts=3,
        ),
        lambda s, n: s.create(
            "grp",
            kind="fanout",
            recipients=[peer(2), peer(3)],
            message=MESSAGE,
            failover="never_started",
            max_attempts=3,
        ),
    ),
    "repeat plan_attempt peer_id": (
        "Recipient.peer_id",
        lambda s, n: s.plan_attempt(
            "grp", n["slot3"], 1, Recipient(conform("hc:t3", "hc:t9"), "hc:default:t3", "hc", True)
        ),
        lambda s, n: s.plan_attempt("grp", n["slot3"], 1, peer(3)),
    ),
}


def outcome(call: Any, store: AssignmentStore, names: dict[str, Any]) -> tuple[Any, Any]:
    try:
        answer: Any = call(store, names)
    except ValueError as error:
        answer = (type(error), str(error))
    return answer, tables(store.path)


@pytest.mark.parametrize("case", list(NOT_BOUND))
def test_an_argument_that_does_not_reach_a_statement_as_given(tmp_path: Path, case: str) -> None:
    """pins_existing: the store binds a hash (``message``), an encoding (``evidence``), a slice
    (``detail``) or nothing (``assessment``, and in the repeats of ``NOT_BOUND`` the
    ``kind`` of a repeated ``create`` and the peer ID of a repeated ``plan_attempt``) of
    these. Other arguments of a repeat are checked or bound: a repeated ``create`` refuses a
    subclass ID, and a repeated ``plan_attempt`` binds its slot in a ``SELECT``, where the
    guard refuses a subclass. For the cases of ``NOT_BOUND`` the guard does not see the
    object, and the call answers and writes exactly as with the plain value: what
    ``__conform__`` would hand over is not asked for in them."""
    _, subclass, plain = NOT_BOUND[case]
    one, _, names = group(tmp_path / "subclass")
    two, _, _ = group(tmp_path / "plain")
    assert outcome(subclass, one, names) == outcome(plain, two, names)


def test_the_list_of_arguments_is_complete() -> None:
    """pins_existing: every parameter of every public method of the store, and every field of
    a recipient, is in ``ARGUMENTS``; every one marked ``guard`` has a case in ``GUARDED``,
    every one marked ``entry`` that is not an ID or a key has one in ``ENTRY``, and every one
    marked ``not bound`` that a caller of a method gives has one in ``NOT_BOUND``."""
    public = [name for name in vars(AssignmentStore) if not name.startswith("_")] + ["__init__"]
    found = {
        f"{name}.{parameter}"
        for name in public
        for parameter in inspect.signature(getattr(AssignmentStore, name)).parameters
        if parameter != "self"
    } | {f"Recipient.{field}" for field in Recipient._fields}
    assert sorted(public) == [
        "__init__",
        "create",
        "describe",
        "get",
        "locate",
        "plan_attempt",
        "record_raised",
        "record_submitted",
    ]
    assert found == set(ARGUMENTS)
    by_answer = {
        answer: {name for name, given in ARGUMENTS.items() if given == answer}
        for answer in ("guard", "entry", "not bound")
    }
    ids_and_keys = {name for name in by_answer["entry"] if name.endswith(("_id", "_key"))}
    assert {argument for argument, _ in GUARDED.values()} == by_answer["guard"]
    assert {argument for argument, _, _ in ENTRY.values()} == by_answer["entry"] - ids_and_keys
    assert {argument for argument, _, _ in NOT_BOUND.values()} - by_answer["guard"] == {
        name for name in by_answer["not bound"] if not name.startswith("__init__")
    }
    assert len(ids_and_keys) == 9


# ---- 3. a receipt whose id is not exactly text (ruling B6)
#
# ``_classify`` takes a receipt's ``id`` only when it is exactly ``str``. A receipt whose ``id``
# is anything else is what a receipt without an ``id`` is: ``unavailable``, with the same
# reason. The ``id`` is the one value of a receipt that reaches a statement
# (``F4-S-B6-before.json``), so the guard never answers for a receipt. In this part
# ``red_at_pretip`` means: fails at ``e177c2f4``, the tip of round 2.


class Same(str):
    """A subclass of ``str`` that changes nothing."""


class NoTruth(str):
    """A subclass of ``str`` whose truth value cannot be asked for."""

    def __bool__(self) -> bool:
        raise RuntimeError("the truth value of a receipt's id was used before its type")


RECEIPT_IDS: dict[str, Any] = {
    "conform-text": lambda: conform("T-1", "T-2"),
    "conform-int": lambda: conform("T-1", 5),
    "lying-eq": lambda: Liar("T-1", "T-2"),
    "empty-subclass": lambda: Same("T-1"),
    "truth-raises": lambda: NoTruth("T-1"),
}
NO_ID: dict[str, Any] = {"absent": None, "none": lambda: None}
SLOT7 = hashlib.sha256(b"hc:t7").hexdigest()[:16]


def place(receipts: Receipts, make_id: Any, key: str, target: str, status: str) -> None:
    """A receipt for ``key`` and ``target``, stored under ``T-1``, whose ``id`` is what
    ``make_id`` returns; with ``make_id`` ``None`` the receipt has no ``id`` at all."""
    receipts.add("T-1", key, status)
    receipts.rows["T-1"].update(target=target)
    if make_id is None:
        del receipts.rows["T-1"]["id"]
    else:
        receipts.rows["T-1"]["id"] = make_id()


# Where the receipt lies (its key, target and status) and the calls made with it there.
PLACES: dict[str, tuple[Any, dict[str, Any]]] = {
    "stored planned": (
        lambda n: (n["key3"], "hc:default:t3", "running"),
        {
            "plan_attempt repeat": lambda s, n: s.plan_attempt("grp", n["slot3"], 1, peer(3)),
            "record_submitted": lambda s, n: s.record_submitted(n["key3"], "T-1"),
            "record_raised": lambda s, n: s.record_raised(n["key3"], "RuntimeError"),
            "get": lambda s, n: s.get("grp"),
            "locate member_key": lambda s, n: s.locate(member_key=n["key3"]),
            "locate task_id": lambda s, n: s.locate(task_id="T-1"),
            "describe": lambda s, n: s.describe("grp"),
            "create repeat": lambda s, n: s.create(
                "grp",
                kind="fanout",
                recipients=[peer(2), peer(3)],
                message=MESSAGE,
                failover="never_started",
                max_attempts=3,
            ),
        },
    ),
    "stored planned, receipt failed": (
        lambda n: (n["key3"], "hc:default:t3", "failed"),
        {
            # with a plain receipt this binds the receipt's id as the successor's parent
            "record_submitted then successor": lambda s, n: (
                s.record_submitted(n["key3"], "T-1"),
                s.plan_attempt(
                    "grp", n["slot3"], 2, peer(4), reason="probe", verdict="never_started"
                ),
            )[1],
        },
    ),
    "stored not_sent": (
        lambda n: (n["key2"], "hc:default:t2", "running"),
        {
            "plan_attempt repeat": lambda s, n: s.plan_attempt("grp", n["slot2"], 1, peer(2)),
            "plan_attempt successor": lambda s, n: second(s, n),
            "record_submitted": lambda s, n: s.record_submitted(n["key2"], "T-1"),
            "record_raised": lambda s, n: s.record_raised(n["key2"], "RuntimeError"),
            "get": lambda s, n: s.get("grp"),
        },
    ),
    "new attempt": (
        lambda n: (n["key2"] + ".a2", "hc:default:t4", "running"),
        {
            "plan_attempt new": lambda s, n: second(s, n),
            "get": lambda s, n: s.get("grp"),
        },
    ),
    "create about to plan": (
        lambda n: (f"g:new-1:{SLOT7}", "hc:default:t7", "running"),
        {"create": lambda s, n: new_group(s)},
    ),
}
RECEIPT_CASES = [
    (where, call, label)
    for where, (_, calls) in PLACES.items()
    for call in calls
    for label in RECEIPT_IDS
]


def with_receipt(folder: Path, where: str, call: str, make_id: Any) -> tuple[Any, Any, Any]:
    """The answer of ``call``, both tables afterwards and the lookups made, for a fresh group
    with one receipt at ``where`` whose ``id`` is what ``make_id`` gives."""
    store, receipts, names = group(folder)
    receipts.calls.clear()
    place(receipts, make_id, *PLACES[where][0](names))
    return (*outcome(PLACES[where][1][call], store, names), receipts.calls)


@pytest.mark.parametrize(
    ("where", "call", "label"), RECEIPT_CASES, ids=[" | ".join(case) for case in RECEIPT_CASES]
)
def test_a_receipt_whose_id_is_not_exactly_text_is_a_receipt_without_an_id(
    tmp_path: Path, where: str, call: str, label: str
) -> None:
    """red_at_pretip for every call that would bind the ``id`` or that took the receipt as
    valid (at ``e177c2f4`` the guard answered, or ``record_submitted`` returned);
    pins_existing for ``locate``, ``describe`` and the calls that never use the receipt.
    Each public method returns or raises exactly what it does for the same receipt with no
    ``id`` (the key absent, and ``None``), both tables are equal afterwards and the same
    lookups were made. The guard's text is not the answer anywhere."""
    given = with_receipt(tmp_path / "given", where, call, RECEIPT_IDS[label])
    assert given[0] != (ValueError, PLAIN_TEXT)
    for name, make_id in NO_ID.items():
        assert given == with_receipt(tmp_path / name, where, call, make_id)


OTHER_WORK = [
    (where, call, label)
    for where, call in (
        ("stored planned", "plan_attempt repeat"),
        ("stored planned", "create repeat"),
        ("stored planned", "get"),
        ("new attempt", "plan_attempt new"),
        ("create about to plan", "create"),
    )
    for label in RECEIPT_IDS
]


@pytest.mark.parametrize(
    ("where", "call", "label"), OTHER_WORK, ids=[" | ".join(case) for case in OTHER_WORK]
)
def test_the_id_of_a_receipt_is_read_before_the_receipt_is_compared(
    tmp_path: Path, where: str, call: str, label: str
) -> None:
    """red_at_pretip (mutant R7: the check behind the comparisons). The receipt names another
    recipient, so with a plain ``id`` it is ``conflicting``. With an ``id`` that is not
    exactly ``str`` it is ``unavailable``, as it is without an ``id``: the answer, the
    tables and the lookups are those of the same receipt without an ``id`` and differ from
    those of the same receipt with the plain ``id``."""

    def answer(folder: str, make_id: Any) -> tuple[Any, Any, Any]:
        store, receipts, names = group(tmp_path / folder)
        receipts.calls.clear()
        key, _, status = PLACES[where][0](names)
        place(receipts, make_id, key, "hc:default:t9", status)
        return (*outcome(PLACES[where][1][call], store, names), receipts.calls)

    given = answer("given", RECEIPT_IDS[label])
    assert given[0] != (ValueError, PLAIN_TEXT)
    assert given == answer("absent", None) == answer("none", lambda: None)
    assert given[:2] != answer("plain", lambda: "T-1")[:2]


EXPECTED_WITHOUT_ID = {
    (
        "stored planned",
        "plan_attempt repeat",
    ): "The receipt under {key3} is unavailable; the attempt cannot be planned again",
    ("stored planned", "record_submitted"): "The receipt cannot be read (receipt has no task ID)",
    ("stored planned", "record_raised"): "returns",
    ("stored planned", "get"): "returns",
    ("stored planned", "locate member_key"): "returns",
    ("stored planned", "locate task_id"): "returns",
    ("stored planned", "describe"): "returns",
    ("stored planned", "create repeat"): "The receipt under request key {key3} cannot be read",
    (
        "stored planned, receipt failed",
        "record_submitted then successor",
    ): "The receipt cannot be read (receipt has no task ID)",
    (
        "stored not_sent",
        "plan_attempt repeat",
    ): "The receipt under {key2} is unavailable; the attempt cannot be planned again",
    (
        "stored not_sent",
        "plan_attempt successor",
    ): "The previous attempt's receipt is unavailable; no successor",
    ("stored not_sent", "record_submitted"): "The receipt cannot be read (receipt has no task ID)",
    ("stored not_sent", "record_raised"): "returns",
    ("stored not_sent", "get"): "returns",
    (
        "new attempt",
        "plan_attempt new",
    ): "The receipt under {key2}.a2 is unavailable; the attempt cannot be planned",
    ("new attempt", "get"): "returns",
    ("create about to plan", "create"): "The receipt under request key g:new-1:"
    + SLOT7
    + " cannot be read",
}


@pytest.mark.parametrize("name", list(NO_ID))
def test_what_a_receipt_without_an_id_gets(tmp_path: Path, name: str) -> None:
    """pins_existing (step 0 b, unchanged by B6): the answer of every call above for a
    receipt without an ``id``, written out. Only ``record_raised`` writes (the attempt
    becomes ``uncertain`` or stays ``not_sent``, with no task ID); no row holds a task ID."""
    assert set(EXPECTED_WITHOUT_ID) == {(where, call) for where, call, _ in RECEIPT_CASES}
    for number, ((where, call), expected) in enumerate(EXPECTED_WITHOUT_ID.items()):
        store, receipts, names = group(tmp_path / str(number))
        place(receipts, NO_ID[name], *PLACES[where][0](names))
        before = tables(store.path)
        answer, after = outcome(PLACES[where][1][call], store, names)
        if expected == "returns":
            assert not (isinstance(answer, tuple) and answer[:1] == (ValueError,)), (where, call)
        else:
            assert answer == (ValueError, expected.format(**names)), (where, call)
        assert (after != before) == (call == "record_raised"), (where, call)
        assert all(row[11] is None and row[12] is None for row in after["members"])


@pytest.mark.parametrize("label", list(RECEIPT_IDS))
def test_a_receipt_whose_id_is_not_plain_is_never_bound(tmp_path: Path, label: str) -> None:
    """red_at_pretip. Round 2 asserted that ``plan_attempt`` (repeat and new), ``record_raised``
    and ``get`` raise the guard's text for such a receipt. Since B6 the receipt is unavailable
    before a statement is reached: none of them raises the guard's text, and no row holds
    a task ID or a parent task ID afterwards. The receipts are a fake in ``TaskStore.decode``'s
    form."""
    for where, call in (
        ("stored planned", "plan_attempt repeat"),
        ("stored planned", "record_raised"),
        ("stored planned", "get"),
        ("new attempt", "plan_attempt new"),
    ):
        answer, after, _ = with_receipt(tmp_path / call, where, call, RECEIPT_IDS[label])
        assert answer != (ValueError, PLAIN_TEXT)
        assert all(row[11] is None and row[12] is None for row in after["members"])


@pytest.mark.parametrize("label", list(RECEIPT_IDS))
def test_record_submitted_refuses_a_receipt_whose_id_is_not_exactly_text(
    tmp_path: Path, label: str
) -> None:
    """red_at_pretip; was ``test_record_submitted_binds_the_task_id_the_caller_gave``. Round 2
    asserted that the receipts with ``__conform__`` are accepted (the caller's ``T-1`` stored)
    and the one that lies about equality is refused as ``lookup returned another task ID``.
    Since B6 each is refused as a receipt without a task ID and nothing is written. With a
    plain receipt the caller's ``task_id`` is stored, as before."""
    store, receipts, names = group(tmp_path / "given")
    place(receipts, RECEIPT_IDS[label], names["key3"], "hc:default:t3", "running")
    before = tables(store.path)
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
        store.record_submitted(names["key3"], "T-1")
    assert str(caught.value) == "The receipt cannot be read (receipt has no task ID)"
    assert tables(store.path) == before
    store, receipts, names = group(tmp_path / "plain")
    place(receipts, lambda: "T-1", names["key3"], "hc:default:t3", "running")
    view = store.record_submitted(names["key3"], "T-1")
    assert (view["admission"], view["task_id"], type(view["task_id"])) == ("submitted", "T-1", str)


@pytest.mark.parametrize("label", list(RECEIPT_IDS))
def test_create_with_such_a_receipt_stores_nothing_and_raises_before_its_transaction(
    tmp_path: Path, label: str
) -> None:
    """red_at_pretip; was ``test_create_with_such_a_receipt_stores_the_assignment_and_then_
    raises``. Round 2 asserted that ``create`` stores the assignment and then raises the
    guard's text from its closing ``get``, on every repeat. Since B6 ``create`` reads the
    receipt under the first key it is about to plan, finds it unavailable and raises there:
    one lookup, no row in either table, and the same on a repeat."""
    store, receipts, _ = group(tmp_path)
    place(receipts, RECEIPT_IDS[label], f"g:new-1:{SLOT7}", "hc:default:t7", "running")
    before = tables(store.path)
    for _ in range(2):
        receipts.calls.clear()
        with pytest.raises(ValueError) as caught:  # noqa: PT011 - the text is compared below
            new_group(store)
        assert str(caught.value) == f"The receipt under request key g:new-1:{SLOT7} cannot be read"
        assert receipts.calls == [("by_request", f"g:new-1:{SLOT7}")]
        assert tables(store.path) == before
        assert store.describe("new-1") is None


# ---- 4. a refusal inside a transaction


def test_a_refusal_inside_create_leaves_no_row(tmp_path: Path) -> None:
    """red_at_pretip: the head row and the first member's row are written before the second
    member's ``peer_id`` reaches its statement; after the refusal neither is there."""
    store, _, _ = group(tmp_path)
    last = Recipient(conform("hc:t8", "hc:t9"), "hc:default:t8", "hc", True)
    guarded(
        lambda: store.create("new-1", kind="fanout", recipients=[peer(7), last], message=MESSAGE),
        store.path,
    )
    assert store.describe("new-1") is None
    assert all(row[0] == "grp" for row in members(store.path))


# ---- 5. the clock


@pytest.mark.parametrize(
    "value",
    [True, FloatChild(1000.0), IntChild(1000)],
    ids=["bool", "float-subclass", "int-subclass"],
)
def test_a_clock_that_returns_no_plain_number_is_refused(tmp_path: Path, value: Any) -> None:
    """red_at_pretip: the value was stored there. Now the first statement that would bind it is
    refused and nothing is written."""
    store = AssignmentStore(tmp_path / "assignments.sqlite", Receipts(), clock=lambda: value)
    guarded(
        lambda: store.create("new-1", kind="single", recipients=[peer(7)], message=MESSAGE),
        store.path,
    )
    assert tables(store.path) == {"assignments": [], "members": []}


def test_a_clock_that_returns_an_integer_or_a_text_is_not_checked(tmp_path: Path) -> None:
    """pins_existing: an ``int`` is a plain number and works. The text ``1000.5``, which UTF-8
    can encode, passes the guard too: the store does not check that a clock returns a number.
    It is stored as that number (the column's affinity) and a comparison with a deadline
    raises ``TypeError``."""
    whole = AssignmentStore(tmp_path / "a.sqlite", Receipts(), clock=lambda: 1000)
    assert whole.create(
        "new-1", kind="single", recipients=[peer(7)], message=MESSAGE, deadline=2e9
    )[1]
    text: Any = lambda: "1000.5"  # noqa: E731
    store = AssignmentStore(tmp_path / "b.sqlite", Receipts(), clock=text)
    assert store.create("new-1", kind="single", recipients=[peer(7)], message=MESSAGE)[1]
    assert tables(store.path)["assignments"][0][9] == 1000.5
    with pytest.raises(TypeError):
        store.create("new-2", kind="single", recipients=[peer(8)], message=MESSAGE, deadline=2e9)


# ---- 6. what a subclass can still do without reaching a statement


def test_a_recipient_that_lies_about_equality_gets_what_the_stored_one_gets(tmp_path: Path) -> None:
    """pins_existing (a known limit, not a refusal): ``_replan`` compares the stored recipient
    with the given one in Python. A recipient ``hc:t9`` whose fields claim to equal the stored
    ``hc:t2`` passes that comparison on a repeat of a ``not_sent`` attempt; nothing of it is
    bound, and the call returns and writes exactly what the call with the stored recipient
    returns and writes."""
    liar = Recipient(
        Liar("hc:t9", "hc:t2"), Liar("hc:default:t9", "hc:default:t2"), Liar("codex", "hc"), True
    )
    answers = []
    for folder, recipient in (("stored", peer(2)), ("liar", liar)):
        store, _, names = group(tmp_path / folder)
        before = tables(store.path)
        view, created = store.plan_attempt("grp", names["slot2"], 1, recipient)
        assert (view["admission"], view["sendable"], view["peer_id"], created) == (
            "planned",
            True,
            "hc:t2",
            False,
        )
        assert tables(store.path) != before
        answers.append((view, created, tables(store.path)))
    assert answers[0] == answers[1]


# --------------------------------------------------------------------------- round 4, B8
#
# A lookup's answer is read once. The measurements are ``F4-S-B8-before.json`` (step 0).

NO_ID_REASON = "receipt has no task ID"
COPY_REASON = "receipt copy failed"


def result(call: Any, store: AssignmentStore, names: dict[str, Any]) -> tuple[Any, Any]:
    """As ``outcome``, for a call that may raise any class: the class and the text."""
    try:
        answer: Any = call(store, names)
    except Exception as error:  # noqa: BLE001 - every class is part of the comparison
        answer = (type(error), str(error))
    return answer, tables(store.path)


class Handing:
    """A lookup that hands out a fresh copy on every call and, when ``change`` is given,
    changes the copies it handed out earlier: at its next call (``on_lookup``) or when
    ``fire`` is called (by the clock)."""

    def __init__(self, inner: Receipts, change: Any = None, on_lookup: bool = False) -> None:
        self.inner, self.change, self.on_lookup = inner, change, on_lookup
        self.out: list[dict[str, Any]] = []

    def fire(self) -> None:
        if self.change is not None:
            for item in self.out:
                self.change(item)
        self.out.clear()

    def _hand(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is not None:
            self.out.append(item)
        return item

    def by_request(self, request_key: str) -> dict[str, Any] | None:
        if self.on_lookup:
            self.fire()
        return self._hand(self.inner.by_request(request_key))

    def get(self, task_id: str) -> dict[str, Any]:
        if self.on_lookup:
            self.fire()
        item = self._hand(self.inner.get(task_id))
        assert item is not None
        return item


def _next_status(item: dict[str, Any]) -> None:
    item["status"] = {"running": "failed", "failed": "completed", "completed": "running"}[
        item["status"]
    ]


# What the owner of a returned receipt does to it after it was returned.
CHANGES: dict[str, Any] = {
    "id none": lambda item: item.update(id=None),
    "id other": lambda item: item.update(id="T-2"),
    "status": _next_status,
    "blocked_seen": lambda item: item["metadata"].update(blocked_seen=True),
    "identity_invalidated": lambda item: item["metadata"].update(identity_invalidated=True),
}
CHANGED_CASES = [
    (where, call, change, by)
    for where, (_, calls) in PLACES.items()
    for call in calls
    for change in CHANGES
    for by in ("lookup", "clock")
]


def handed(folder: Path, where: str, call: str, status: str, change: Any, by: str) -> Any:
    store, receipts, names = group(folder)
    key, target, _ = PLACES[where][0](names)
    place(receipts, lambda: "T-1", key, target, status)
    receipts.calls.clear()
    handing = Handing(receipts, change, on_lookup=by == "lookup")
    store.receipts = handing
    if by == "clock":
        store.clock = lambda: (handing.fire(), 1000.0)[1]
    return (*result(PLACES[where][1][call], store, names), receipts.calls)


@pytest.mark.parametrize(
    ("where", "call", "change", "by"), CHANGED_CASES, ids=[" | ".join(c) for c in CHANGED_CASES]
)
def test_a_receipt_changed_after_it_was_returned_changes_nothing(
    tmp_path: Path, where: str, call: str, change: str, by: str
) -> None:
    """Red at ``8419c9ed`` for the cases with a row that ``F4-S-B8-before.json`` lists under
    ``b_differing``; pins_existing for the others.

    The lookup hands out a receipt (a ``dict`` with a ``metadata`` ``dict``) and changes it
    afterwards: its ``id``, its ``status`` or one of the flags the store reads, either at
    its next call or when the store asks the clock (which the writing methods do after their
    lookups and before their statement). For each status tried, the call answers and raises,
    both tables hold and the lookups made are exactly what they are with a lookup that hands
    out equal objects and never changes one."""
    for status in ("running", "failed", "completed"):
        changed = handed(tmp_path / f"changed-{status}", where, call, status, CHANGES[change], by)
        control = handed(tmp_path / f"control-{status}", where, call, status, None, by)
        assert changed == control, status
        assert "IntegrityError" not in repr(changed[0])


def test_the_changed_dict_of_review_1(tmp_path: Path) -> None:
    """Red at ``8419c9ed`` (``get`` raised ``sqlite3.IntegrityError`` there; the start commit
    stored ``submitted`` without a task ID). Review 1, B1, first case: the first lookup inside
    one ``get`` returns a ``dict`` with the ID ``T1``; the second lookup sets that ``dict``'s
    ``id`` to ``None`` and finds nothing. ``get`` returns, and both tables hold, what they do
    when the first lookup's ``dict`` is never changed."""
    answers = []
    for folder, changes in (("changed", True), ("control", False)):
        store, _ = make(tmp_path / folder)
        view, _ = store.create("a", kind="fanout", recipients=[peer(1), peer(2)], message=MESSAGE)
        keys = [slot["attempts"][0]["member_key"] for slot in view["slots"]]
        first = {
            "id": "T1",
            "request_key": keys[0],
            "target": "hc:default:t1",
            "message": MESSAGE,
            "status": "running",
            "metadata": {"kind": "hc"},
        }

        class Lookup:
            def by_request(self, request_key: str) -> dict[str, Any] | None:
                if request_key == keys[0]:  # noqa: B023 - used inside this iteration only
                    return first  # noqa: B023
                if changes:  # noqa: B023
                    first["id"] = None  # noqa: B023
                return None

        store.receipts = Lookup()  # type: ignore[assignment]
        answers.append((store.get("a"), tables(store.path)))
    assert answers[0] == answers[1]
    bound = [row[11] for row in answers[0][1]["members"]]
    assert sorted(bound, key=repr) == ["T1", None]


class TwoAnswersDict(dict):  # type: ignore[type-arg]
    """Review 1, B1, second case: a ``dict`` whose ``get`` and ``[]`` answer differently for
    ``id``. Its own storage holds the plain ``T-1``."""

    def get(self, key: Any, default: Any = None) -> Any:
        return "T-1" if key == "id" else super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        return conform("T-1", "T-2") if key == "id" else super().__getitem__(key)


class TwoAnswers(Mapping):  # type: ignore[type-arg]
    """The same for a mapping that is not a ``dict``: ``get`` says the plain ``T-1``, ``[]``
    hands out an instance of a subclass of ``str``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: Any, default: Any = None) -> Any:
        return "T-1" if key == "id" else self.data.get(key, default)

    def __getitem__(self, key: Any) -> Any:
        return conform("T-1", "T-2") if key == "id" else self.data[key]

    def __iter__(self) -> Any:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


class Wrapping:
    """A lookup that hands out ``wrap(receipt)`` for every receipt the inner lookup finds."""

    def __init__(self, inner: Receipts, wrap: Any) -> None:
        self.inner, self.wrap = inner, wrap

    def by_request(self, request_key: str) -> Any:
        item = self.inner.by_request(request_key)
        return None if item is None else self.wrap(item)

    def get(self, task_id: str) -> Any:
        return self.wrap(self.inner.get(task_id))


def wrapped(folder: Path, where: str, call: str, wrap: Any) -> tuple[Any, Any, Any]:
    """As ``with_receipt`` for a plain receipt ``T-1`` that the lookup hands out wrapped."""
    store, receipts, names = group(folder)
    receipts.calls.clear()
    place(receipts, lambda: "T-1", *PLACES[where][0](names))
    store.receipts = Wrapping(receipts, wrap)  # type: ignore[assignment]
    return (*result(PLACES[where][1][call], store, names), receipts.calls)


PLACE_CALLS = [(where, call) for where, (_, calls) in PLACES.items() for call in calls]


@pytest.mark.parametrize(("where", "call"), PLACE_CALLS, ids=[" | ".join(c) for c in PLACE_CALLS])
def test_a_mapping_with_two_answers_has_one_class(tmp_path: Path, where: str, call: str) -> None:
    """Red at ``8419c9ed`` for the calls that bind the ``id`` or take the receipt as valid
    (there ``_classify`` asked ``get`` and the statement bound what ``[]`` gave: the guard's
    text out of ``create`` after its rows were committed); pins_existing for the others.

    The store copies the mapping once and reads the copy. A ``dict`` subclass is copied from
    its own storage (``F4-S-B8-before.json``, ``c_dict_reads``), so it is the plain receipt
    ``T-1`` in every method. A mapping that is not a ``dict`` is copied through ``keys()`` and
    ``[]``, so its ``id`` is the subclass instance and it is a receipt without an ID in every
    method: ``create`` refuses it and stores nothing. The guard's text is nowhere the answer.
    """
    as_dict = wrapped(tmp_path / "dict", where, call, TwoAnswersDict)
    assert as_dict == with_receipt(tmp_path / "plain", where, call, lambda: "T-1")
    as_mapping = wrapped(tmp_path / "mapping", where, call, TwoAnswers)
    assert as_mapping == with_receipt(tmp_path / "no-id", where, call, NO_ID["none"])
    for answer in (as_dict[0], as_mapping[0]):
        assert answer != (ValueError, PLAIN_TEXT)


def test_create_stores_nothing_for_a_mapping_with_two_answers(tmp_path: Path) -> None:
    """Red at ``8419c9ed`` (the head and member rows of ``new-1`` stayed stored after the
    guard's text). ``create`` refuses the receipt as one it cannot read, before its
    transaction, and neither table changes."""
    store, receipts, _ = group(tmp_path)
    place(receipts, lambda: "T-1", f"g:new-1:{SLOT7}", "hc:default:t7", "running")
    store.receipts = Wrapping(receipts, TwoAnswers)  # type: ignore[assignment]
    before = tables(store.path)
    with pytest.raises(ValueError, match=r"^The receipt under request key g:new-1:\w+ cannot be"):
        new_group(store)
    assert tables(store.path) == before
    assert store.describe("new-1") is None


class RaisingMapping(Mapping):  # type: ignore[type-arg]
    """A receipt that raises ``error`` in ``keys()``, in ``__iter__`` or in ``[]``."""

    def __init__(self, data: dict[str, Any], where: str, error: type[Exception]) -> None:
        self.data, self.where, self.error = data, where, error

    def keys(self) -> Any:
        if self.where == "keys":
            raise self.error("raised by the receipt")
        return super().keys()

    def __iter__(self) -> Any:
        if self.where == "iter":
            raise self.error("raised by the receipt")
        return iter(self.data)

    def __getitem__(self, key: Any) -> Any:
        if self.where == "getitem":
            raise self.error("raised by the receipt")
        return self.data[key]

    def __len__(self) -> int:
        return len(self.data)


def _raise(error: type[Exception]) -> Any:
    def wrap(item: Any) -> Any:
        raise error("raised by the lookup")

    return wrap


RAISING_CASES = [
    (where, call, part) for where, call in PLACE_CALLS for part in ("keys", "iter", "getitem")
]


@pytest.mark.parametrize(
    ("where", "call", "part"), RAISING_CASES, ids=[" | ".join(c) for c in RAISING_CASES]
)
def test_a_receipt_that_raises_while_it_is_copied(
    tmp_path: Path, where: str, call: str, part: str
) -> None:
    """Red at ``8419c9ed`` for the calls that read the receipt (there ``keys()`` and
    ``__iter__`` were never called, and an ``OSError`` out of ``[]`` left the method as an
    ``OSError``: ``F4-S-B8-before.json``, ``d_raising``); pins_existing for ``locate`` and
    ``describe``.

    An exception out of the copy is treated as the same exception out of the lookup: for a
    class of ``READ_ERRORS`` (here ``OSError``) the answer and both tables are what a lookup
    that raises it gets, and only the reason differs, which says that the copy failed; a
    class outside (here ``RuntimeError``) leaves the method as it does out of a lookup."""
    for error in (OSError, RuntimeError):
        name = error.__name__
        copied = wrapped(
            tmp_path / f"copy-{name}",
            where,
            call,
            lambda item, error=error: RaisingMapping(item, part, error),
        )
        lookup = wrapped(tmp_path / f"lookup-{name}", where, call, _raise(error))
        text = repr(lookup)
        for reason in ("lookup by key failed", "lookup by task ID failed", "raised by the lookup"):
            text = text.replace(reason, "X")
        expected = repr(copied).replace(COPY_REASON, "X").replace("raised by the receipt", "X")
        assert expected == text
        if lookup[0] == (error, "raised by the lookup"):
            assert copied[0] == (error, "raised by the receipt")


def test_no_line_outside_classify_reads_the_id_of_a_receipt() -> None:
    """Red at ``8419c9ed`` (``["id"]`` of the lookup's mapping was read outside ``_classify``
    there).

    In the module's source, no subscript with the constant ``"id"`` and no call of ``.get``
    with the constant ``"id"`` stands outside ``_classify``; inside it there is exactly one,
    on the copy. What this test cannot see: a key that is not written as a constant at the
    place (``name = "id"`` and then ``receipt[name]``), a read through another name
    (``read = getattr(receipt, "get")`` and then ``read("id")``, ``operator.itemgetter``),
    and a read of every item (``receipt.items()``, ``dict(receipt)``)."""
    tree = ast.parse(inspect.getsource(assignments))

    def reads(node: ast.AST) -> list[str]:
        found = []
        for item in ast.walk(node):
            if (
                isinstance(item, ast.Subscript)
                and isinstance(item.slice, ast.Constant)
                and item.slice.value == "id"
            ) or (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "get"
                and item.args
                and isinstance(item.args[0], ast.Constant)
                and item.args[0].value == "id"
            ):
                found.append(ast.unparse(item))
        return found

    classify = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_classify"
    ]
    assert len(classify) == 1
    inside = reads(classify[0])
    assert inside == ['copy.get("id")'.replace('"', "'")]
    assert sorted(reads(tree)) == sorted(inside)


class SplitId(dict):  # type: ignore[type-arg]
    """A ``dict`` whose ``[]`` answers ``other`` for ``id``, whatever its storage holds."""

    other = "T-9"

    def __getitem__(self, key: Any) -> Any:
        return self.other if key == "id" else super().__getitem__(key)


class SplitBack(SplitId):
    other = "T-1"


@pytest.mark.parametrize(
    "change",
    [
        {"target": "hc:default:t3"},
        {"target": "hc:default:t9"},
        {"request_key": "g:other:key"},
        {"message": "other work"},
        {"metadata": {"kind": "other"}},
    ],
    ids=["valid", "another recipient", "another request key", "other work", "another provider"],
)
def test_a_bound_attempt_is_compared_with_the_copied_id(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    """Red at ``8419c9ed`` (``_by_task`` compared ``["id"]`` of the lookup's mapping there).
    Added after the mutants B8-3b and B8-4 fell to the source test alone.

    An attempt is bound to ``T-1``; the lookup by task ID then hands out a ``dict`` whose
    storage and whose ``[]`` disagree about ``id``. What the store compares with the ID it
    asked for is the copy's ``id``, for a receipt that is valid and for one that is
    conflicting: storage ``T-1`` is the plain receipt ``T-1``, and storage ``T-9``
    is the plain receipt ``T-9`` (another task ID), whatever ``[]`` says."""
    answers = {}
    for name, stored, wrap in (
        ("split", "T-1", SplitId),
        ("plain", "T-1", dict),
        ("split back", "T-9", SplitBack),
        ("plain other", "T-9", dict),
    ):
        store, receipts, names = group(tmp_path / name)
        place(receipts, lambda: "T-1", names["key3"], "hc:default:t3", "running")
        store.record_submitted(names["key3"], "T-1")
        receipts.rows["T-1"].update(change, id=stored)
        store.receipts = Wrapping(receipts, wrap)  # type: ignore[assignment]
        answers[name] = (
            result(lambda s, n: s.get("grp"), store, names),
            result(lambda s, n: s.record_raised(n["key3"], "RuntimeError"), store, names),
        )
    assert answers["split"] == answers["plain"]
    assert answers["split back"] == answers["plain other"]
    assert answers["plain"] != answers["plain other"]


# --------------------------------------------------------------------------- round 4, B9
#
# One rule for a task ID (``assignments._is_task_id``): exactly ``str``, 1 to 128 characters,
# and UTF-8 can encode it. ``record_submitted`` and ``locate`` use it at entry, ``_classify``
# for a receipt's ``id``. The measurement is ``F4-S-B9-before.json``.

OUTSIDE_REASON = "receipt task ID is outside the rule for a task ID"
INSIDE = [1, 36, 37, 127, 128]
OUTSIDE = [129, 4096]


def stored_ids(path: Path) -> list[Any]:
    """Every ``task_id`` and ``parent_task_id`` that is not NULL in the member table."""
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT task_id, parent_task_id FROM assignment_members").fetchall()
    return [value for row in rows for value in row if value is not None]


def assert_named(path: Path) -> None:
    """Every stored task ID passes the one function, so a public call can name it."""
    for value in stored_ids(path):
        assert assignments._is_task_id(value), len(value)


def failed_receipt(folder: Path, task: str) -> tuple[AssignmentStore, Receipts, dict[str, Any]]:
    """The group ``grp`` and a valid, failed receipt with the ID ``task`` under ``key3``."""
    store, receipts, names = group(folder)
    receipts.add(task, names["key3"], "failed")
    receipts.rows[task].update(target="hc:default:t3")
    return store, receipts, names


def attempt3(store: AssignmentStore, names: dict[str, Any]) -> dict[str, Any]:
    return next(
        attempt
        for slot in store.get("grp")["slots"]
        for attempt in slot["attempts"]
        if attempt["member_key"] == names["key3"]
    )


@pytest.mark.parametrize("length", INSIDE)
@pytest.mark.parametrize("first", ["record_submitted", "get"])
def test_a_stored_task_id_inside_the_rule_can_be_named(
    tmp_path: Path, length: int, first: str
) -> None:
    """Red at ``6532cc7`` in its last line only (the one function does not exist there);
    every line before it is pins_existing there and at ``8419c9ed``
    (``F4-S-B9-before.json``). It is the test that the review's mutant O7 survived without.

    A valid receipt whose plain ID has 1, 36, 37, 127 or 128 characters. ``record_submitted``
    binds the ID and returns the attempt with it, or ``get`` alone adopts it from under the
    member key; ``get`` shows it; ``record_submitted`` with the same ID is then a no-op that
    returns the stored binding; ``locate`` finds the attempt by it; a successor that follows
    the settled attempt stores it as ``parent_task_id``; and every stored ID passes the rule.
    """
    task = "T" * length
    store, _, names = failed_receipt(tmp_path, task)
    if first == "record_submitted":
        view = store.record_submitted(names["key3"], task)
        assert (view["task_id"], view["admission"]) == (task, "submitted")
    shown = attempt3(store, names)
    assert (shown["task_id"], shown["lookup"]) == (task, "valid")
    before = tables(store.path)
    again = store.record_submitted(names["key3"], task)
    assert (again["task_id"], again["member_key"]) == (task, names["key3"])
    assert tables(store.path) == before
    found = store.locate(task_id=task)
    assert found is not None and found["member_key"] == names["key3"]
    successor, created = store.plan_attempt(
        "grp", names["slot3"], 2, peer(4), reason="probe", verdict="never_started"
    )
    assert created and successor["parent_task_id"] == task
    assert sorted(stored_ids(store.path)) == [task, task]
    assert_named(store.path)


@pytest.mark.parametrize("length", OUTSIDE)
@pytest.mark.parametrize(("where", "call"), PLACE_CALLS, ids=[" | ".join(c) for c in PLACE_CALLS])
def test_a_receipt_id_outside_the_rule_is_a_receipt_without_an_id(
    tmp_path: Path, where: str, call: str, length: int
) -> None:
    """Red at ``6532cc7`` for the calls that read the receipt (a plain ID of 129 or 4096
    characters was ``valid`` there and since the start commit: this is the change of
    behaviour that ruling B9 makes); pins_existing for ``locate`` and ``describe``.

    In every place of round 3's table the call answers, both tables hold and the lookups
    made are what they are for a receipt whose ``id`` is ``None``; only the reason differs,
    which is fixed and holds nothing of the ID. Nothing is stored that cannot be named."""
    task = "T" * length
    long = with_receipt(tmp_path / "long", where, call, lambda: task)
    none = with_receipt(tmp_path / "none", where, call, NO_ID["none"])
    assert repr(long).replace(OUTSIDE_REASON, NO_ID_REASON) == repr(none)
    assert "TTTT" not in repr(long)
    assert_named(tmp_path / "long" / "assignments.sqlite")


@pytest.mark.parametrize("length", OUTSIDE)
def test_a_task_id_outside_the_rule_is_refused_at_entry_and_never_stored(
    tmp_path: Path, length: int
) -> None:
    """Red at ``6532cc7`` for the steps after the entry refusals (``get`` bound the ID and a
    successor stored it as ``parent_task_id`` there); the entry refusals are pins_existing.

    ``record_submitted(key, id)`` and ``locate(task_id=id)`` refuse with the fixed text
    before any lookup and before the file is opened (a file removed after the store was
    made is not made again). With the
    receipt under the member key, ``get`` does not bind the ID, a successor is refused, and
    no task ID is stored."""
    task = "T" * length
    receipts = Receipts()
    missing = AssignmentStore(tmp_path / "missing" / "assignments.sqlite", receipts)
    for item in missing.path.parent.iterdir():
        item.unlink()
    for call in (
        lambda s: s.record_submitted("g:grp:key", task),
        lambda s: s.locate(task_id=task),
    ):
        with pytest.raises(ValueError) as refusal:
            call(missing)
        assert str(refusal.value) == TASK_TEXT
    assert receipts.calls == [] and list(missing.path.parent.iterdir()) == []

    store, receipts, names = failed_receipt(tmp_path / "there", task)
    receipts.calls.clear()
    before = tables(store.path)
    for call in (
        lambda s: s.record_submitted(names["key3"], task),
        lambda s: s.locate(task_id=task),
    ):
        with pytest.raises(ValueError) as refusal:
            call(store)
        assert str(refusal.value) == TASK_TEXT
    assert receipts.calls == [] and tables(store.path) == before
    shown = attempt3(store, names)
    assert (shown["task_id"], shown["lookup"], shown["lookup_detail"]) == (
        None,
        "unavailable",
        OUTSIDE_REASON,
    )
    with pytest.raises(ValueError, match="^The previous attempt's receipt is unavailable"):
        store.plan_attempt(
            "grp", names["slot3"], 2, peer(4), reason="probe", verdict="never_started"
        )
    assert stored_ids(store.path) == []


def test_the_one_rule_for_a_task_id() -> None:
    """Red at ``6532cc7`` (there is no such function there). The function itself: exactly
    ``str``, 1 to 128 characters; the bound is pinned from both sides."""
    rule = assignments._is_task_id
    assert [rule("T" * n) for n in (0, 1, 36, 37, 64, 65, 127, 128, 129, 4096)] == [
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert rule("é" * 128) and rule("\x00") and rule("a\n")
    for value in (None, 5, b"T", Same("T"), conform("T-1", "T-2"), ["T"]):
        assert not rule(value)


ALL_IDS: dict[str, Any] = {
    **RECEIPT_IDS,
    **NO_ID,
    "plain": lambda: "T-1",
    **{f"length {n}": (lambda n=n: "T" * n) for n in INSIDE + OUTSIDE},
}


@pytest.mark.parametrize("make_id", list(ALL_IDS))
def test_nothing_is_stored_that_cannot_be_named(tmp_path: Path, make_id: str) -> None:
    """Red at ``6532cc7``: for the lengths 129 and 4096 because ``get``, ``record_raised``, a
    repeat of ``plan_attempt`` and of ``create`` stored the ID there, and for every other
    case that stores an ID because the function does not exist there.

    After each place/call sequence of ``PLACE_CALLS``, for every kind of receipt ``id`` in
    ``ALL_IDS`` (those that are not exactly ``str``, absent, ``None``, a plain one, and the
    lengths of the tests above), every ``task_id`` and
    ``parent_task_id`` in the member table passes the one function."""
    for number, (where, call) in enumerate(PLACE_CALLS):
        with_receipt(tmp_path / str(number), where, call, ALL_IDS[make_id])
        assert_named(tmp_path / str(number) / "assignments.sqlite")


# --------------------------------------------------------------------------- round 4, B10
#
# No text that UTF-8 cannot encode: one function (``assignments._encodable``) says whether a
# text can be encoded, and no public call of the store raises ``UnicodeEncodeError``. The
# measurement is ``F4-S-B10-before.json``.

UTF8_TEXT = "The assignment store binds no text that UTF-8 cannot encode"
MESSAGE_UTF8_TEXT = "Message must be text that UTF-8 can encode"
TARGET_UTF8_TEXT = "Recipient target must be text that UTF-8 can encode"
KEYS_UTF8_TEXT = "Assessment member keys must be text that UTF-8 can encode"
FACTS_UTF8_TEXT = "Assessment fact names must be text that UTF-8 can encode"
PATH_TEXT = "The assignment store path cannot be encoded as a file name"
MESSAGE_REASON = "receipt message identity cannot be read"


def lone(base: str) -> dict[str, str]:
    """``base`` with a lone surrogate first, in the middle and last, and with U+DFFF."""
    half = len(base) // 2
    return {
        "first": "\ud800" + base,
        "middle": base[:half] + "\ud800" + base[half:],
        "last": base + "\ud800",
        "U+DFFF": base + "\udfff",
    }


def _recipient(number: int, **change: Any) -> Recipient:
    return peer(number)._replace(**change)


# argument: (the text without a surrogate, or the name of a key of the group; the call;
#            the text of its refusal, ``None`` for a refusal text of before this round that is
#            only compared over the four forms, ``"returns"`` for a call that answers)
SURROGATES: dict[str, tuple[str, Any, str | None]] = {
    "__init__.path": (
        "x",
        lambda s, n, v: AssignmentStore(s.path.parent / "other" / v, Receipts()),
        PATH_TEXT,
    ),
    "create.assignment_id": (
        "new-1",
        lambda s, n, v: s.create(v, kind="single", recipients=[peer(7)], message=MESSAGE),
        None,
    ),
    "create.kind": ("fanout", lambda s, n, v: new_group(s, kind=v), None),
    "create.message": (MESSAGE, lambda s, n, v: new_group(s, message=v), MESSAGE_UTF8_TEXT),
    "create.route": ("direct", lambda s, n, v: new_group(s, route=v), None),
    "create.failover": ("off", lambda s, n, v: new_group(s, failover=v), None),
    "create.independent_of": ("hc:t5", lambda s, n, v: new_group(s, independent_of=v), None),
    "create Recipient.peer_id": (
        "hc:t7",
        lambda s, n, v: new_group(s, first=_recipient(7, peer_id=v)),
        None,
    ),
    "create Recipient.target": (
        "hc:default:t7",
        lambda s, n, v: new_group(s, first=_recipient(7, target=v)),
        TARGET_UTF8_TEXT,
    ),
    "create Recipient.provider": (
        "hc",
        lambda s, n, v: new_group(s, first=_recipient(7, provider=v)),
        None,
    ),
    "plan_attempt.assignment_id": (
        "grp",
        lambda s, n, v: s.plan_attempt(v, n["slot2"], 2, peer(4), reason="probe"),
        None,
    ),
    "plan_attempt.slot": ("slot2", lambda s, n, v: second(s, n, slot=v), None),
    "plan_attempt.message": (MESSAGE, lambda s, n, v: second(s, n, message=v), MESSAGE_UTF8_TEXT),
    "plan_attempt.reason": ("probe", lambda s, n, v: second(s, n, reason=v), UTF8_TEXT),
    "plan_attempt.verdict": ("never_started", lambda s, n, v: second(s, n, verdict=v), None),
    "plan_attempt.evidence key": ("k", lambda s, n, v: second(s, n, evidence={v: "v"}), UTF8_TEXT),
    "plan_attempt.evidence value": (
        "v",
        lambda s, n, v: second(s, n, evidence={"k": v}),
        UTF8_TEXT,
    ),
    "plan_attempt Recipient.peer_id": (
        "hc:t4",
        lambda s, n, v: second(s, n, recipient=_recipient(4, peer_id=v)),
        None,
    ),
    "plan_attempt Recipient.target": (
        "hc:default:t4",
        lambda s, n, v: second(s, n, recipient=_recipient(4, target=v)),
        TARGET_UTF8_TEXT,
    ),
    "plan_attempt Recipient.provider": (
        "hc",
        lambda s, n, v: second(s, n, recipient=_recipient(4, provider=v)),
        None,
    ),
    "plan_attempt repeat Recipient.target": (
        "hc:default:t3",
        lambda s, n, v: s.plan_attempt("grp", n["slot3"], 1, _recipient(3, target=v)),
        TARGET_UTF8_TEXT,
    ),
    "record_submitted.member_key": (
        "key3",
        lambda s, n, v: s.record_submitted(v, "T-1"),
        MEMBER_TEXT,
    ),
    "record_submitted.task_id": (
        "T-1",
        lambda s, n, v: s.record_submitted(n["key3"], v),
        TASK_TEXT,
    ),
    "record_raised.member_key": (
        "key3",
        lambda s, n, v: s.record_raised(v, "RuntimeError"),
        MEMBER_TEXT,
    ),
    "record_raised.detail": (
        "RuntimeError",
        lambda s, n, v: s.record_raised(n["key3"], v),
        UTF8_TEXT,
    ),
    "get.assignment_id": ("grp", lambda s, n, v: s.get(v), None),
    "get.assessment key": (
        "key3",
        lambda s, n, v: s.get("grp", assessment={v: {"task_id": None}}),
        KEYS_UTF8_TEXT,
    ),
    "get.assessment task_id": (
        "T",
        lambda s, n, v: s.get("grp", assessment={n["key3"]: {"task_id": v}}),
        "returns",
    ),
    "get.assessment fact name": (
        "stalled",
        lambda s, n, v: s.get("grp", assessment={n["key3"]: {"task_id": None, v: True}}),
        FACTS_UTF8_TEXT,
    ),
    "get.assessment verdict": (
        "never_started",
        lambda s, n, v: s.get("grp", assessment={n["key3"]: {"task_id": None, "verdict": v}}),
        None,
    ),
    "locate.task_id": ("T-1", lambda s, n, v: s.locate(task_id=v), TASK_TEXT),
    "locate.member_key": ("key3", lambda s, n, v: s.locate(member_key=v), MEMBER_TEXT),
    "describe.assignment_id": ("grp", lambda s, n, v: s.describe(v), None),
    "clock": (
        "1000",
        lambda s, n, v: (setattr(s, "clock", lambda: v), s.record_raised(n["key3"], "X"))[1],
        UTF8_TEXT,
    ),
}
ENTRY_CHECKED = [
    "record_submitted.member_key",
    "record_submitted.task_id",
    "record_raised.member_key",
    "locate.task_id",
    "locate.member_key",
]


def test_the_surrogate_table_covers_the_arguments_that_hold_a_text() -> None:
    """Red at ``43eef9f`` with the rest of this part (pins the table, not the store): every
    row of ``ARGUMENTS`` is in ``SURROGATES`` (a recipient's fields once for ``create`` and
    once for ``plan_attempt``, an assessment by its keys and its texts, ``evidence``
    by key and value) except the arguments that are no text and hold none."""
    no_text = {
        "__init__.receipts",
        "__init__.clock",
        "create.recipients",
        "create.max_attempts",
        "create.quorum",
        "create.deadline",
        "plan_attempt.attempt",
        "plan_attempt.recipient",
        "plan_attempt.evidence",
        "get.assessment",
        "Recipient.peer_id",
        "Recipient.target",
        "Recipient.provider",
        "Recipient.local",
    }
    assert no_text <= set(ARGUMENTS)
    assert set(ARGUMENTS) - no_text <= set(SURROGATES)


@pytest.mark.parametrize("argument", list(SURROGATES))
def test_no_call_raises_an_encoding_error(tmp_path: Path, argument: str) -> None:
    """Red at ``43eef9f`` for a part of the arguments (``F4-S-r4-B10-red.log``);
    pins_existing for the others.

    For a lone surrogate first, in the middle and last, and for U+DFFF last: the call
    answers, or it raises exactly ``ValueError`` (not ``UnicodeEncodeError``, which is a
    subclass) with one text for the four forms that holds nothing of the value and carries
    no other exception (``__cause__`` and ``__context__`` are ``None``); after a refusal
    every file in the store's folder is byte for byte what it was, and both tables too. For
    the five entry-checked parameters no lookup was called."""
    base, call, expected = SURROGATES[argument]
    texts = set()
    for form in ("first", "middle", "last", "U+DFFF"):
        store, receipts, names = group(tmp_path / form.replace("+", ""))
        value = lone(names.get(base, base))[form]
        receipts.calls.clear()
        before = files(store.path.parent), tables(store.path)
        try:
            call(store, names, value)
        except Exception as error:  # noqa: BLE001 - the class is what is asserted
            assert type(error) is ValueError, form
            assert error.__cause__ is None and error.__context__ is None, form
            assert not any(0xD800 <= ord(char) <= 0xDFFF for char in str(error)), form
            assert "\\ud" not in str(error), form
            texts.add(str(error))
            assert (files(store.path.parent), tables(store.path)) == before, form
        else:
            texts.add("returns")
        if argument in ENTRY_CHECKED:
            assert receipts.calls == [], form
    assert len(texts) == 1
    if expected is not None:
        assert texts == {expected}


@pytest.mark.parametrize("argument", ENTRY_CHECKED)
@pytest.mark.parametrize("form", ["first", "middle", "last", "U+DFFF"])
def test_an_entry_refusal_of_a_surrogate_comes_before_the_file(
    tmp_path: Path, argument: str, form: str
) -> None:
    """Red at ``43eef9f`` (there the file was opened and ``UnicodeEncodeError`` came out of
    the statement, or, for ``record_submitted``'s task ID, the ID went to the lookup).
    With the store's file removed after the store was made, the refusal is the same and the
    folder is still empty; no lookup was called."""
    call = SURROGATES[argument][1]
    path, _ = stocked(tmp_path)
    lookup = RefusingLookup()
    store = AssignmentStore(path, lookup)
    for item in tmp_path.iterdir():
        item.unlink()
    value = lone("a")[form]
    names = {"key3": "a"}
    refused(
        lambda s, v: call(s, names, v),
        store,
        value,
        MEMBER_TEXT if "member_key" in argument else TASK_TEXT,
    )
    assert list(tmp_path.iterdir()) == []
    assert lookup.calls == []


RECEIPT_TEXTS = {
    "request_key": lambda item, v: item.update(request_key=v),
    "target": lambda item, v: item.update(target=v),
    "status": lambda item, v: item.update(status=v),
    "metadata.kind": lambda item, v: item["metadata"].update(kind=v),
    "id": lambda item, v: item.update(id=v),
    "message": lambda item, v: item.update(message=v),
}


def with_field(folder: Path, where: str, call: str, change: Any) -> tuple[Any, Any, Any]:
    """As ``with_receipt`` for the plain receipt ``T-1`` after ``change(receipt)``."""
    store, receipts, names = group(folder)
    receipts.calls.clear()
    place(receipts, lambda: "T-1", *PLACES[where][0](names))
    change(receipts.rows["T-1"])
    return (*result(PLACES[where][1][call], store, names), receipts.calls)


@pytest.mark.parametrize(("where", "call"), PLACE_CALLS, ids=[" | ".join(c) for c in PLACE_CALLS])
def test_a_receipt_with_a_text_that_cannot_be_encoded(
    tmp_path: Path, where: str, call: str
) -> None:
    """Red at ``43eef9f`` in a part of the pairs, for the ``id`` or the ``message``
    (``UnicodeEncodeError`` at ``8419c9ed``; at ``43eef9f`` the message gave the reason of
    a failed copy); pins_existing for the fields that are compared and not encoded.

    In the pairs of ``PLACE_CALLS``, for the four forms: a receipt whose ``id`` cannot be
    encoded is what a receipt whose ``id`` is ``None`` is, apart from the reason, which is
    the one of ruling B9; a receipt whose ``message`` cannot be encoded is what a receipt
    whose ``message`` is ``None`` is, with the same reason; and with such a text in
    ``request_key``, ``target``, ``status`` or the provider the call answers or raises
    exactly ``ValueError``. A view shows the receipt's own ``status`` as it is; a refusal
    holds no surrogate (for ``status`` that is red at step 01 of round 5, in the pair
    ``stored planned, receipt failed | record_submitted then successor``)."""
    none = with_receipt(tmp_path / "no-id", where, call, NO_ID["none"])
    blank = with_field(tmp_path / "no-message", where, call, lambda item: item.update(message=None))
    for form in ("first", "middle", "last", "U+DFFF"):
        for field, change in RECEIPT_TEXTS.items():
            base = {"id": "T-1", "message": MESSAGE, "status": "running"}.get(field, "hc")
            value = lone(base)[form]
            got = with_field(
                tmp_path / f"{field}-{form.replace('+', '')}",
                where,
                call,
                lambda item, change=change, value=value: change(item, value),
            )
            answer = got[0]
            raised = isinstance(answer, tuple) and isinstance(answer[0], type)
            if raised:
                assert answer[0] is ValueError, (field, form)
            if field != "status" or raised:
                assert "\\ud" not in repr(got[:2]), (field, form)
            if field == "id":
                assert repr(got).replace(OUTSIDE_REASON, NO_ID_REASON) == repr(none), form
            if field == "message":
                assert got == blank, form
    if "locate" not in call and call != "describe" and where != "new attempt":
        assert MESSAGE_REASON in repr(blank) or blank[0][0] is ValueError


EDGES = {"U+D7FF": "\ud7ff", "U+E000": "\ue000", "outside the BMP": "\U0001f600"}


@pytest.mark.parametrize("edge", list(EDGES))
def test_the_characters_beside_the_surrogates_are_accepted(tmp_path: Path, edge: str) -> None:
    """pins_existing at ``43eef9f`` and ``8419c9ed`` but for the function in the first line
    (red: it does not exist there). U+D7FF, U+E000 and a character outside the BMP are
    accepted wherever the text is otherwise accepted: as a message, a target, a reason, an
    evidence key and value, a detail, a member key and a task ID that are not stored, a
    receipt's ``id`` and a receipt's message. U+D800 and U+DFFF are not (the tests above).
    A path is not in the list: which names a folder takes is the file system's answer, and
    one may refuse U+D7FF in a name with ``OSError``."""
    char = EDGES[edge]
    assert assignments._encodable(char) and assignments._is_task_id(char)
    receipts = Receipts()
    store = AssignmentStore(tmp_path / "store.sqlite", receipts)
    target, message = f"hc:default:t1{char}", f"{char}work{char}"
    one = Recipient("hc:t1", target, "hc", True)
    view, _ = store.create(
        "a", kind="single", recipients=[one], message=message, failover="never_started"
    )
    key = view["slots"][0]["attempts"][0]["member_key"]
    assert view["slots"][0]["attempts"][0]["target"] == target
    with pytest.raises(ValueError, match=r"^Unknown member key$"):
        store.record_submitted(char, "5")
    with pytest.raises(ValueError, match=r"^Unknown member key$"):
        store.record_raised(char, "RuntimeError")
    assert store.locate(member_key=char) is None and store.locate(task_id=char) is None
    task = f"T{char}"
    receipts.add(task, key, "failed")
    receipts.rows[task].update(target=target, message=message)
    bound = store.record_submitted(key, task)
    assert (bound["task_id"], bound["lookup"]) == (task, "valid")
    found = store.locate(task_id=task)
    assert found is not None and found["member_key"] == key
    successor, created = store.plan_attempt(
        "a",
        view["slots"][0]["slot"],
        2,
        Recipient("hc:t2", f"hc:default:t2{char}", "hc", True),
        reason=f"probe{char}",
        verdict="never_started",
        evidence={f"k{char}": f"v{char}"},
    )
    assert created
    assert (successor["reason"], successor["parent_task_id"]) == (f"probe{char}", task)
    assert successor["evidence"] == {f"k{char}": f"v{char}"}
    raised = store.record_raised(successor["member_key"], f"detail{char}")
    assert raised["detail"].endswith(f"detail{char}")
    assert_named(store.path)


def test_the_one_function_for_text_that_utf8_can_encode() -> None:
    """Red at ``43eef9f`` (no such function there). The function is asked about every code
    point of the BMP and some outside it, and agrees with ``str.encode``: only U+D800 to
    U+DFFF cannot be encoded, wherever they stand in a text. The rule for a task ID asks
    it."""
    encodable = assignments._encodable
    for code in [*range(0x10000), 0x10000, 0x1F600, 0x10FFFF]:
        char = chr(code)
        try:
            char.encode("utf-8")
        except UnicodeEncodeError:
            wanted = False
        else:
            wanted = True
        assert encodable(char) is wanted, hex(code)
        assert wanted is not (0xD800 <= code <= 0xDFFF)
    for text in ("\ud800a", "a\udbffb", "ab\udc00", "a\udfff"):
        assert not encodable(text) and not assignments._is_task_id(text)
    for text in ("", "a\x00b", "a\n", "\u00e9\u0665", "\ud7ff\ue000"):
        assert encodable(text)


def test_a_refusal_carries_no_encoding_exception(tmp_path: Path) -> None:
    """Red at ``43eef9f`` (``UnicodeEncodeError`` itself left each of these calls).
    Ruling B10, point 5, for an entry refusal, a guard refusal, the message refusal and
    the refusal of a path, each in an ordinary call (not made while another exception is
    handled): the error is exactly ``ValueError``, its only argument is the fixed text,
    and it has no ``__cause__`` and no ``__context__``. A refusal raised inside the
    handler of the encoding error would have that error as its ``__context__``."""
    store, _, names = group(tmp_path)
    for call, text in (
        (lambda: store.record_raised("\ud800", "RuntimeError"), MEMBER_TEXT),
        (lambda: store.record_raised(names["key3"], "detail\ud800"), UTF8_TEXT),
        (lambda: new_group(store, message="work\ud800"), MESSAGE_UTF8_TEXT),
        (lambda: AssignmentStore(tmp_path / "x\ud800", Receipts()), PATH_TEXT),
    ):
        with pytest.raises(ValueError) as caught:  # noqa: PT011 - compared below
            call()
        assert type(caught.value) is ValueError
        assert caught.value.args == (text,)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__suppress_context__ is False


# --------------------------------------------------------------------------- round 4, F5


def test_only_transaction_control_runs_outside_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (green at ``8419c9ed``; the measurement is ``F4-S-F5-trace-before.json``).
    A trace callback on each connection of the store lists what SQLite ran; beside it, what
    passed ``_PlainConnection.execute``. In the calls of this test (each public method, answering
    and refusing) the two lists are the same statements in the same order, but for the
    ``COMMIT`` or ``ROLLBACK`` that ``sqlite3`` issues when a ``with`` block ends. Those
    carry no parameter. The callback names an ``UPDATE`` twice in a row, with one text,
    for one call of ``execute``; such a repeat of the text before it is the only other
    thing the lists differ by. The test sees these calls and no others."""
    passed: list[str] = []
    ran: list[str] = []
    real = assignments._PlainConnection
    real_execute, real_init = real.execute, real.__init__

    def execute(self: Any, sql: str, parameters: Any = (), /) -> Any:
        passed.append(sql.split()[0])
        return real_execute(self, sql, parameters)

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        self.set_trace_callback(ran.append)

    monkeypatch.setattr(real, "execute", execute)
    monkeypatch.setattr(real, "__init__", init)
    store, receipts, names = group(tmp_path)
    assert ran.count("COMMIT") >= 1 and "INSERT" in passed
    receipts.add("T-4", names["key2"] + ".a2")
    receipts.rows["T-4"]["target"] = "hc:default:t4"
    calls: dict[str, Any] = {
        "create new": lambda: new_group(store),
        "create repeat": lambda: new_group(store),
        "create refused": lambda: new_group(store, message="other work"),
        "get, nothing to bind": lambda: store.get("grp"),
        "record_raised": lambda: store.record_raised(names["key2"], "RuntimeError"),
        "plan_attempt new": lambda: second(store, names),
        "plan_attempt repeat": lambda: store.plan_attempt("grp", names["slot3"], 1, peer(3)),
        "plan_attempt refused": lambda: store.plan_attempt("grp", names["slot3"], 2, peer(4)),
        "get that binds": lambda: (
            place(receipts, lambda: "T-1", names["key3"], "hc:default:t3", "running"),
            store.get("grp"),
        ),
        "record_submitted": lambda: store.record_submitted(names["key2"] + ".a2", "T-4"),
        "record_submitted refused": lambda: store.record_submitted(names["key2"], "T-9"),
        "locate": lambda: store.locate(task_id="T-1"),
        "describe": lambda: store.describe("grp"),
        "describe unknown": lambda: store.describe("zz"),
    }
    outside: set[str] = set()
    again: set[str] = set()
    for name, call in calls.items():
        passed.clear()
        ran.clear()
        try:
            call()
        except ValueError:
            assert "refused" in name or name == "describe unknown", name
        else:
            assert "refused" not in name, name
        assert passed, name
        assert "COMMIT" not in passed and "ROLLBACK" not in passed, name
        matched, before = 0, ""
        for text in ran:
            word = text.split()[0]
            if matched < len(passed) and word == passed[matched]:
                matched += 1
            elif word in ("COMMIT", "ROLLBACK"):
                outside.add(word)
            else:
                assert text == before, (name, text)
                again.add(word)
            before = text
        assert matched == len(passed), name
    assert outside == {"COMMIT", "ROLLBACK"}
    assert again == {"UPDATE"}


# --------------------------------------------------------------------------- round 5, B11
#
# No refusal text holds a value of a receipt. The measurement is ``F4-S-B11-before.json``.
# In round 5 no commit was made by the writer; ``step 01`` is the commit that adds these
# tests, the step before the fix.

STATUS_TEXTS = {
    "previous": "The previous attempt is {}; no successor",
    "earlier, new successor": "An earlier attempt is unresolved (attempt 1 {}); no successor",
    "earlier, repeat": (
        "An earlier attempt is unresolved (attempt 1 {}); the attempt cannot be planned again"
    ),
}
OTHER_STATUSES: dict[str, Any] = {
    **{"surrogate " + form: value for form, value in lone("running").items()},
    "100000 characters": "x" * 100_000,
    "5": 5,
    "None": None,
    "RUNNING": "RUNNING",
    "str subclass": Same("running"),
}


def status_refusal(folder: Path, text: str, status: Any, flags: tuple[str, ...] = ()) -> Any:
    """What a ``plan_attempt`` in the slot of ``hc:t3`` raises when the receipt of attempt 1,
    bound as ``T-1`` while it was ``failed``, has the status ``status`` and the metadata
    flags ``flags``: attempt 2 anew (``previous``), attempt 2 again after it was planned
    (``earlier, repeat``) or attempt 3 after it (``earlier, new successor``). The store's
    files and both tables are what they were before the refused call."""
    store, receipts, names = group(folder)
    place(receipts, lambda: "T-1", names["key3"], "hc:default:t3", "failed")
    store.record_submitted(names["key3"], "T-1")

    def plan(attempt: int, number: int) -> Any:
        return store.plan_attempt(
            "grp", names["slot3"], attempt, peer(number), reason="probe", verdict="never_started"
        )

    if text != "previous":
        plan(2, 4)
    receipts.rows["T-1"]["status"] = status
    receipts.rows["T-1"]["metadata"].update(dict.fromkeys(flags, True))
    before = files(store.path.parent), tables(store.path)
    with pytest.raises(ValueError) as caught:
        plan(3, 5) if text == "earlier, new successor" else plan(2, 4)
    assert (files(store.path.parent), tables(store.path)) == before
    return caught.value


@pytest.mark.parametrize("text", list(STATUS_TEXTS))
@pytest.mark.parametrize("label", list(OTHER_STATUSES))
def test_a_refusal_names_no_status_outside_the_set(tmp_path: Path, text: str, label: str) -> None:
    """Red at step 01 (round 5; the text held the status there, see ``a_other_values`` in
    ``F4-S-B11-before.json``). For the refusals that name a receipt's status and the
    values of ``OTHER_STATUSES``, the text has the words ``another status`` in the
    status's place, and the exception is exactly ``ValueError`` without cause or context."""
    error = status_refusal(tmp_path, text, OTHER_STATUSES[label])
    assert type(error) is ValueError
    assert str(error) == STATUS_TEXTS[text].format("another status")
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("text", list(STATUS_TEXTS))
def test_a_refusal_names_a_status_of_the_set(tmp_path: Path, text: str) -> None:
    """pins_existing (``c_known`` in ``F4-S-B11-before.json``). ``previous``: the
    statuses that are not settled without success, with ``identity_invalidated`` set (which
    lets ``completed`` reach the text). The ``earlier`` texts: the statuses of the set, with
    ``blocked_seen`` and ``identity_invalidated`` set. The text names the status."""
    if text == "previous":
        statuses = sorted(STATUSES - set(assignments.SETTLED_UNSUCCESSFUL))
        flags: tuple[str, ...] = ("identity_invalidated",)
    else:
        statuses, flags = sorted(STATUSES), ("blocked_seen", "identity_invalidated")
    assert len(statuses) == (7 if text == "previous" else 10)
    for status in statuses:
        error = status_refusal(tmp_path / status, text, status, flags)
        assert str(error) == STATUS_TEXTS[text].format(status)


def test_the_statuses_of_the_module_are_those_of_the_receipt_contract() -> None:
    """Red at step 01 (round 5; the module had no such set). The set the module states is
    ``answerback_bridge.adapters.contracts.STATUSES``."""
    assert assignments.RECEIPT_STATUSES == STATUSES
    assert type(assignments.RECEIPT_STATUSES) is frozenset


# --------------------------------------------------------------------------- round 5, R2
#
# The copy of ``metadata`` for a mapping that is no plain ``dict``. Review 2's mutants N4
# (only an exact ``dict`` is copied) and N5 (a mapping with a false truth value is not
# copied) passed the four assignment files of round 4.


class DictMetadata(dict[str, Any]):
    """A ``dict`` subclass."""


class FalseDictMetadata(DictMetadata):
    """A ``dict`` subclass whose truth value is false."""

    def __len__(self) -> int:
        return 0


class MappingMetadata(Mapping[str, Any]):
    """A ``Mapping`` that is no ``dict``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __iter__(self) -> Any:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


class FalseMappingMetadata(MappingMetadata):
    """A ``Mapping`` that is no ``dict`` and whose truth value is false."""

    def __len__(self) -> int:
        return 0


METADATA_KINDS = (DictMetadata, FalseDictMetadata, MappingMetadata, FalseMappingMetadata)


class HandingMetadata(Handing):
    """As ``Handing``; the ``metadata`` of a receipt it hands out is of the class ``kind``."""

    def __init__(self, inner: Receipts, kind: Any, change: Any, on_lookup: bool) -> None:
        super().__init__(inner, change, on_lookup)
        self.kind = kind

    def _hand(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is not None:
            item["metadata"] = self.kind(item["metadata"])
        return super()._hand(item)


def set_flag(flag: str) -> Any:
    """Sets ``flag`` in the ``metadata`` a ``HandingMetadata`` handed out."""

    def change(item: dict[str, Any]) -> None:
        metadata = item["metadata"]
        if isinstance(metadata, dict):
            metadata[flag] = True
        else:
            metadata.data[flag] = True

    return change


@pytest.mark.parametrize("kind", METADATA_KINDS, ids=[kind.__name__ for kind in METADATA_KINDS])
@pytest.mark.parametrize("flag", ["identity_invalidated", "blocked_seen"])
@pytest.mark.parametrize("by", ["lookup", "clock"])
def test_a_metadata_mapping_changed_after_it_was_returned(
    tmp_path: Path, kind: Any, flag: str, by: str
) -> None:
    """pins_existing. For the pairs of ``PLACE_CALLS`` and each status tried: the lookup
    hands out a receipt whose ``metadata`` is of the class ``kind`` and sets ``flag`` in it
    afterwards, at its next call or when the store asks the clock. The answer, both tables
    and the lookups made are what they are with the control of
    ``test_a_receipt_changed_after_it_was_returned_changes_nothing``: a lookup that hands
    out separate plain ``dict`` objects and changes none."""
    assert len(PLACE_CALLS) == 17
    for number, (where, call) in enumerate(PLACE_CALLS):
        for status in ("running", "failed", "completed"):
            store, receipts, names = group(tmp_path / f"changed-{number}-{status}")
            key, target, _ = PLACES[where][0](names)
            place(receipts, lambda: "T-1", key, target, status)
            receipts.calls.clear()
            handing = HandingMetadata(receipts, kind, set_flag(flag), by == "lookup")
            store.receipts = handing
            if by == "clock":
                store.clock = lambda handing=handing: (handing.fire(), 1000.0)[1]
            changed = (*result(PLACES[where][1][call], store, names), receipts.calls)
            control = handed(tmp_path / f"control-{number}-{status}", where, call, status, None, by)
            assert changed == control, (where, call, status)
