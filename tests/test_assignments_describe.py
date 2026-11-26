"""``AssignmentStore.describe`` (row F4-P, ruling R1 of the fan-out rulings).

Labels. Every test above the line "fix round 1" is ``red_at_pretip``: at ``ec5ed7fd`` the
store has no ``describe``, so each test fails there with ``AttributeError``
(``out/F4-P-describe-red.log``). The tests below that line carry their own label, and their
pre-tip is ``eac43df4``, where ``describe`` opened the store as ``get`` does
(``out/F4-P-fix1-red.log``).

Where the inputs come from. Assignment IDs, peers and recipients have the forms that
``create()`` accepts (``assignments.py``: ``_GROUP_ID``, ``_SINGLE_ID``, ``_check_recipient``);
targets follow ``CoreTasks.target_key`` (``hc:<session>:<terminal>``). Receipts have the form
that ``TaskStore.decode`` returns (``tasks.py`` ``reserve`` and ``decode_task``): ``id``,
``request_key``, ``target``, ``message``, ``status`` and a ``metadata`` mapping with ``kind``.
"""

import itertools
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge import assignments
from answerback_bridge.assignments import AssignmentStore, Recipient
from answerback_bridge.retention import content_identity

MESSAGE = "Review the change on branch akirem/f4p-impl"
OTHER_MESSAGE = "Review another change"
GROUP = "ab-20261121-F4P-group"
SINGLE = "ab-20261121-F4P-single"
KEYS = {
    "kind",
    "recipients",
    "quorum",
    "route",
    "failover",
    "max_attempts",
    "independent_of",
    "deadline",
}
OTHER_WORK = r"^assignment_id already describes different work$"
BAD_ID = r"^assignment_id must be 1–120 letters, digits, dots, colons, _ or -$"
PEERS = ["hc:t1", "hc:t2", "hc:t3", "hc:t4", "hc:t5", "hc:t6", "hc:t7", "hc:t8"]


def R(peer: str) -> Recipient:
    """A resolved recipient on this machine, with the adapter's own target form."""
    provider, _, rest = peer.partition(":")
    return Recipient(peer, f"{provider}:default:{rest}", provider, True)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Receipts:
    """Receipts in the form ``TaskStore.decode`` returns; every lookup is counted."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, request_key: str, status: str, *, target: str, message: str = MESSAGE) -> str:
        task_id = str(uuid.uuid4())
        self.rows[task_id] = {
            "id": task_id,
            "request_key": request_key,
            "target": target,
            "message": message,
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


class RefusingLookup:
    """A receipt lookup whose every method, and every other attribute, raises.

    ``AssertionError`` is none of the store's ``READ_ERRORS``, so the store cannot take it
    for an unreadable receipt: any use of the lookup fails the test.
    """

    def __getattribute__(self, name: str) -> Any:
        raise AssertionError(f"describe used the receipt lookup ({name})")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def receipts() -> Receipts:
    return Receipts()


@pytest.fixture
def store(tmp_path: Path, receipts: Receipts, clock: Clock) -> AssignmentStore:
    return AssignmentStore(tmp_path / "state" / "assignments.sqlite", receipts, clock=clock)


def described(store: AssignmentStore, assignment_id: str) -> dict[str, Any]:
    result = store.describe(assignment_id)
    assert result is not None
    return dict(result)


def dump(path: Path) -> list[tuple[Any, ...]]:
    """Every row of the store's two tables, read with a plain ``sqlite3`` connection."""
    with closing(sqlite3.connect(path)) as db:
        return [
            *map(tuple, db.execute("SELECT * FROM assignments ORDER BY assignment_id")),
            *map(tuple, db.execute("SELECT * FROM assignment_members ORDER BY member_key")),
        ]


# ---------------------------------------------------------------- the round trip (item 3)

_MEMBERS = [("single", 1, None)] + [
    ("fanout", count, quorum) for count in (2, 8) for quorum in (None, 1, count)
]
_DEADLINES = ("none", "integer", "float", "passed")


def _shapes() -> list[Any]:
    cases = []
    for (
        kind,
        count,
        quorum,
    ), route, failover, attempts, independent, deadline in itertools.product(
        _MEMBERS,
        assignments.ROUTES,
        assignments.FAILOVER_MODES,
        (1, 2, 3),
        (None, "hc:author"),
        _DEADLINES,
    ):
        name = (
            f"{kind}{count}-q{quorum}-{route}-{failover}-a{attempts}-"
            f"{'independent' if independent else 'plain'}-{deadline}"
        )
        cases.append(
            pytest.param(
                kind, count, quorum, route, failover, attempts, independent, deadline, id=name
            )
        )
    return cases


@pytest.mark.parametrize(
    ("kind", "count", "quorum", "route", "failover", "attempts", "independent", "deadline"),
    _shapes(),
)
def test_describe_round_trips_through_create(
    store: AssignmentStore,
    clock: Clock,
    kind: str,
    count: int,
    quorum: int | None,
    route: str,
    failover: str,
    attempts: int,
    independent: str | None,
    deadline: str,
) -> None:
    """red_at_pretip. ``create(id, message=first, **describe(id))`` is the stored assignment.

    Every shape ``create()`` accepts: both kinds; two and eight members; no quorum, quorum
    1 and quorum equal to the number of members; both routes; both failover modes;
    ``max_attempts`` 1, 2 and 3; with and without ``independent_of``; no deadline, an
    integer, a float, and a deadline that has passed since the first call.
    """
    given_deadline = {"none": None, "integer": 2000, "float": 2000.5, "passed": 1500.25}[deadline]
    assignment_id = SINGLE if kind == "single" else GROUP
    given: dict[str, Any] = {
        "kind": kind,
        "recipients": [R(peer) for peer in PEERS[:count]],
        "quorum": quorum,
        "route": route,
        "failover": failover,
        "max_attempts": attempts,
        "independent_of": independent,
        "deadline": given_deadline,
    }
    _, created = store.create(assignment_id, message=MESSAGE, **given)
    assert created is True
    if deadline == "passed":
        clock.now = 3000.0
    before = store.get(assignment_id)
    assert before["deadline_passed"] is (deadline == "passed")

    result = store.describe(assignment_id)

    assert type(result) is dict
    assert set(result) == KEYS and len(result) == len(KEYS)
    assert result["kind"] == kind and type(result["kind"]) is str
    assert type(result["recipients"]) is list
    assert result["recipients"] == given["recipients"]
    for recipient in result["recipients"]:
        assert type(recipient) is Recipient
        assert [type(field) for field in recipient] == [str, str, str, bool]
    assert result["quorum"] == quorum
    assert type(result["quorum"]) is (type(None) if quorum is None else int)
    assert result["route"] == route and type(result["route"]) is str
    assert result["failover"] == failover and type(result["failover"]) is str
    assert result["max_attempts"] == attempts and type(result["max_attempts"]) is int
    assert result["independent_of"] == independent
    assert type(result["independent_of"]) is (type(None) if independent is None else str)
    assert result["deadline"] == given_deadline
    assert type(result["deadline"]) is (type(None) if given_deadline is None else float)
    # The message is not returned in any form: neither its text nor its content identity.
    assert MESSAGE not in repr(result)
    assert content_identity(MESSAGE)["sha256"] not in repr(result)

    again, created = store.create(assignment_id, message=MESSAGE, **result)
    assert created is False
    assert again == before
    with pytest.raises(ValueError, match=OTHER_WORK):
        store.create(assignment_id, message=OTHER_MESSAGE, **result)
    assert store.get(assignment_id) == before
    assert store.describe(assignment_id) == result


def test_describe_returns_a_new_mapping_on_every_call(store: AssignmentStore) -> None:
    """red_at_pretip. Changing one answer changes neither the store nor the next answer."""
    store.create(GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE)
    first = described(store, GROUP)
    first["recipients"].append(R("hc:t3"))
    first["kind"] = "single"
    assert described(store, GROUP) == {
        "kind": "fanout",
        "recipients": [R("hc:t1"), R("hc:t2")],
        "quorum": None,
        "route": "direct",
        "failover": "off",
        "max_attempts": 2,
        "independent_of": None,
        "deadline": None,
    }


def test_other_work_is_refused_by_the_key_check_when_a_member_holds_a_receipt(
    store: AssignmentStore, receipts: Receipts
) -> None:
    """red_at_pretip. With a receipt under a member key ``create()`` has a second text.

    ``create()`` reads the member keys before it compares the identity (rulings, R1: "the
    order inside ``create()`` costs a different refusal text and no write"). The repeat
    with the first message is still the stored assignment.
    """
    view, _ = store.create(
        GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE
    )
    key = view["slots"][0]["attempts"][0]["member_key"]
    receipts.add(key, "queued", target="hc:default:t1")
    result = described(store, GROUP)
    rows = dump(store.path)
    with pytest.raises(
        ValueError, match=rf"^request key {re.escape(key)} already describes different work$"
    ):
        store.create(GROUP, message=OTHER_MESSAGE, **result)
    assert dump(store.path) == rows
    before = store.get(GROUP)
    again, created = store.create(GROUP, message=MESSAGE, **result)
    assert created is False and again == before


# ---------------------------------------------------------------- recipients (item 4)


@pytest.mark.parametrize(
    "order",
    [["hc:t1", "hc:t2", "hc:t3"], ["hc:t3", "hc:t1", "hc:t2"], ["hc:t2", "hc:t3", "hc:t1"]],
    ids=["123", "312", "231"],
)
def test_recipients_come_back_in_the_order_create_stored_them(
    store: AssignmentStore, order: list[str]
) -> None:
    """red_at_pretip. The stored order, and ``create()`` takes it back in any order."""
    given = [R(peer) for peer in order]
    store.create(GROUP, kind="fanout", recipients=given, message=MESSAGE)
    before = store.get(GROUP)
    result = described(store, GROUP)
    assert result["recipients"] == given
    assert [slot["peer_id"] for slot in before["slots"]] == order
    for recipients in (result["recipients"], list(reversed(result["recipients"]))):
        again, created = store.create(
            GROUP, message=MESSAGE, **{**result, "recipients": recipients}
        )
        assert created is False and again == before
    assert described(store, GROUP)["recipients"] == given


def test_a_successor_for_another_peer_is_never_among_the_recipients(
    store: AssignmentStore,
) -> None:
    """red_at_pretip. Attempts 2 and 3 of a slot go to other peers; attempt 1 is returned.

    Built through public calls only: the adapter call of attempt 1 raised before anything
    was reserved (``record_raised`` with no receipt under the key, a local recipient), so
    the slot is ``not_sent`` and ``plan_attempt`` accepts a successor for another peer.
    """
    given = [R("hc:t1"), R("hc:t2")]
    view, _ = store.create(
        GROUP,
        kind="fanout",
        recipients=given,
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    slot = view["slots"][0]["slot"]
    first = view["slots"][0]["attempts"][0]["member_key"]
    assert store.record_raised(first, "ConnectionError")["admission"] == "not_sent"
    second, created = store.plan_attempt(GROUP, slot, 2, R("hc:t8"), reason="never started")
    assert created and second["peer_id"] == "hc:t8"
    assert described(store, GROUP)["recipients"] == given
    assert store.record_raised(second["member_key"], "ConnectionError")["admission"] == "not_sent"
    third, created = store.plan_attempt(GROUP, slot, 3, R("hc:t0"), reason="never started")
    assert created and third["peer_id"] == "hc:t0"

    before = store.get(GROUP)
    assert [item["peer_id"] for item in before["slots"][0]["attempts"]] == [
        "hc:t1",
        "hc:t8",
        "hc:t0",
    ]
    result = described(store, GROUP)
    assert result["recipients"] == given
    again, created = store.create(GROUP, message=MESSAGE, **result)
    assert created is False and again == before


# ---------------------------------------------------------------- no lookup, no write (item 5)


def test_describe_uses_no_receipt_lookup_and_leaves_the_file_as_it_is(
    store: AssignmentStore, receipts: Receipts
) -> None:
    """red_at_pretip. Every method of the lookup raises; the file is byte for byte the same."""
    store.create(
        GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE, quorum=1
    )
    store.create(SINGLE, kind="single", recipients=[R("hc:t3")], message=MESSAGE)
    quiet = AssignmentStore(store.path, RefusingLookup(), clock=store.clock)  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="describe used the receipt lookup"):
        quiet.get(GROUP)  # the control: a status read does use the lookup
    folder = sorted(os.listdir(store.path.parent))
    content = store.path.read_bytes()
    receipts.calls.clear()

    assert described(quiet, GROUP)["quorum"] == 1
    assert described(quiet, SINGLE)["kind"] == "single"
    assert quiet.describe("ab-not-stored") is None
    assert described(store, GROUP)["quorum"] == 1

    assert receipts.calls == []
    assert store.path.read_bytes() == content
    assert sorted(os.listdir(store.path.parent)) == folder


def test_describe_binds_no_receipt_and_writes_no_winner_where_get_does(
    store: AssignmentStore, receipts: Receipts
) -> None:
    """red_at_pretip. A valid completed receipt under a member key, not yet bound.

    ``get`` binds it and writes the slot's winner (the control at the end); ``describe``
    leaves every row of both tables as it is.
    """
    view, _ = store.create(
        GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE
    )
    key = view["slots"][0]["attempts"][0]["member_key"]
    task_id = receipts.add(key, "completed", target="hc:default:t1")
    quiet = AssignmentStore(store.path, RefusingLookup(), clock=store.clock)  # type: ignore[arg-type]
    rows = dump(store.path)
    content = store.path.read_bytes()
    receipts.calls.clear()

    for reader in (quiet, store):
        assert described(reader, GROUP)["recipients"] == [R("hc:t1"), R("hc:t2")]
    assert receipts.calls == []
    assert dump(store.path) == rows
    assert store.path.read_bytes() == content

    after = store.get(GROUP)  # the control
    attempt = after["slots"][0]["attempts"][0]
    assert (attempt["task_id"], attempt["winner"], attempt["admission"]) == (
        task_id,
        True,
        "submitted",
    )
    assert dump(store.path) != rows
    assert store.path.read_bytes() != content
    assert described(store, GROUP)["recipients"] == [R("hc:t1"), R("hc:t2")]


@pytest.mark.parametrize("assignment_id", [GROUP, "ab-not-stored"], ids=["stored", "unknown"])
def test_describe_reads_both_tables_in_one_read_transaction(
    store: AssignmentStore, monkeypatch: pytest.MonkeyPatch, assignment_id: str
) -> None:
    """red_at_pretip. One connection, one deferred ``BEGIN``, ``SELECT`` only, one ``COMMIT``."""
    store.create(GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE)
    opened: list[list[str]] = []
    connect = sqlite3.connect

    def traced(*args: Any, **options: Any) -> sqlite3.Connection:
        db = connect(*args, **options)
        opened.append([])
        db.set_trace_callback(opened[-1].append)
        return db

    monkeypatch.setattr(assignments.sqlite3, "connect", traced)
    store.describe(assignment_id)
    monkeypatch.undo()

    assert len(opened) == 1
    statements = [" ".join(text.split()) for text in opened[0]]
    work = [text for text in statements if not text.upper().startswith("PRAGMA FOREIGN_KEYS")]
    assert work[0].upper() == "BEGIN" and work[-1].upper() == "COMMIT"
    reads = work[1:-1]
    assert reads and all(text.upper().startswith("SELECT ") for text in reads)
    tables = [re.findall(r"\bFROM (\w+)", text) for text in reads]
    if assignment_id == GROUP:
        assert tables == [["assignments"], ["assignment_members"]]
    else:
        assert tables == [["assignments"]]


# ---------------------------------------------------------------- unknown and malformed IDs


def test_an_id_that_no_assignment_has_gives_none(store: AssignmentStore) -> None:
    """red_at_pretip. Item 1, also in a store that holds other assignments."""
    assert store.describe(GROUP) is None
    store.create(GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE)
    assert store.describe(SINGLE) is None
    assert store.describe(GROUP + "x") is None
    assert store.describe(GROUP[:-1]) is None
    assert store.describe(GROUP.upper()) is None


def test_an_id_of_the_other_kinds_length_is_read_like_any_other(store: AssignmentStore) -> None:
    """red_at_pretip. 97 to 120 characters fit a single assignment and no group.

    ``describe`` is not told the kind, so it accepts what either kind accepts: such an ID
    is ``None`` when it is not stored and is described when a single assignment has it.
    """
    longest = "s" * 120
    assert store.describe("g" * 97) is None
    assert store.describe(longest) is None
    store.create(longest, kind="single", recipients=[R("hc:t1")], message=MESSAGE)
    result = described(store, longest)
    assert result["kind"] == "single" and result["recipients"] == [R("hc:t1")]
    again, created = store.create(longest, message=MESSAGE, **result)
    assert created is False and again == store.get(longest)
    assert store.describe("s" * 119) is None
    with pytest.raises(ValueError, match=BAD_ID):
        store.describe("s" * 121)


@pytest.mark.parametrize(
    "assignment_id",
    [
        None,
        5,
        5.0,
        True,
        b"5",
        ["5"],
        "",
        " ",
        "ab\x00cd",
        "ab\ncd",
        "ab-20261121\n",
        "ab cd",
        "ab/cd",
        "é" * 8,
        "x" * 5000,
    ],
    ids=[
        "none",
        "integer",
        "float",
        "bool",
        "bytes",
        "list",
        "empty",
        "space",
        "nul",
        "newline",
        "trailing-newline",
        "inner-space",
        "slash",
        "non-ascii",
        "5000",
    ],
)
def test_a_malformed_id_is_refused_with_a_fixed_text(
    store: AssignmentStore, receipts: Receipts, assignment_id: Any
) -> None:
    """red_at_pretip. Item 6: ``ValueError`` with one fixed text, before the file is opened.

    The text names no value of the caller (ruling R13). An integer does not reach SQLite,
    where it would compare equal to the stored text ``5``.
    """
    store.create("5", kind="single", recipients=[R("hc:t1")], message=MESSAGE)
    content = store.path.read_bytes()
    receipts.calls.clear()
    with pytest.raises(ValueError, match=BAD_ID) as caught:
        store.describe(assignment_id)
    text = str(caught.value)
    assert len(text) < 80
    if isinstance(assignment_id, str) and assignment_id.strip():
        assert assignment_id not in text
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert receipts.calls == [] and store.path.read_bytes() == content
    assert described(store, "5")["kind"] == "single"


def test_a_malformed_id_is_refused_before_the_store_is_opened(
    store: AssignmentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip. No connection is made for an ID that cannot be stored."""

    def refuse(*args: Any, **options: Any) -> sqlite3.Connection:
        raise AssertionError("the store was opened")

    monkeypatch.setattr(assignments.sqlite3, "connect", refuse)
    for assignment_id in (None, 5, "", "ab\ncd", "x" * 5000):
        with pytest.raises(ValueError, match=BAD_ID):
            store.describe(assignment_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------- a store that cannot be read


def test_a_store_that_cannot_be_read_raises_as_get_does(store: AssignmentStore) -> None:
    """red_at_pretip. Item 7: the module's behaviour for a read, ``sqlite3.Error``; no retry."""
    store.create(GROUP, kind="fanout", recipients=[R("hc:t1"), R("hc:t2")], message=MESSAGE)
    store.path.write_bytes(b"this is not a database file\n" * 200)
    with pytest.raises(sqlite3.DatabaseError) as from_get:
        store.get(GROUP)
    with pytest.raises(sqlite3.DatabaseError) as from_describe:
        store.describe(GROUP)
    assert type(from_describe.value) is type(from_get.value)
    assert str(from_describe.value) == str(from_get.value)


# ================================================================ fix round 1 (review 1)

MISSING = "unable to open database file"
MEMBERS = [R("hc:t1"), Recipient("kiro:rev", "kiro:rev", "kiro", False)]
STORED = {
    "kind": "fanout",
    "recipients": MEMBERS,
    "quorum": 1,
    "route": "direct",
    "failover": "off",
    "max_attempts": 2,
    "independent_of": None,
    "deadline": None,
}
# The writer that is killed: its own process, a write transaction, changed rows, then "ready".
# ``cache_size=1`` makes SQLite write changed pages into the database file before any commit.
WRITER = """
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=1")
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE assignment_members SET target='interrupted-write', reason='interrupted'")
print("ready", flush=True)
time.sleep(600)
"""


def listing(folder: Path) -> list[tuple[str, int]]:
    return sorted((name, (folder / name).stat().st_size) for name in os.listdir(folder))


@pytest.mark.parametrize("gone", ["file", "folder"])
def test_describe_of_a_missing_store_raises_and_makes_no_file(
    store: AssignmentStore, gone: str
) -> None:
    """B1. ``file``: red_at_pretip (an empty file was made there, then ``no such table``).

    ``folder``: pins_existing (SQLite makes no folder; the error was the same at the pre-tip).
    """
    store.create(GROUP, **STORED, message=MESSAGE)
    folder = store.path.parent
    if gone == "file":
        store.path.unlink()
        watched = folder
    else:
        shutil.rmtree(folder)
        watched = folder.parent
    before = listing(watched)
    for assignment_id in (GROUP, "ab-not-stored"):
        with pytest.raises(sqlite3.OperationalError, match=f"^{MISSING}$") as caught:
            store.describe(assignment_id)
        assert caught.value.sqlite_errorname == "SQLITE_CANTOPEN"
        assert listing(watched) == before
    assert not store.path.exists()


def test_describe_after_a_killed_writer_reads_the_committed_state_as_get_does(
    store: AssignmentStore, tmp_path: Path
) -> None:
    """B1, pins_existing: what ``out/F4-P-fix1-opens.log`` measured, for ``describe`` and ``get``.

    The writer is ended with ``SIGKILL`` inside a write transaction, so its journal stays
    beside a database file that already holds uncommitted pages. The first ``describe``
    returns the last committed state; opening the file lets SQLite complete its own rollback,
    which puts the committed bytes back and removes the journal. ``get`` (the control, on a
    copy of the same two files) does the same. An open that cannot write the file would raise
    here on every call until some other call had opened the store.
    """
    for number in range(150):
        store.create(f"ab-{number}", **STORED, message=MESSAGE)
    committed = store.path.read_bytes()
    journal = store.path.with_name(store.path.name + "-journal")
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(store.path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert writer.stdout is not None and writer.stdout.readline().strip() == "ready"
    finally:
        writer.kill()
        assert writer.wait() == -signal.SIGKILL
    assert journal.exists() and journal.stat().st_size > 0
    assert store.path.read_bytes() != committed  # uncommitted pages are in the file
    control = tmp_path / "control"
    shutil.copytree(store.path.parent, control)

    assert store.describe("ab-7") == STORED
    assert not journal.exists()
    assert store.path.read_bytes() == committed
    assert listing(store.path.parent) == [(store.path.name, len(committed))]
    assert store.describe("ab-7") == STORED

    other = AssignmentStore.__new__(AssignmentStore)  # no __init__: it opens the file itself
    other.path, other.receipts, other.clock = control / store.path.name, Receipts(), store.clock
    view = other.get("ab-7")
    assert [slot["attempts"][0]["target"] for slot in view["slots"]] == [
        member.target for member in MEMBERS
    ]
    assert not (control / journal.name).exists()
    assert other.path.read_bytes() == committed


def test_describe_reads_a_store_whose_path_has_characters_a_uri_gives_a_meaning(
    tmp_path: Path, receipts: Receipts, clock: Clock
) -> None:
    """B1, pins_existing. A space, ``?``, ``#``, ``%`` and letters outside ASCII reach SQLite whole.

    ``%41`` would read ``A`` and ``?mode=memory`` would be an option if the path were put into
    the URI as it is.
    """
    path = tmp_path / "state é #1 100%41 ?x" / "assign ments?mode=memory#frag%20ü.sqlite"
    odd = AssignmentStore(path, receipts, clock=clock)
    odd.create(GROUP, **STORED, message=MESSAGE)
    before = listing(path.parent)
    content = path.read_bytes()
    assert odd.describe(GROUP) == STORED
    assert odd.describe("ab-not-stored") is None
    assert listing(path.parent) == before and path.read_bytes() == content
    assert sorted(os.listdir(tmp_path)) == ["state é #1 100%41 ?x"]


def test_describe_reads_a_store_that_this_user_may_not_write(store: AssignmentStore) -> None:
    """B1, pins_existing. The file and its folder without write permission: the read succeeds."""
    store.create(GROUP, **STORED, message=MESSAGE)
    content = store.path.read_bytes()
    folder = store.path.parent
    before = listing(folder)
    store.path.chmod(0o400)
    folder.chmod(0o500)
    try:
        assert store.describe(GROUP) == STORED
        assert store.describe("ab-not-stored") is None
        assert listing(folder) == before and store.path.read_bytes() == content
    finally:
        folder.chmod(0o700)
        store.path.chmod(0o600)


def test_describe_waits_for_another_openers_lock_with_one_connection(
    store: AssignmentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1, pins_existing. One connection with a lock wait of 10 seconds, and it does wait.

    Another opener holds the exclusive lock for 0.6 seconds; ``describe`` answers after it
    is released, without a second connection.
    """
    store.create(GROUP, **STORED, message=MESSAGE)
    holder = sqlite3.connect(store.path, isolation_level=None, check_same_thread=False)
    calls: list[dict[str, Any]] = []
    connect = sqlite3.connect

    def counted(*args: Any, **options: Any) -> sqlite3.Connection:
        calls.append(options)
        return connect(*args, **options)

    monkeypatch.setattr(assignments.sqlite3, "connect", counted)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        release = threading.Timer(0.6, holder.execute, ["ROLLBACK"])
        started = time.monotonic()
        release.start()
        assert store.describe(GROUP) == STORED
        waited = time.monotonic() - started
        release.join()
    finally:
        holder.close()
    assert waited > 0.5
    assert len(calls) == 1 and calls[0]["timeout"] == 10


@pytest.mark.parametrize("damage", ["unreadable", "other_work"])
def test_the_repeat_runs_creates_receipt_checks_and_can_refuse_the_first_message(
    store: AssignmentStore, receipts: Receipts, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """B2, pins_existing: ``create()``'s behaviour, seen through the round trip.

    ``create()`` looks the receipt under every member key up before it compares the work. With
    a receipt that cannot be read, or one that holds other work, the attempt is uncertain
    after ``record_raised``; ``describe`` still returns the eight arguments, and the repeat
    with the first message is refused. No row changes.
    """
    view, created = store.create(SINGLE, kind="single", recipients=[R("hc:t1")], message=MESSAGE)
    assert created
    key = view["slots"][0]["attempts"][0]["member_key"]
    if damage == "unreadable":

        def fail(request_key: str) -> None:
            raise OSError("the receipt store cannot be read")

        monkeypatch.setattr(receipts, "by_request", fail)
        refusal = rf"^The receipt under request key {re.escape(key)} cannot be read$"
    else:
        receipts.add(key, "queued", target="hc:default:t1", message=OTHER_MESSAGE)
        refusal = rf"^request key {re.escape(key)} already describes different work$"
    assert store.record_raised(key, "ConnectionError")["admission"] == "uncertain"
    rows = dump(store.path)
    content = store.path.read_bytes()

    result = described(store, SINGLE)
    assert result == {
        "kind": "single",
        "recipients": [R("hc:t1")],
        "quorum": None,
        "route": "direct",
        "failover": "off",
        "max_attempts": 2,
        "independent_of": None,
        "deadline": None,
    }
    with pytest.raises(ValueError, match=refusal):
        store.create(SINGLE, message=MESSAGE, **result)
    assert dump(store.path) == rows and store.path.read_bytes() == content


# ================================================================ fix round 2 (review 2)

# The same writer, but it keeps every changed page in its own memory: when it is killed, its
# journal is beside a database file that still holds the committed bytes.
WRITER_IN_MEMORY = WRITER.replace("PRAGMA cache_size=1", "PRAGMA cache_spill=OFF")


def kill_a_writer(path: Path, script: str) -> None:
    """Run ``script`` on the file until it says ``ready``; end it with ``SIGKILL``; wait for it."""
    command = [sys.executable, "-c", script, str(path)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as writer:
        try:
            assert writer.stdout is not None and writer.stdout.readline().strip() == "ready"
        finally:
            writer.kill()
            assert writer.wait() == -signal.SIGKILL


def test_a_rollback_that_the_folder_does_not_allow_raises_and_a_later_describe_answers(
    store: AssignmentStore,
) -> None:
    """R2-B1, pins_existing. The store needs recovery, its file may be written, its folder not.

    SQLite's rollback cannot finish there, and ``describe`` raises. Only the class is the
    contract: no text is asserted, and nothing about the two files in between. With the folder
    writable again ``describe`` returns what was committed before the writer started.
    """
    for number in range(150):
        store.create(f"ab-{number}", **STORED, message=MESSAGE)
    committed = store.path.read_bytes()
    kill_a_writer(store.path, WRITER)
    assert store.path.read_bytes() != committed  # uncommitted pages are in the file
    folder = store.path.parent
    folder.chmod(0o500)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("this process may write into a folder without the write bit (root)")
        with pytest.raises(sqlite3.OperationalError):
            store.describe("ab-7")
    finally:
        folder.chmod(0o700)
    assert store.describe("ab-7") == STORED
    assert store.describe("ab-not-stored") is None
    assert store.path.read_bytes() == committed


def test_a_killed_writer_that_wrote_no_page_into_the_file_leaves_nothing_to_recover(
    store: AssignmentStore,
) -> None:
    """R2-B2, pins_existing. A journal beside the file, and yet no recovery and no change.

    ``describe`` returns the committed description, and the bytes of the file and of the
    journal are the same before and after; once more with the file and the folder not writable.
    """
    for number in range(10):
        store.create(f"ab-{number}", **STORED, message=MESSAGE)
    committed = store.path.read_bytes()
    kill_a_writer(store.path, WRITER_IN_MEMORY)
    journal = store.path.with_name(store.path.name + "-journal")
    left = journal.read_bytes()
    assert left and store.path.read_bytes() == committed  # no page of the writer is in the file
    folder = store.path.parent
    before = listing(folder)

    assert store.describe("ab-7") == STORED
    assert store.path.read_bytes() == committed and journal.read_bytes() == left
    store.path.chmod(0o400)
    journal.chmod(0o400)
    folder.chmod(0o500)
    try:
        assert store.describe("ab-7") == STORED
        assert store.describe("ab-not-stored") is None
        assert store.path.read_bytes() == committed and journal.read_bytes() == left
    finally:
        folder.chmod(0o700)
        journal.chmod(0o600)
        store.path.chmod(0o600)
    assert listing(folder) == before
