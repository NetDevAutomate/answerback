"""Assignment store, review round 1: the Codex reviewer's reproductions (F2 fix round 1).

Source: the reviewer's scratch file ``test_f2_review.py`` (review
``F2-review-codex.md``), ported to the fixed interface (``Recipient``). Test names keep the
reviewer's names, so a finding can be traced to its test.

Labels. At ``3fa106c`` (the start of fix round 1) every test here fails on import, because
``Recipient`` does not exist there. The label says whether the *behaviour* was wrong at
``3fa106c``: ``red_at_pretip`` when the reviewer's unported probe failed there
(``out/F2-fix1-red.log``), ``pins_existing`` when it passed and the test pins it.

Receipts come from a real ``TaskStore`` or from ``MemoryReceipts``, whose rows have the
``TaskStore.decode`` shape. ``MemoryReceipts.add`` has no default target. Targets follow
``CoreTasks.target_key`` (``hc:<session>:<terminal>``). The spawned processes use their own
``TaskStore`` and SQLite connections; they start no bridge and no agent.
"""

import multiprocessing
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.assignments import APPLICATION_ID, AssignmentStore, Recipient
from answerback_bridge.process import BridgeError
from answerback_bridge.tasks import STATES, TaskStore

MESSAGE = "Independent F2 review"
T1 = Recipient("hc:t1", "hc:default:t1", "hc", True)
T2 = Recipient("hc:t2", "hc:default:t2", "hc", True)
T3 = Recipient("hc:t3", "hc:default:t3", "hc", True)


class MemoryReceipts:
    """Receipts in the ``TaskStore.decode`` shape; every receipt names its target."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.error: Exception | None = None

    def add(self, key: str, status: str | None = "uncertain", *, target: str, **changes: Any):
        receipt = {
            "id": f"receipt-{key}",
            "request_key": key,
            "message": MESSAGE,
            "target": target,
            "metadata": {"kind": "hc"},
            "status": status,
        }
        receipt.update(changes)
        self.rows[key] = receipt
        return receipt

    def by_request(self, key: str) -> dict[str, Any] | None:
        if self.error is not None:
            raise self.error
        return self.rows.get(key)

    def get(self, task_id: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        for row in self.rows.values():
            if row.get("id") == task_id:
                return row
        raise ValueError("Unknown task ID")


def make_store(tmp_path: Path, receipts: Any = None, **options: Any):
    receipts = MemoryReceipts() if receipts is None else receipts
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts)
    view, _ = store.create(
        "review",
        kind="single",
        recipients=[T1],
        message=MESSAGE,
        failover="never_started",
        **options,
    )
    return store, receipts, view


def first(view: dict[str, Any]) -> dict[str, Any]:
    return view["slots"][0]["attempts"][0]


# ---------------------------------------------------------------- finding 1


@pytest.mark.parametrize("path", ["adopt", "record", "recover", "successor"])
@pytest.mark.parametrize(("target", "kind"), [("hc:default:t99", "hc"), ("hc:default:t1", "herdr")])
def test_wrong_recipient_receipt_must_not_satisfy(
    tmp_path: Path, path: str, target: str, kind: str
) -> None:
    """red_at_pretip: finding 1, every binding path checks target and provider."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store = AssignmentStore(tmp_path / "assignments.sqlite", tasks)
    if path == "successor" and kind != "hc":
        target = "hc:default:t2"  # the successor's own target, with the wrong provider
    if path == "adopt":
        task, _ = tasks.reserve(target, MESSAGE, "review", {"kind": kind})
        tasks.update(task["id"], "completed", result="wrong recipient's result")
        with pytest.raises(ValueError, match="different work"):
            store.create("review", kind="single", recipients=[T1], message=MESSAGE)
        return
    store.create(
        "review", kind="single", recipients=[T1], message=MESSAGE, failover="never_started"
    )
    key = "review"
    if path == "successor":
        task, _ = tasks.reserve("hc:default:t1", MESSAGE, key, {"kind": "hc"})
        tasks.update(task["id"], "failed")
        key = store.plan_attempt("review", "0", 2, T2)[0]["member_key"]
    task, _ = tasks.reserve(target, MESSAGE, key, {"kind": kind})
    tasks.update(task["id"], "completed", result="wrong recipient's result")
    if path == "record":
        with pytest.raises(ValueError, match="does not belong"):
            store.record_submitted(key, task["id"])
    view = store.get("review")
    assert view["status"] != "satisfied"
    assert view["slots"][0]["winner"] is None
    assert view["slots"][0]["attempts"][-1]["lookup"] == "conflicting"


# ---------------------------------------------------------------- finding 2


@pytest.mark.parametrize(
    "damage", ["missing_message", "wrong_message", "missing_id", "bad_retention"]
)
@pytest.mark.parametrize("operation", ["get", "raised", "successor"])
def test_invalid_uncertain_receipt_is_not_absence(
    tmp_path: Path, damage: str, operation: str
) -> None:
    """red_at_pretip: finding 2, conflicting or unreadable is never "no receipt"."""
    store, receipts, _ = make_store(tmp_path)
    if operation == "successor":
        store.record_raised("review", "local refusal")
    row = receipts.add("review", target="hc:default:t1")
    if damage == "missing_message":
        row.pop("message")
    elif damage == "wrong_message":
        row["message"] = "different work under the same key"
    elif damage == "missing_id":
        row.pop("id")
    else:
        row["_retention"] = {"fields": {"message": {}}}
    expected = "conflicting" if damage == "wrong_message" else "unavailable"
    if operation == "get":
        view = store.get("review")
        assert view["slots"][0]["status"] == "needs_decision"
        assert first(view)["lookup"] == expected
    elif operation == "raised":
        attempt = store.record_raised("review", "adapter raised")
        assert attempt["admission"] == "uncertain"
        assert attempt["lookup"] == expected
    else:
        with pytest.raises(ValueError, match="no successor|cannot be planned"):
            store.plan_attempt("review", "0", 2, T2)


@pytest.mark.parametrize("status", [*STATES, "future_state", None])
@pytest.mark.parametrize("bound", [False, True])
def test_all_receipt_states_have_conservative_reads(
    tmp_path: Path, status: str | None, bound: bool
) -> None:
    """pins_existing: every receipt state, bound or found by key."""
    store, receipts, _ = make_store(tmp_path)
    row = receipts.add("review", status, target="hc:default:t1")
    if bound:
        store.record_submitted("review", row["id"])
    view = store.get("review")
    if status in ("pending", "queued", "submitted", "running"):
        # Fix round 3 (N2): `active` needs the live facts; without them, needs_decision.
        assert view["slots"][0]["status"] == "needs_decision"
        live = {"task_id": row["id"], "core_blocked": False, "stalled": False}
        view = store.get("review", assessment={"review": live})
    expected = {
        "pending": "active",
        "queued": "active",
        "submitted": "active",
        "running": "active",
        "blocked": "blocked",
        "completed": "satisfied",
    }.get(str(status), "needs_decision")
    assert view["slots"][0]["status"] == expected
    if status not in ("failed", "cancelled", "abandoned"):
        with pytest.raises(ValueError):
            store.plan_attempt("review", "0", 2, T2)


@pytest.mark.parametrize(
    "error", [ValueError("corrupt receipt"), sqlite3.DatabaseError("corrupt"), OSError("io")]
)
@pytest.mark.parametrize("bound", [False, True])
def test_receipt_read_errors_cannot_authorize_successor(
    tmp_path: Path, error: Exception, bound: bool
) -> None:
    """red_at_pretip for the read (it raised at 3fa106c for two error types); the refusal
    pins_existing. Fix round 1 reads every lookup failure as `unavailable`."""
    store, receipts, _ = make_store(tmp_path)
    row = receipts.add("review", target="hc:default:t1")
    if bound:
        store.record_submitted("review", row["id"])
    receipts.error = error
    view = store.get("review")
    assert view["slots"][0]["status"] == "needs_decision"
    assert first(view)["lookup"] == "unavailable"
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt("review", "0", 2, T2)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT count(*) FROM assignment_members").fetchone()[0] == 1


def test_existing_not_sent_attempt_refreshes_receipt_before_replanning(tmp_path: Path) -> None:
    """red_at_pretip: finding 2, the repeat of a `not_sent` attempt looks the key up."""
    store, receipts, _ = make_store(tmp_path)
    store.record_raised("review", "local refusal")
    receipts.add("review", "uncertain", target="hc:default:t1")
    attempt, created = store.plan_attempt("review", "0", 1, T1)
    assert not created
    assert attempt["admission"] == "uncertain"
    assert attempt["receipt_status"] == "uncertain"
    assert store.get("review")["slots"][0]["status"] == "needs_decision"


@pytest.mark.parametrize("damage", ["wrong_target", "unreadable"])
def test_existing_not_sent_attempt_with_a_bad_receipt_is_not_planned_again(
    tmp_path: Path, damage: str
) -> None:
    """red_at_pretip: the same, for a conflicting or unreadable receipt under the key."""
    store, receipts, _ = make_store(tmp_path)
    store.record_raised("review", "local refusal")
    if damage == "wrong_target":
        receipts.add("review", target="hc:default:t99")
    else:
        receipts.error = OSError("io")
    with pytest.raises(ValueError, match="cannot be planned again"):
        store.plan_attempt("review", "0", 1, T1)
    assert first(store.get("review"))["admission"] == "not_sent"


# ---------------------------------------------------------------- finding 3


def test_recovery_read_cannot_hide_later_adapter_exception(tmp_path: Path) -> None:
    """red_at_pretip: finding 3, a recovery bind does not prove the call returned."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks)
    tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
    observer = AssignmentStore(store.path, tasks)
    assert observer.get("review")["status"] == "needs_decision"  # no live facts (N2)
    assert observer.get("review")["slots"][0]["attempts"][0]["admission"] == "submitted"
    attempt = store.record_raised("review", "RuntimeError")
    view = store.get("review")
    assert attempt["admission"] == "uncertain"
    assert view["status"] == "needs_decision"


def test_record_submitted_after_the_raise_keeps_uncertain(tmp_path: Path) -> None:
    """pins_existing: a later bind does not erase a recorded failure."""
    store, receipts, _ = make_store(tmp_path)
    row = receipts.add("review", "running", target="hc:default:t1")
    store.record_raised("review", "RuntimeError")
    assert store.record_submitted("review", row["id"])["admission"] == "uncertain"
    assert store.get("review")["slots"][0]["status"] == "needs_decision"


# ---------------------------------------------------------------- finding 5 (part)


@pytest.mark.parametrize("status", ["failed", "cancelled", "abandoned"])
def test_admission_uncertain_is_not_erased_by_final_receipt(tmp_path: Path, status: str) -> None:
    """red_at_pretip: draft slot row "latest attempt uncertain" reads `needs_decision`."""
    store, receipts, _ = make_store(tmp_path, max_attempts=1)
    receipts.add("review", status, target="hc:default:t1")
    store.record_raised("review", "lost adapter reply")
    view = store.get("review")
    assert view["slots"][0]["status"] == "needs_decision"


def test_no_candidate_and_stall_have_no_status_input(tmp_path: Path) -> None:
    """pins_existing: F2 reads no invented metadata names for status, only `assessment`.
    Changed in fix round 3 (N2): metadata that claims a stall is still not read; the
    live slot reads `needs_decision` because its Core and stall facts were not supplied,
    and supplying them false gives `active`."""
    store, receipts, _ = make_store(tmp_path)
    receipts.add(
        "review", "failed", target="hc:default:t1", metadata={"kind": "hc", "delivery": "x"}
    )
    view = store.get("review")
    assert view["slots"][0]["status"] == "needs_decision"
    receipts.rows["review"]["status"] = "submitted"
    receipts.rows["review"]["metadata"].update(core_status="blocked", stalled=True)
    view = store.get("review")
    assert view["slots"][0]["status"] == "needs_decision"
    assert view["slots"][0]["assessment_absent"] == ["core_blocked", "stalled"]
    live = {"task_id": "receipt-review", "core_blocked": False, "stalled": False}
    assert store.get("review", assessment={"review": live})["slots"][0]["status"] == "active"


# ---------------------------------------------------------------- finding 6


def test_invalidated_winner_is_not_currently_satisfied(tmp_path: Path) -> None:
    """red_at_pretip: finding 6, the winner stays stored; the slot needs a decision."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks)
    task, _ = tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
    tasks.update(task["id"], "completed", result="provisional wrong result")
    assert store.get("review")["status"] == "satisfied"
    tasks.invalidate_submission(task["id"], "delivery identity mismatch")
    view = store.get("review")
    assert view["slots"][0]["winner"] == 1
    assert first(view)["invalidated"] is True
    assert view["status"] == "needs_decision"
    with pytest.raises(ValueError, match="satisfied"):
        store.plan_attempt("review", "0", 2, T2)


# ---------------------------------------------------------------- finding 7


def test_foreign_task_database_is_refused_without_modification(tmp_path: Path) -> None:
    """red_at_pretip: finding 7."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    before = tasks.path.read_bytes()
    with pytest.raises(ValueError, match="not an assignment store"):
        AssignmentStore(tasks.path, tasks)
    assert tasks.path.read_bytes() == before
    TaskStore(tasks.path)  # still opens


@pytest.mark.parametrize("damage", ["version", "schema"])
def test_a_marked_store_with_another_schema_is_refused(tmp_path: Path, damage: str) -> None:
    """red_at_pretip for `schema` (round 1 accepted a changed schema); `version` pins_existing
    (round 1 refused a newer version too). The marker alone is not enough."""
    path = tmp_path / "assignments.sqlite"
    AssignmentStore(path, MemoryReceipts())
    with closing(sqlite3.connect(path)) as db:
        if damage == "version":
            db.execute("PRAGMA user_version=2")
        else:
            db.execute("DROP TRIGGER assignment_winner_write_once")
        db.commit()
    with pytest.raises(ValueError, match="schema"):
        AssignmentStore(path, MemoryReceipts())


@pytest.mark.parametrize("case", ["corrupt", "newer", "symlink_file", "symlink_parent"])
def test_bad_store_paths_and_versions_fail(tmp_path: Path, case: str) -> None:
    """pins_existing"""
    path = tmp_path / "state" / "assignments.sqlite"
    path.parent.mkdir()
    expected: type[Exception]
    if case == "corrupt":
        path.write_bytes(b"not a database")
        expected = sqlite3.DatabaseError
    elif case == "newer":
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=2")
        expected = ValueError
    elif case == "symlink_file":
        real = tmp_path / "real.sqlite"
        real.write_bytes(b"")
        path.symlink_to(real)
        expected = ValueError
    else:
        path.parent.rmdir()
        real = tmp_path / "real"
        real.mkdir()
        path.parent.symlink_to(real, target_is_directory=True)
        expected = BridgeError
    with pytest.raises(expected):
        AssignmentStore(path, MemoryReceipts())


def test_schema_marker_and_constraints(tmp_path: Path) -> None:
    """red_at_pretip for the marker and foreign keys; modes and timeout pins_existing."""
    store, _, _ = make_store(tmp_path)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    with store._connection() as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE assignment_members SET admission='done'")


def test_ancestor_symlink_and_hardlink_are_followed(tmp_path: Path) -> None:
    """pins_existing, a characterization: only the leaf and its parent are checked."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    store = AssignmentStore(alias / "nested" / "assignments.sqlite", MemoryReceipts())
    assert store.path.resolve() == real / "nested" / "assignments.sqlite"
    hardlink = real / "other.sqlite"
    os.link(store.path, hardlink)
    AssignmentStore(hardlink, MemoryReceipts())
    assert hardlink.stat().st_ino == store.path.stat().st_ino


# ---------------------------------------------------------------- finding 8


def test_late_live_attempt_is_listed_after_another_attempt_wins(tmp_path: Path) -> None:
    """red_at_pretip: finding 8. `outstanding` stays "slots that have not met the rule";
    rewrite c2 also lists the uncertain-admission completion in the won slot."""
    store, receipts, _ = make_store(tmp_path)
    store.record_raised("review", "refusal")
    store.plan_attempt("review", "0", 2, T2)
    receipts.add("review.a2", "running", target="hc:default:t2")
    receipts.add("review", "completed", target="hc:default:t1")
    # Fix round 4 (R4-1): the running attempt 2 carries its unknown Core state.
    assert store.get("review")["status"] == "needs_decision"
    live = {"task_id": "receipt-review.a2", "core_blocked": False, "stalled": False}
    view = store.get("review", assessment={"review.a2": live})
    assert view["status"] == "needs_decision"  # c2: earlier completion still needs Core
    assert view["outstanding"] == []
    assert view["unfinished_attempts"] == [
        {
            "slot": "0",
            "attempt": 1,
            "task_id": "receipt-review",
            "lookup": "valid",
            "receipt_status": "completed",
        },
        {
            "slot": "0",
            "attempt": 2,
            "task_id": "receipt-review.a2",
            "lookup": "valid",
            "receipt_status": "running",
        },
    ]


# ---------------------------------------------------------------- finding 9


def test_locate_finds_an_attempt_by_task_id_or_member_key(tmp_path: Path) -> None:
    """red_at_pretip: finding 9, the reverse lookup F6 needs for `task_failover(task_id)`."""
    receipts = MemoryReceipts()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts)
    view, _ = store.create("grp", kind="fanout", recipients=[T1, T2], message=MESSAGE)
    key = view["slots"][1]["attempts"][0]["member_key"]
    row = receipts.add(key, "running", target="hc:default:t2")
    store.record_submitted(key, row["id"])
    expected = {
        "assignment_id": "grp",
        "slot": view["slots"][1]["slot"],
        "attempt": 1,
        "member_key": key,
        "task_id": row["id"],
    }
    assert store.locate(task_id=row["id"]) == expected
    assert store.locate(member_key=key) == expected
    assert store.locate(task_id="nothing") is None
    with pytest.raises(ValueError, match="exactly one"):
        store.locate()
    with pytest.raises(ValueError, match="exactly one"):
        store.locate(task_id=row["id"], member_key=key)


# ---------------------------------------------------------------- finding 10


@pytest.mark.parametrize(
    "recipient",
    [
        Recipient("node:other-host/hc:t1", "node:other-host/hc:t1", "node", False),
        Recipient("hc:other-host/t1", "hc:default:other-host/t1", "hc", False),
    ],
)
def test_remote_lookup_miss_does_not_prove_not_sent(tmp_path: Path, recipient: Recipient) -> None:
    """red_at_pretip: finding 10, plan section 6 proof 1 is for local recipients only."""
    store = AssignmentStore(tmp_path / "assignments.sqlite", MemoryReceipts())
    store.create(
        "review", kind="single", recipients=[recipient], message=MESSAGE, failover="never_started"
    )
    attempt = store.record_raised("review", "TransportUnavailable")
    assert attempt["admission"] == "uncertain"
    assert "remote recipient" in attempt["detail"]
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt("review", "0", 2, T2)


def test_started_or_abandoned_work_needs_a_never_started_guard(tmp_path: Path) -> None:
    """pins_existing, a characterization of the F3/F6 boundary (finding 10): the store
    accepts a successor to an abandoned receipt and stores `verdict` as a label only."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks)
    task, _ = tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
    task = tasks.update(task["id"], "uncertain")
    tasks.abandon(task, "release reservation, execution may continue")
    attempt, created = store.plan_attempt("review", "0", 2, T2, verdict="never")
    assert created and attempt["verdict"] == "never"


# ---------------------------------------------------------------- finding 11


def test_same_successor_retry_can_omit_optional_message(tmp_path: Path) -> None:
    """red_at_pretip: finding 11, an omitted message matches the stored attempt."""
    store, receipts, _ = make_store(tmp_path)
    receipts.add("review", "failed", target="hc:default:t1")
    store.plan_attempt("review", "0", 2, T2, message="Attempt 2 header\n" + MESSAGE)
    attempt, created = store.plan_attempt("review", "0", 2, T2)
    assert not created and attempt["member_key"] == "review.a2"
    with pytest.raises(ValueError, match="different work"):
        store.plan_attempt("review", "0", 2, T2, message="Another header\n" + MESSAGE)


def test_retry_options_are_identity_but_default_omission_is_not_inherited(
    tmp_path: Path,
) -> None:
    """pins_existing"""
    store, _, _ = make_store(tmp_path, max_attempts=3)
    with pytest.raises(ValueError, match="different work"):
        store.create("review", kind="single", recipients=[T1], message=MESSAGE)
    again, created = store.create(
        "review",
        kind="single",
        recipients=[T1],
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    assert not created
    assert again["max_attempts"] == 3


# ---------------------------------------------------------------- found right, pinned


def test_two_keys_cannot_share_one_task_and_binding_cannot_be_replaced(tmp_path: Path) -> None:
    """pins_existing"""
    store, receipts, _ = make_store(tmp_path)
    store.record_raised("review", "refused")
    store.plan_attempt("review", "0", 2, T2)
    receipts.add("review.a2", "failed", target="hc:default:t2", id="same-id")
    store.record_submitted("review.a2", "same-id")
    receipts.add("review", "failed", target="hc:default:t1", id="same-id")
    with pytest.raises(sqlite3.IntegrityError):
        store.get("review")
    receipts.rows["review"]["id"] = "different-id"
    with pytest.raises(ValueError):
        store.record_submitted("review.a2", "different-id")


def test_derived_key_collision_rolls_back_whole_assignment(tmp_path: Path) -> None:
    """pins_existing"""
    store, receipts, _ = make_store(tmp_path)
    store.create("review.a2", kind="single", recipients=[T2], message=MESSAGE)
    receipts.add("review", "failed", target="hc:default:t1")
    with pytest.raises(ValueError, match="member key"):
        store.plan_attempt("review", "0", 2, T3)
    with closing(sqlite3.connect(store.path)) as db:
        count = "SELECT count(*) FROM assignment_members WHERE "
        assert db.execute(count + "assignment_id='review'").fetchone()[0] == 1
        assert db.execute(count + "member_key='review.a2'").fetchone()[0] == 1


def test_fresh_import_has_no_heavy_modules() -> None:
    """pins_existing"""
    code = (
        "import sys\n"
        "import answerback_bridge.assignments\n"
        "heavy = {'answerback_bridge.service', 'answerback_bridge.workers',"
        " 'answerback_bridge.native_workers', 'answerback_bridge.runtime',"
        " 'answerback_bridge.mcp_server', 'mcp', 'nats', 'acp'}\n"
        "assert not heavy.intersection(sys.modules), sorted(heavy.intersection(sys.modules))\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------- separate processes


def seed_real(tmp_path: Path, *, bind: bool = True):
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, view = make_store(tmp_path, tasks)
    task, _ = tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
    tasks.update(task["id"], "failed")
    tasks.metadata(task["id"], {"delivery": "not_attempted"})
    if bind:
        store.record_submitted("review", task["id"])
    return tasks, store, view


def process_call(root: str, action: str, peer: str, barrier: Any, output: Any) -> None:
    try:
        tasks = TaskStore(Path(root) / "tasks.sqlite")
        store = AssignmentStore(Path(root) / "assignments.sqlite", tasks)
        barrier.wait(timeout=15)
        if action == "plan":
            value: Any = store.plan_attempt(
                "review", "0", 2, Recipient(peer, "hc:default:" + peer[3:], "hc", True)
            )
        elif action == "plan_slot":
            import hashlib

            slot = hashlib.sha256(peer.encode()).hexdigest()[:16]
            name = "hc:replacement" + peer[-1]
            value = store.plan_attempt(
                "review", slot, 2, Recipient(name, "hc:default:" + name[3:], "hc", True)
            )
        else:
            value = store.create(
                "review",
                kind="single",
                recipients=[T1],
                message=MESSAGE,
                failover="never_started",
            )
        output.put(("ok", value))
    except Exception as error:  # reported to the parent, which asserts on it
        output.put((type(error).__name__, str(error)))


def join_owned(processes: list[Any]) -> None:
    for process in processes:
        process.join(20)
    alive = [process for process in processes if process.is_alive()]
    for process in alive:
        process.kill()
        process.join(5)
    assert not alive, "Owned test process did not finish"
    assert all(process.exitcode == 0 for process in processes)


@pytest.mark.parametrize("action", ["create", "same_attempt", "different_attempt"])
def test_two_processes_serialize_writes(tmp_path: Path, action: str) -> None:
    """pins_existing"""
    if action != "create":
        seed_real(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    peers = ["hc:t2", "hc:t3" if action == "different_attempt" else "hc:t2"]
    call = "create" if action == "create" else "plan"
    processes = [
        ctx.Process(target=process_call, args=(str(tmp_path), call, peer, barrier, output))
        for peer in peers
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    join_owned(processes)
    successes = [value for state, value in results if state == "ok"]
    assert sum(created for _, created in successes) == 1
    if action == "different_attempt":
        assert len(successes) == 1
        assert [state for state, _ in results].count("ValueError") == 1
    else:
        assert len(successes) == 2
    with closing(sqlite3.connect(tmp_path / "assignments.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM assignments").fetchone()[0] == 1
        members = db.execute("SELECT count(*) FROM assignment_members").fetchone()[0]
        assert members == (1 if action == "create" else 2)


def test_two_processes_keep_both_distinct_slot_updates(tmp_path: Path) -> None:
    """pins_existing"""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store = AssignmentStore(tmp_path / "assignments.sqlite", tasks)
    view, _ = store.create(
        "review", kind="fanout", recipients=[T1, T2], message=MESSAGE, failover="never_started"
    )
    for slot in view["slots"]:
        store.record_raised(slot["attempts"][0]["member_key"], "not reserved")
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=process_call, args=(str(tmp_path), "plan_slot", peer, barrier, output))
        for peer in ("hc:t1", "hc:t2")
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    join_owned(processes)
    assert all(state == "ok" and value[1] is True for state, value in results)
    view = store.get("review")
    attempts = [[a["attempt"] for a in slot["attempts"]] for slot in view["slots"]]
    assert attempts == [[1, 2], [1, 2]]


class _Snapshot:
    """Reads one preferred attempt as completed and the others as running."""

    def __init__(self, tasks: TaskStore, preferred: str, barrier: Any) -> None:
        self.tasks, self.preferred, self.barrier, self.seen = tasks, preferred, barrier, 0

    def by_request(self, key: str) -> dict[str, Any] | None:
        row = self.tasks.by_request(key)
        if row is not None and key != self.preferred:
            row["status"] = "running"
        self.seen += 1
        if self.seen == 2:
            self.barrier.wait(timeout=15)
        return row

    def get(self, task_id: str) -> dict[str, Any]:
        return self.tasks.get(task_id)


def winner_reader(root: str, preferred: str, barrier: Any, output: Any) -> None:
    try:
        tasks = TaskStore(Path(root) / "tasks.sqlite")
        view = AssignmentStore(
            Path(root) / "assignments.sqlite", _Snapshot(tasks, preferred, barrier)
        ).get("review")
        output.put(("ok", view["slots"][0]["winner"]))
    except Exception as error:  # reported to the parent, which asserts on it
        output.put((type(error).__name__, str(error)))


def test_two_processes_compare_and_set_one_winner(tmp_path: Path) -> None:
    """pins_existing"""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks)
    store.record_raised("review", "not reserved")
    store.plan_attempt("review", "0", 2, T2)
    for key, target in [("review", "hc:default:t1"), ("review.a2", "hc:default:t2")]:
        task, _ = tasks.reserve(target, MESSAGE, key, {"kind": "hc"})
        tasks.update(task["id"], "completed", result="done")
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=winner_reader, args=(str(tmp_path), key, barrier, output))
        for key in ("review", "review.a2")
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    join_owned(processes)
    assert all(state == "ok" for state, _ in results), results
    assert len({winner for _, winner in results}) == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert (
            db.execute("SELECT count(*) FROM assignment_members WHERE winner=1").fetchone()[0] == 1
        )


def plan_after_signal(root: str, signal: Any, done: Any, output: Any) -> None:
    try:
        tasks = TaskStore(Path(root) / "tasks.sqlite")
        store = AssignmentStore(Path(root) / "assignments.sqlite", tasks)
        assert signal.wait(15)
        output.put(("ok", store.plan_attempt("review", "0", 2, T2)[1]))
    except Exception as error:  # reported to the parent, which asserts on it
        output.put((type(error).__name__, str(error)))
    finally:
        done.set()


def test_get_tolerates_a_new_attempt_from_another_process(tmp_path: Path) -> None:
    """red_at_pretip: finding 4. Process B commits attempt 2 while process A reads
    receipts; A starts its read again and returns one coherent view with both attempts."""
    tasks, _, _ = seed_real(tmp_path, bind=False)
    ctx = multiprocessing.get_context("spawn")
    signal, done, output = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=plan_after_signal, args=(str(tmp_path), signal, done, output))
    process.start()

    class Interleaved:
        def by_request(self, key: str) -> dict[str, Any] | None:
            snapshot = tasks.by_request(key)
            signal.set()
            assert done.wait(15)
            return snapshot

        def get(self, task_id: str) -> dict[str, Any]:
            return tasks.get(task_id)

    try:
        store = AssignmentStore(tmp_path / "assignments.sqlite", Interleaved())
        view = store.get("review")
        assert [a["attempt"] for a in view["slots"][0]["attempts"]] == [1, 2]
        assert view["status"] == "admitting"
    finally:
        join_owned([process])
        assert output.get(timeout=5) == ("ok", True)


def hold_database(path: str, acquired: Any, release: Any) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        acquired.set()
        assert release.wait(20)
        db.rollback()


def test_database_locked_is_bounded_and_retry_preserves_attempt_number(tmp_path: Path) -> None:
    """pins_existing: a held lock raises after the 10 s timeout and writes nothing."""
    _, store, _ = seed_real(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold_database, args=(str(store.path), acquired, release))
    process.start()
    try:
        assert acquired.wait(10)
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.plan_attempt("review", "0", 2, T2)
        assert 9 <= time.monotonic() - start < 15
    finally:
        release.set()
        join_owned([process])
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT count(*) FROM assignment_members").fetchone()[0] == 1
    attempt, created = store.plan_attempt("review", "0", 2, T2)
    assert created and attempt["attempt"] == 2


def crash_writer(root: str, reserve: bool, ready: Any) -> None:
    tasks = TaskStore(Path(root) / "tasks.sqlite")
    make_store(Path(root), tasks)
    if reserve:
        tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
    ready.send("durable")
    ready.recv()


@pytest.mark.parametrize("reserve", [False, True])
def test_killed_process_recovers_both_crash_windows(tmp_path: Path, reserve: bool) -> None:
    """pins_existing"""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=crash_writer, args=(str(tmp_path), reserve, child))
    process.start()
    assert parent.poll(15)
    assert parent.recv() == "durable"
    process.kill()
    process.join(10)
    assert not process.is_alive() and process.exitcode != 0
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    tasks.recover_pending()
    store = AssignmentStore(tmp_path / "assignments.sqlite", tasks)
    view, created = store.create(
        "review", kind="single", recipients=[T1], message=MESSAGE, failover="never_started"
    )
    assert not created
    assert view["status"] == ("needs_decision" if reserve else "admitting")
    assert first(view)["receipt_status"] == ("uncertain" if reserve else None)
    with pytest.raises(ValueError):
        store.plan_attempt("review", "0", 2, T2)
    if reserve:
        _, created = tasks.reserve("hc:default:t1", MESSAGE, "review", {"kind": "hc"})
        assert not created


# ================================================================ second review (fix round 2)
#
# Source: the reviewer's probe file ``test_fix_attacks.py`` (review ``F2-review2-codex.md``).
# Every test in this part is ``red_at_pretip`` against ``3a09bea`` unless it says otherwise:
# its unported probe failed there (``out/F2-fix2-red.log``).


class AttackReceipts:
    """The reviewer's fake: a receipt names the recipient whose adapter reserved it."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.error: Exception | None = None

    def add(
        self,
        key: str,
        *,
        recipient: Recipient = T1,
        message: str = MESSAGE,
        status: str = "uncertain",
    ) -> dict[str, Any]:
        row = {
            "id": f"task-{key}",
            "request_key": key,
            "target": recipient.target,
            "metadata": {"kind": recipient.provider},
            "message": message,
            "status": status,
        }
        self.rows[key] = row
        return row

    def by_request(self, key: str) -> dict[str, Any] | None:
        if self.error is not None:
            raise self.error
        return self.rows.get(key)

    def get(self, task_id: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        for row in self.rows.values():
            if row.get("id") == task_id:
                return row
        raise ValueError("Unknown task ID")


def attack_store(tmp_path: Path, receipts: Any = None, recipient: Recipient = T1, cap: int = 3):
    receipts = AttackReceipts() if receipts is None else receipts
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts)
    store.create(
        "a",
        kind="single",
        recipients=[recipient],
        message=MESSAGE,
        failover="never_started",
        max_attempts=cap,
    )
    return store, receipts


def latest(view: dict[str, Any]) -> dict[str, Any]:
    return view["slots"][0]["attempts"][-1]


# ---------------------------------------------------------------- review 2, finding 1


def test_new_third_attempt_checks_completion_of_earlier_attempt(tmp_path: Path) -> None:
    """red_at_pretip: an earlier attempt completed but no `get` has stored the winner yet;
    planning attempt 3 must see it and refuse."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _ = attack_store(tmp_path, tasks)
    store.record_raised("a", "not reserved")
    store.plan_attempt("a", "0", 2, T2)
    child, _ = tasks.reserve(T2.target, MESSAGE, "a.a2", {"kind": "hc"})
    tasks.update(child["id"], "failed")
    parent, _ = tasks.reserve(T1.target, MESSAGE, "a", {"kind": "hc"})
    tasks.update(parent["id"], "completed", result="late result")
    with pytest.raises(ValueError, match="satisfied"):
        store.plan_attempt("a", "0", 3, T3)
    assert [a["attempt"] for a in store.get("a")["slots"][0]["attempts"]] == [1, 2]


@pytest.mark.parametrize("seen_by_get", [True, False])
def test_replan_not_sent_successor_after_earlier_attempt_wins_is_refused(
    tmp_path: Path, seen_by_get: bool
) -> None:
    """red_at_pretip: re-planning a `not_sent` attempt in a won slot is refused, whether
    the winner is stored (`get` ran) or not yet."""
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    store.record_raised("a.a2", "refused")
    receipts.add("a", status="completed")
    if seen_by_get:
        assert store.get("a")["status"] == "needs_decision"  # c2: winner's Core unknown
    with pytest.raises(ValueError, match="satisfied"):
        store.plan_attempt("a", "0", 2, T2)
    assert latest(store.get("a"))["admission"] == "not_sent"


def test_a_bound_attempt_in_a_won_slot_is_still_returned(tmp_path: Path) -> None:
    """pins_existing: the idempotent repeat of a bound attempt is kept after a win."""
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    row = receipts.add("a.a2", recipient=T2, status="running")
    store.record_submitted("a.a2", row["id"])
    receipts.add("a", status="completed")
    live = {"task_id": row["id"], "core_blocked": False, "stalled": False}  # R4-1
    assert store.get("a", assessment={"a.a2": live})["status"] == "needs_decision"  # c2
    item, created = store.plan_attempt("a", "0", 2, T2)
    assert not created
    assert item["task_id"] == row["id"] and item["admission"] == "submitted"


# ---------------------------------------------------------------- review 2, finding 2


@pytest.mark.parametrize("damage", ["uncertain", "wrong_target", "missing_provider", "io_error"])
def test_planned_replay_refreshes_its_key(tmp_path: Path, damage: str) -> None:
    """red_at_pretip: the repeat of an unbound `planned` attempt looks its key up."""
    store, receipts = attack_store(tmp_path)
    row = receipts.add("a")
    if damage == "wrong_target":
        row["target"] = T3.target
    elif damage == "missing_provider":
        row["metadata"].clear()
    elif damage == "io_error":
        receipts.error = OSError("unreadable")
    if damage == "uncertain":
        item, created = store.plan_attempt("a", "0", 1, T1)
        assert not created
        assert item["admission"] == "submitted" and item["task_id"] == row["id"]
        assert item["lookup"] == "valid"
        return
    with pytest.raises(ValueError, match="cannot be planned again"):
        store.plan_attempt("a", "0", 1, T1)
    found = store.locate(member_key="a")
    assert found is not None and found["task_id"] is None


@pytest.mark.parametrize("damage", ["uncertain", "wrong_target", "unreadable"])
def test_new_successor_checks_preoccupied_derived_key(tmp_path: Path, damage: str) -> None:
    """red_at_pretip: a new successor looks its own key up before it is returned."""
    store, receipts = attack_store(tmp_path)
    receipts.add("a", status="failed")
    row = receipts.add("a.a2", recipient=T2)
    if damage == "wrong_target":
        row["target"] = T3.target
    elif damage == "unreadable":
        row.pop("message")
    if damage == "uncertain":
        item, created = store.plan_attempt("a", "0", 2, T2)
        assert created
        assert item["admission"] == "uncertain" and item["task_id"] == row["id"]
        return
    with pytest.raises(ValueError, match="cannot be planned"):
        store.plan_attempt("a", "0", 2, T2)
    assert len(store.get("a")["slots"][0]["attempts"]) == 1


# ---------------------------------------------------------------- review 2, finding 3


def test_an_unmarked_foreign_file_is_refused_unchanged(tmp_path: Path) -> None:
    """red_at_pretip (review 2): a foreign file with a `sqliteX…` table is never
    restamped. Split in fix round 3 (N7) from a test that also added the extra objects
    below on top of this table, so that the table alone caused every refusal."""
    path = tmp_path / "assignments.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sqliteXhost (payload TEXT)")
        db.execute("INSERT INTO sqliteXhost VALUES ('foreign data')")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="not an assignment store"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


def test_a_reserved_sqlite_name_cannot_be_used_for_an_extra_object(tmp_path: Path) -> None:
    """pins_existing: SQLite itself refuses user objects named `sqlite_…`."""
    path = tmp_path / "assignments.sqlite"
    AssignmentStore(path, AttackReceipts())
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(sqlite3.OperationalError, match="reserved"):
            db.execute("CREATE TABLE sqlite_foreign (payload TEXT)")


def _extend_marked_store(path: Path, statement: str) -> bytes:
    """Add exactly one object to an otherwise exact, marked store; return its bytes."""
    AssignmentStore(path, AttackReceipts())
    with closing(sqlite3.connect(path)) as db:
        db.execute(statement)
        db.commit()
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema")}
    assert {"assignments", "assignment_members"} <= names
    return path.read_bytes()


def test_one_extra_table_on_an_exact_store_is_refused(tmp_path: Path) -> None:
    """pins_existing at 6a7b4d0 (red under a mutation that ignores tables): N7."""
    path = tmp_path / "assignments.sqlite"
    before = _extend_marked_store(path, "CREATE TABLE sqliteXforeign (payload TEXT)")
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


def test_one_extra_index_on_an_exact_store_is_refused(tmp_path: Path) -> None:
    """pins_existing at 6a7b4d0 (red under a mutation that ignores indexes): N7. The index
    is on an F2 table, so no extra table exists."""
    path = tmp_path / "assignments.sqlite"
    before = _extend_marked_store(path, "CREATE INDEX sqliteXidx ON assignments(created)")
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


def test_one_extra_trigger_on_an_exact_store_is_refused(tmp_path: Path) -> None:
    """pins_existing at 6a7b4d0 (red under a mutation that ignores triggers): N7."""
    path = tmp_path / "assignments.sqlite"
    before = _extend_marked_store(
        path, "CREATE TRIGGER sqliteXtrg AFTER INSERT ON assignments BEGIN SELECT 1; END"
    )
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


def test_one_extra_view_on_an_exact_store_is_refused(tmp_path: Path) -> None:
    """pins_existing at 6a7b4d0 (red under a mutation that ignores views): N7."""
    path = tmp_path / "assignments.sqlite"
    before = _extend_marked_store(
        path, "CREATE VIEW sqliteXview AS SELECT assignment_id FROM assignments"
    )
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


def test_marked_store_with_only_a_sqlitex_object_is_refused(tmp_path: Path) -> None:
    """red_at_pretip: the reviewer's exact case on a marked store."""
    path = tmp_path / "assignments.sqlite"
    AssignmentStore(path, AttackReceipts())
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sqliteXforeign (payload TEXT)")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(path, AttackReceipts())
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [0, 2, 100])
def test_marked_other_version_is_refused_byte_for_byte(tmp_path: Path, version: int) -> None:
    """pins_existing"""
    store, receipts = attack_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.commit()
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="schema version"):
        AssignmentStore(store.path, receipts)
    assert store.path.read_bytes() == before


# ---------------------------------------------------------------- review 2, findings 4 and 5


@pytest.mark.parametrize(
    "status", ["pending", "queued", "submitted", "running", "blocked", "uncertain"]
)
def test_unfinished_summary_includes_all_nonterminal_receipts(tmp_path: Path, status: str) -> None:
    """red_at_pretip for `uncertain`; the other states pins_existing."""
    store, receipts = attack_store(tmp_path)
    row = receipts.add("a", status=status)
    view = store.get("a")
    assert any(item["task_id"] == row["id"] for item in view["unfinished_attempts"])


@pytest.mark.parametrize("damage", ["unreadable", "remote_miss"])
def test_unfinished_summary_never_reads_an_unknown_end_as_finished(
    tmp_path: Path, damage: str
) -> None:
    """red_at_pretip: an unreadable receipt or a remote miss after a raise may still run."""
    remote = Recipient("node:other-host/hc:t1", "node:other-host/hc:t1", "node", False)
    store, receipts = attack_store(tmp_path, recipient=T1 if damage == "unreadable" else remote)
    if damage == "unreadable":
        receipts.add("a", status="running")
        store.record_raised("a", "lost reply")
        receipts.error = OSError("io")
    else:
        store.record_raised("a", "lost reply")
    view = store.get("a")
    assert [item["attempt"] for item in view["unfinished_attempts"]] == [1]


def test_invalidated_flag_on_recompleted_receipt_is_not_success(tmp_path: Path) -> None:
    """red_at_pretip: `completed` with `identity_invalidated` is no valid completion."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _ = attack_store(tmp_path, tasks)
    task, _ = tasks.reserve(T1.target, MESSAGE, "a", {"kind": "hc"})
    tasks.update(task["id"], "completed", result="first")
    assert store.get("a")["status"] == "satisfied"
    tasks.invalidate_submission(task["id"], "wrong delivery")
    tasks.update(task["id"], "completed", result="late unchecked update")
    view = store.get("a")
    assert latest(view)["invalidated"] is True
    assert view["status"] == "needs_decision"
    assert view["slots"][0]["late_results"] == []


def test_invalidated_completion_never_becomes_a_winner(tmp_path: Path) -> None:
    """red_at_pretip: no winner is written for an invalidated completion."""
    store, receipts = attack_store(tmp_path)
    row = receipts.add("a", status="completed")
    row["metadata"]["identity_invalidated"] = True
    view = store.get("a")
    assert view["slots"][0]["winner"] is None
    assert view["status"] == "needs_decision"


# ================================================================ third review (fix round 3)
#
# Source: the reviewer's probe file ``test_round3.py`` (review ``F2-review3-codex.md``, input
# ``probes-r3.txt``). Each test here is ``red_at_pretip`` against ``82a4b70`` unless it says
# otherwise (``out/F2-fix3-red.log``).


class R3Receipts:
    """The reviewer's round-3 fake: copies on read, per-key read errors, a read hook."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.after: Any = None

    def add(self, key: str, recipient: Recipient = T1, status: str = "failed") -> dict[str, Any]:
        self.rows[key] = {
            "id": f"id-{key}",
            "request_key": key,
            "target": recipient.target,
            "metadata": {"kind": recipient.provider},
            "status": status,
            "message": MESSAGE,
        }
        return self.rows[key]

    def by_request(self, key: str) -> dict[str, Any] | None:
        self.calls.append(key)
        if key in self.errors:
            raise self.errors[key]
        row = self.rows.get(key)
        row = None if row is None else {**row, "metadata": dict(row["metadata"])}
        if self.after is not None:
            self.after(key)
        return row

    def get(self, task_id: str) -> dict[str, Any]:
        key = next((k for k, row in self.rows.items() if row["id"] == task_id), None)
        if key is None:
            raise ValueError("missing receipt")
        row = self.by_request(key)
        assert row is not None
        return row


class R3Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def r3_store(tmp_path: Path, *, deadline: float | None = None, receipts: Any = None):
    receipts = R3Receipts() if receipts is None else receipts
    clock = R3Clock()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts, clock=clock)
    store.create(
        "a",
        kind="single",
        recipients=[T1],
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
        deadline=deadline,
    )
    return store, receipts, clock


# ---------------------------------------------------------------- N1


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("operation", ["planned", "not_sent", "third"])
@pytest.mark.parametrize("damage", ["conflicting", "unavailable", "uncertain", "blocked"])
def test_bad_older_receipt_cannot_leave_new_work_sendable(
    tmp_path: Path, bound: bool, operation: str, damage: str
) -> None:
    """red_at_pretip: N1. A conflicting, unreadable, uncertain or blocked receipt of an
    older attempt vetoes the re-planning of a later attempt and a new successor."""
    store, receipts, _ = r3_store(tmp_path)
    first = receipts.add("a")
    if bound:
        store.record_submitted("a", first["id"])
    store.plan_attempt("a", "0", 2, T2)
    if operation == "not_sent":
        store.record_raised("a.a2", "local refusal")
    elif operation == "third":
        second = receipts.add("a.a2", T2)
        store.record_submitted("a.a2", second["id"])
    if damage == "conflicting":
        first["target"] = "hc:default:other"
    elif damage == "unavailable":
        receipts.errors["a"] = OSError("older receipt cannot be read")
    else:
        first["status"] = damage
    third = operation == "third"
    with pytest.raises(ValueError, match="earlier attempt is unresolved"):
        store.plan_attempt("a", "0", 3 if third else 2, T3 if third else T2)
    receipts.errors.clear()
    assert len(store.get("a")["slots"][0]["attempts"]) == 2


# ---------------------------------------------------------------- N2


@pytest.mark.parametrize("fact", ["core_blocked", "stalled"])
@pytest.mark.parametrize("mode", ["missing", "partial", "stale_id"])
def test_missing_or_stale_live_fact_is_not_active(tmp_path: Path, fact: str, mode: str) -> None:
    """red_at_pretip: N2."""
    store, receipts, _ = r3_store(tmp_path)
    row = receipts.add("a", status="running")
    store.record_submitted("a", row["id"])
    supplied = store.get("a", assessment={"a": {"task_id": row["id"], fact: True}})
    assert supplied["status"] == ("blocked" if fact == "core_blocked" else "needs_decision")
    values: dict[str, Any] = {} if mode == "missing" else {"a": {"task_id": row["id"]}}
    if mode == "stale_id":
        values["a"].update(task_id="old-receipt", **{fact: True})
    assert store.get("a", assessment=values)["status"] == "needs_decision"


def test_missing_core_fact_cannot_hide_permission_behind_quorum(tmp_path: Path) -> None:
    """red_at_pretip: N2."""
    receipts = R3Receipts()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts)
    view, _ = store.create("g", kind="fanout", recipients=[T1, T2], message=MESSAGE, quorum=1)
    key1, key2 = [slot["attempts"][0]["member_key"] for slot in view["slots"]]
    receipts.add(key1, status="completed")
    second = receipts.add(key2, T2, "submitted")
    known = store.get("g", assessment={key2: {"task_id": second["id"], "core_blocked": True}})
    assert known["status"] == "blocked"
    assert store.get("g")["status"] == "needs_decision"


@pytest.mark.parametrize("fact", ["core_blocked", "stalled"])
def test_true_live_fact_is_not_ignored_on_failed_auto_receipt(tmp_path: Path, fact: str) -> None:
    """red_at_pretip: N2, a contradictory fact is named. Changed in fix round 4 (R4-1): a
    supplied Core block reads blocked even when it contradicts the receipt."""
    store, receipts, _ = r3_store(tmp_path)
    row = receipts.add("a")
    given = {"task_id": row["id"], fact: True, "verdict": "auto", "no_candidate": False}
    view = store.get("a", assessment={"a": given})
    assert view["status"] == ("blocked" if fact == "core_blocked" else "needs_decision")
    assert view["slots"][0]["retry_eligible"] is False
    assert view["slots"][0]["assessment_contradictions"] == [fact]


@pytest.mark.parametrize("older", [False, True])
def test_rewrite_current_block_after_public_abandonment(tmp_path: Path, older: bool) -> None:
    """red_at_pretip (older); pins_existing (latest): review6 public-store reproduction."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks, max_attempts=3)
    if older:
        store.record_raised("review", "local refusal")
        store.plan_attempt("review", "0", 2, T2)
    first_task, _ = tasks.reserve(T1.target, MESSAGE, "review", {"kind": "hc"})
    tasks.update(first_task["id"], "blocked")
    store.record_raised("review", "lost adapter reply")
    tasks.abandon(tasks.get(first_task["id"]), "release reservation; terminal still waits")
    if older:
        replacement, _ = tasks.reserve(T2.target, MESSAGE, "review.a2", {"kind": "hc"})
        tasks.update(replacement["id"], "completed", result="replacement result")
    view = store.get(
        "review", assessment={"review": {"task_id": first_task["id"], "core_blocked": True}}
    )
    assert view["status"] == "blocked"
    assert view["slots"][0]["assessment_stale_attempts"] == []


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("final", ["completed", "failed", "cancelled", "abandoned"])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("core", [None, False, True])
def test_rewrite_final_receipts_at_every_position(
    tmp_path: Path, position: int, final: str, uncertain: bool, core: bool | None
) -> None:
    """red_at_pretip: c1..c4; certain ended false/missing controls pin existing behavior.

    Build three attempts from real failed receipts, then change the selected fake
    receipt to the profile. Admission uncertainty is written by record_raised.
    """
    store, receipts, _ = make_store(tmp_path, max_attempts=3)
    members = ("review", "review.a2", "review.a3")
    tasks = []
    for n, (key, peer) in enumerate(zip(members, (T1, T2, T3), strict=True), 1):
        if n > 1:
            store.plan_attempt("review", "0", n, peer)
        task = receipts.add(key, "failed", target=peer.target)
        store.record_submitted(key, task["id"])
        tasks.append(task)
    tasks[position]["status"] = final
    if uncertain:
        store.record_raised(members[position], "adapter reply lost")
    # A different completion isolates Q1 from latest execution state.
    tasks[0 if position == 2 else 2]["status"] = "completed"
    assessment = {
        key: {"task_id": task["id"], "core_blocked": False, "stalled": False}
        for key, task in zip(members, tasks, strict=True)
    }
    if core is None:
        assessment[members[position]].pop("core_blocked")
    else:
        assessment[members[position]]["core_blocked"] = core
    view = store.get("review", assessment=assessment)
    may_hold = uncertain or final == "abandoned"
    expected = (
        "blocked"
        if core is True
        else "needs_decision"
        if core is None and may_hold
        else "satisfied"
    )
    assert view["status"] == expected
    assert [a["attempt"] for a in view["unfinished_attempts"]] == (
        [position + 1] if may_hold else []
    )
    slot = view["slots"][0]
    entry = {"attempt": position + 1, "member_key": members[position], "facts": ["core_blocked"]}
    assert slot["assessment_absent_attempts"] == ([entry] if core is None and may_hold else [])
    assert slot["assessment_contradiction_attempts"] == (
        [entry] if core is True and not may_hold else []
    )
    assert slot["assessment_contradictions"] == (
        ["core_blocked"] if position == 2 and core is True and not may_hold else []
    )


@pytest.mark.parametrize("path", ["metadata_after_completion", "update_after_invalidation"])
def test_rewrite_invalidated_completion_can_be_produced_by_task_store(
    tmp_path: Path, path: str
) -> None:
    """red_at_pretip: public TaskStore can retain invalidation on a completed receipt."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks)
    task, _ = tasks.reserve(T1.target, MESSAGE, "review", {"kind": "hc"})
    if path == "update_after_invalidation":
        tasks.invalidate_submission(task["id"], "identity mismatch")
    tasks.update(task["id"], "completed", result="result")
    if path == "metadata_after_completion":
        tasks.metadata(task["id"], {"identity_invalidated": True})
    receipt = tasks.get(task["id"])
    assert receipt["status"] == "completed"
    assert receipt["metadata"]["identity_invalidated"] is True
    view = store.get("review")
    assert view["slots"][0]["winner"] is None
    assert view["slots"][0]["blocked_unknown"] is True
    assert [a["attempt"] for a in view["unfinished_attempts"]] == [1]


@pytest.mark.parametrize("admission", ["planned", "not_sent"])
def test_rewrite_unreserved_history_true_core_is_a_contradiction(
    tmp_path: Path, admission: str
) -> None:
    """red_at_pretip: c3 for class none; profiles come from the public attempt view."""
    from answerback_bridge.assignments import _slot_status

    store, _, view = make_store(tmp_path)
    if admission == "not_sent":
        store.record_raised("review", "before reservation")
        view = store.get("review")
    earlier = first(view)
    latest_item = dict(
        earlier,
        attempt=2,
        member_key="review.a2",
        admission="submitted",
        task_id="second",
        lookup="valid",
        receipt_status="completed",
    )
    reading = _slot_status([earlier, latest_item], 3, False, [{"core_blocked": True}, {}])
    assert reading.status == "blocked"
    assert list(reading.contradiction_attempts) == [
        {"attempt": 1, "member_key": "review", "facts": ["core_blocked"]}
    ]
    assert reading.contradictions == []


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("observation", ["missing", "stale_false", "stale_true"])
def test_rewrite_diagnostics_keep_each_unknown_beside_a_known_block(
    tmp_path: Path, position: int, observation: str
) -> None:
    """red_at_pretip: R6-2, stale/missing Core attributed even when a known block wins."""
    store, receipts, _ = make_store(tmp_path, max_attempts=3)
    keys = ("review", "review.a2", "review.a3")
    tasks = []
    for n, (key, peer) in enumerate(zip(keys, (T1, T2, T3), strict=True), 1):
        if n > 1:
            store.plan_attempt("review", "0", n, peer)
        task = receipts.add(key, "failed", target=peer.target)
        store.record_submitted(key, task["id"])
        tasks.append(task)
    tasks[position]["status"] = "running"
    tasks[2]["status"] = "completed"
    assessment = {keys[1 - position]: {"task_id": tasks[1 - position]["id"], "core_blocked": True}}
    if observation != "missing":
        assessment[keys[position]] = {
            "task_id": "stale-id",
            "core_blocked": observation == "stale_true",
        }
    slot = store.get("review", assessment=assessment)["slots"][0]
    assert slot["status"] == "blocked"
    assert slot["blocked_unknown"] is False
    assert slot["assessment_absent"] == slot["assessment_contradictions"] == []
    assert slot["assessment_stale"] is False
    assert slot["assessment_stale_attempts"] == ([position + 1] if observation != "missing" else [])
    assert slot["assessment_absent_attempts"] == [
        {"attempt": position + 1, "member_key": keys[position], "facts": ["core_blocked"]}
    ]
    assert slot["assessment_contradiction_attempts"] == [
        {"attempt": 2 - position, "member_key": keys[1 - position], "facts": ["core_blocked"]}
    ]


# ---------------------------------------------------------------- N3


def test_auto_assessment_does_not_invent_a_running_successor(tmp_path: Path) -> None:
    """red_at_pretip: N3. An auto verdict with a candidate does not make the failed
    attempt active: nothing runs and nothing is planned until F6 plans attempt 2."""
    store, receipts, _ = r3_store(tmp_path)
    row = receipts.add("a")
    given = {"task_id": row["id"], "verdict": "auto", "no_candidate": False}
    view = store.get("a", assessment={"a": given})
    assert len(view["slots"][0]["attempts"]) == 1
    assert view["status"] == "needs_decision"
    assert view["counts"]["active"] == 0 and view["unfinished_attempts"] == []
    assert view["slots"][0]["retry_eligible"] is True
    attempt, created = store.plan_attempt("a", "0", 2, T2, verdict="auto")
    assert created and attempt["admission"] == "planned"
    assert store.get("a")["status"] == "admitting"


@pytest.mark.parametrize(
    "given",
    [
        {"verdict": "auto", "no_candidate": False, "core_blocked": True},
        {"verdict": "auto", "no_candidate": True},
        {"verdict": "auto"},
    ],
)
def test_retry_eligibility_needs_auto_a_candidate_and_no_contradiction(
    tmp_path: Path, given: dict[str, Any]
) -> None:
    """red_at_pretip: N3, `retry_eligible` is never set by a partial or contradictory
    assessment."""
    store, receipts, _ = r3_store(tmp_path)
    row = receipts.add("a")
    view = store.get("a", assessment={"a": {"task_id": row["id"], **given}})
    assert view["slots"][0]["retry_eligible"] is False


# ---------------------------------------------------------------- N4


class SteppingClock:
    """Returns each value in turn and then the last one: time passes between calls."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_new_successor_rechecks_deadline_after_own_receipt_lookup(tmp_path: Path) -> None:
    """red_at_pretip: N4. The clock passes the deadline during the own-key lookup."""
    store, receipts, clock = r3_store(tmp_path, deadline=2000)
    receipts.add("a")
    clock.now = 1999

    def advance(key: str) -> None:
        if key == "a.a2":
            clock.now = 2001

    receipts.after = advance
    with pytest.raises(ValueError, match="deadline"):
        store.plan_attempt("a", "0", 2, T2)
    receipts.after = None
    assert len(store.get("a")["slots"][0]["attempts"]) == 1


def test_replanned_successor_rechecks_deadline_at_the_write(tmp_path: Path) -> None:
    """pins_existing: N4 for re-planning a `not_sent` successor; the read of the stored
    clock already followed the lookups at 82a4b70, and the write-time check keeps it."""
    store, receipts, clock = r3_store(tmp_path, deadline=2000)
    receipts.add("a")
    store.plan_attempt("a", "0", 2, T2)
    store.record_raised("a.a2", "local refusal")
    clock.now = 1999

    def advance(key: str) -> None:
        if key == "a.a2":
            clock.now = 2001

    receipts.after = advance
    with pytest.raises(ValueError, match="deadline"):
        store.plan_attempt("a", "0", 2, T2)
    receipts.after = None
    assert store.get("a")["slots"][0]["attempts"][-1]["admission"] == "not_sent"


def test_new_assignment_checks_its_deadline_after_taking_the_write_lock(tmp_path: Path) -> None:
    """red_at_pretip: N4. The first clock reading is before the lock, the next one after
    it (time spent waiting for the lock). Exact: no thread, no sleep."""
    clock = SteppingClock(1000.0, 2000.0)
    store = AssignmentStore(tmp_path / "assignments.sqlite", R3Receipts(), clock=clock)
    with pytest.raises(ValueError, match="already passed"):
        store.create("a", kind="single", recipients=[T1], message=MESSAGE, deadline=1500)
    with pytest.raises(ValueError, match="Unknown assignment"):
        store.get("a")


def test_new_assignment_stores_the_time_taken_under_the_lock(tmp_path: Path) -> None:
    """red_at_pretip: N4, the stored creation time is the checked one."""
    clock = SteppingClock(1000.0, 1200.0)
    store = AssignmentStore(tmp_path / "assignments.sqlite", R3Receipts(), clock=clock)
    view, created = store.create(
        "a", kind="single", recipients=[T1], message=MESSAGE, deadline=1500
    )
    assert created and view["created"] == 1200.0


def test_new_assignment_checks_deadline_after_waiting_for_writer_lock(tmp_path: Path) -> None:
    """red_at_pretip: N4, the reviewer's probe with a real SQLite writer lock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    called = threading.Event()

    class SignalledClock(R3Clock):
        def __call__(self) -> float:
            called.set()
            return self.now

    clock = SignalledClock()
    store = AssignmentStore(tmp_path / "assignments.sqlite", R3Receipts(), clock=clock)
    with closing(sqlite3.connect(store.path)) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        future = pool.submit(
            store.create, "a", kind="single", recipients=[T1], message=MESSAGE, deadline=1500
        )
        try:
            assert called.wait(5)
            clock.now = 2000
        finally:
            blocker.rollback()
        with pytest.raises(ValueError, match="already passed"):
            future.result(timeout=15)


def test_an_identical_create_replay_after_the_deadline_returns_the_assignment(
    tmp_path: Path,
) -> None:
    """pins_existing: N4's earlier refusal applies to a new ID only."""
    store, _, clock = r3_store(tmp_path, deadline=2000)
    clock.now = 2500
    view, created = store.create(
        "a",
        kind="single",
        recipients=[T1],
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
        deadline=2000,
    )
    assert not created and view["deadline_passed"] is True


# ---------------------------------------------------------------- N5


@pytest.mark.parametrize("damage", ["won", "conflicting", "unavailable", "expired", "none"])
def test_a_read_view_never_says_sendable(tmp_path: Path, damage: str) -> None:
    """red_at_pretip: N5. A view's `admission` is a stored fact; `sendable` is false in
    every view, whatever the admission reads, and only the guarded planning call can
    return `sendable: true`."""
    store, receipts, clock = r3_store(tmp_path, deadline=2000)
    store.record_raised("a", "local refusal")
    planned, _ = store.plan_attempt("a", "0", 2, T2)
    assert planned["admission"] == "planned" and planned["sendable"] is True
    if damage == "won":
        receipts.add("a", status="completed")
    elif damage == "conflicting":
        receipts.add("a.a2", T3)
    elif damage == "unavailable":
        receipts.errors["a.a2"] = OSError("no read")
    elif damage == "expired":
        clock.now = 2000
    view = store.get("a")
    latest = view["slots"][0]["attempts"][-1]
    assert latest["admission"] == "planned"
    assert all(item["sendable"] is False for item in view["slots"][0]["attempts"])
    if damage == "none":
        again, created = store.plan_attempt("a", "0", 2, T2)
        assert not created and again["sendable"] is True
    else:
        with pytest.raises(ValueError):
            store.plan_attempt("a", "0", 2, T2)


def test_create_and_record_views_are_never_sendable(tmp_path: Path) -> None:
    """red_at_pretip: N5, including the reviewer's create/precheck race."""
    receipts = R3Receipts()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts)

    def after_precheck(key: str) -> None:
        receipts.after = None
        receipts.add(key, T3, "uncertain")

    receipts.after = after_precheck
    view, created = store.create("a", kind="single", recipients=[T1], message=MESSAGE)
    item = view["slots"][0]["attempts"][0]
    assert created and item["admission"] == "planned" and item["lookup"] == "conflicting"
    assert item["sendable"] is False
    with pytest.raises(ValueError, match="cannot be planned again"):
        store.plan_attempt("a", "0", 1, T1)
    assert store.record_raised("a", "ValueError")["sendable"] is False


def test_planning_returns_sendable_only_for_a_fresh_planned_attempt(tmp_path: Path) -> None:
    """red_at_pretip: N5. A bound or uncertain result of `plan_attempt` is not sendable."""
    store, receipts, _ = r3_store(tmp_path)
    first, created = store.plan_attempt("a", "0", 1, T1)
    assert not created and first["admission"] == "planned" and first["sendable"] is True
    row = receipts.add("a", status="running")
    bound, _ = store.plan_attempt("a", "0", 1, T1)
    assert bound["task_id"] == row["id"] and bound["sendable"] is False
    receipts.add("a.a2", T2, "uncertain")
    row["status"] = "failed"
    successor, created = store.plan_attempt("a", "0", 2, T2)
    assert created and successor["admission"] == "uncertain"
    assert successor["sendable"] is False


# ---------------------------------------------------------------- N6


def test_analyze_on_the_store_s_own_file_keeps_it_openable(tmp_path: Path) -> None:
    """red_at_pretip: N6. SQLite's statistics table is allowed by its exact name."""
    store, receipts, _ = r3_store(tmp_path)
    store.plan_attempt("a", "0", 1, T1)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("ANALYZE")
        db.commit()
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema")}
    assert "sqlite_stat1" in names
    reopened = AssignmentStore(store.path, receipts)
    assert reopened.get("a")["slots"][0]["attempts"][0]["admission"] == "planned"


def test_sqlite_sequence_is_allowed_by_its_exact_name(tmp_path: Path) -> None:
    """red_at_pretip: N6 (the prompt allows `sqlite_sequence`; the reviewer's probe
    `test_sqlite_sequence_is_refused_after_autoincrement_table_removed` characterized the
    old refusal and now fails on purpose)."""
    store, receipts, _ = r3_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("CREATE TABLE scratch_autoinc (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        db.execute("DROP TABLE scratch_autoinc")
        db.commit()
    AssignmentStore(store.path, receipts)


def test_statistics_do_not_hide_another_object(tmp_path: Path) -> None:
    """pins_existing: with `sqlite_stat1` present, an extra user table is still refused,
    byte for byte unchanged."""
    store, receipts, _ = r3_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("ANALYZE")
        db.execute("CREATE TABLE sqliteXstat1 (value TEXT)")
        db.commit()
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="schema does not match"):
        AssignmentStore(store.path, receipts)
    assert store.path.read_bytes() == before


def test_an_unmarked_file_with_only_statistics_is_not_empty(tmp_path: Path) -> None:
    """pins_existing: a foreign file is never restamped because it only holds statistics."""
    path = tmp_path / "assignments.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE host (value TEXT)")
        db.execute("CREATE INDEX host_value ON host(value)")
        db.execute("INSERT INTO host VALUES ('x')")
        db.execute("ANALYZE")
        db.execute("DROP TABLE host")
        db.commit()
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == [("sqlite_stat1",)]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="not an assignment store"):
        AssignmentStore(path, R3Receipts())
    assert path.read_bytes() == before


# ================================================================ fourth review (fix round 4)
#
# Source: the reviewer's ``test_round4.py`` (review ``F2-review4-codex.md``). Each test here
# is ``red_at_pretip`` against ``06a2923`` (``out/F2-fix4-red.log``).


def r4_group(tmp_path: Path, status: str = "running", quorum: int = 1):
    receipts = R3Receipts()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts, clock=R3Clock())
    view, _ = store.create(
        "g",
        kind="fanout",
        recipients=[T1, T2],
        message=MESSAGE,
        quorum=quorum,
        failover="never_started",
        max_attempts=3,
    )
    first, second = [slot["attempts"][0]["member_key"] for slot in view["slots"]]
    receipts.add(first, T1, "completed")
    row = receipts.add(second, T2, status)
    return store, receipts, second, row


# ---------------------------------------------------------------- R4-2


@pytest.mark.parametrize("fact", ["verdict", "no_candidate"])
def test_false_live_core_fact_with_f3_never_is_not_intrinsically_contradictory(
    tmp_path: Path, fact: str
) -> None:
    """red_at_pretip: R4-2. F3 can say `never` for running work, and candidates can be
    known while it runs: inapplicable, not contradictory."""
    store, _, key, row = r4_group(tmp_path)
    values: dict[str, Any] = {"task_id": row["id"], "core_blocked": False, "stalled": False}
    values[fact] = "never" if fact == "verdict" else False
    view = store.get("g", assessment={key: values})
    assert view["slots"][1]["status"] == "active"
    assert view["slots"][1]["assessment_contradictions"] == []
    assert view["slots"][1]["assessment_inapplicable"] == [fact]


def test_refusal_auto_is_a_real_plan_proof_not_a_contradiction(tmp_path: Path) -> None:
    """red_at_pretip: R4-2. A local refusal with no receipt is proof 1 (plan section 6)."""
    store, _, _ = r3_store(tmp_path)
    store.record_raised("a", "AdmissionRefused")
    facts = {"a": {"task_id": None, "verdict": "auto", "no_candidate": False}}
    slot = store.get("a", assessment=facts)["slots"][0]
    assert slot["assessment_contradictions"] == []
    assert slot["status"] == "not_sent" and slot["retry_eligible"] is True


# ---------------------------------------------------------------- R4-1


@pytest.mark.parametrize("status", ["pending", "queued", "submitted", "running"])
@pytest.mark.parametrize(
    "extra",
    [
        {"verdict": "never"},
        {"verdict": "auto"},
        {"no_candidate": True},
        {"no_candidate": False},
    ],
)
def test_contradiction_must_not_hide_known_core_block(
    tmp_path: Path, status: str, extra: dict[str, Any]
) -> None:
    """red_at_pretip: R4-1."""
    store, _, key, row = r4_group(tmp_path, status)
    base = {"task_id": row["id"], "core_blocked": True, "stalled": False}
    assert store.get("g", assessment={key: base})["status"] == "blocked"
    assert store.get("g", assessment={key: {**base, **extra}})["status"] == "blocked"


@pytest.mark.parametrize("extra", [{"verdict": "never"}, {"no_candidate": False}])
def test_contradiction_must_not_erase_unknown_core_gate(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    """red_at_pretip: R4-1."""
    store, _, key, row = r4_group(tmp_path)
    base = {"task_id": row["id"], "stalled": False}
    assert store.get("g", assessment={key: base})["status"] == "needs_decision"
    assert store.get("g", assessment={key: {**base, **extra}})["status"] == "needs_decision"


@pytest.mark.parametrize("status", ["uncertain", "future_state"])
def test_current_core_block_overrides_uncertain_or_unknown_receipt(
    tmp_path: Path, status: str
) -> None:
    """red_at_pretip: R4-1 (and R4-2: no contradiction named)."""
    store, _, key, row = r4_group(tmp_path, status)
    view = store.get("g", assessment={key: {"task_id": row["id"], "core_blocked": True}})
    assert view["status"] == "blocked"
    assert view["slots"][1]["assessment_contradictions"] == []


@pytest.mark.parametrize("damage", ["unavailable", "conflicting", "gone"])
@pytest.mark.parametrize("supplied", [False, True])
def test_bound_read_failure_does_not_hide_unchecked_or_known_block(
    tmp_path: Path, damage: str, supplied: bool
) -> None:
    """red_at_pretip: R4-1."""
    store, receipts, key, row = r4_group(tmp_path, "submitted")
    store.record_submitted(key, row["id"])
    if damage == "unavailable":
        receipts.errors[key] = OSError("temporarily unreadable")
    elif damage == "conflicting":
        row["message"] = "other work"
    else:
        del receipts.rows[key]
    facts = {key: {"task_id": row["id"], "core_blocked": True}} if supplied else None
    view = store.get("g", assessment=facts)
    assert view["status"] == ("blocked" if supplied else "needs_decision")


# ---------------------------------------------------------------- D2 (code part)


def test_an_expiry_seen_earlier_in_the_same_replay_is_kept(tmp_path: Path) -> None:
    """red_at_pretip: review 4, D2. The clock reads 2001 at the start of the replay and
    1999 at the write; the expiry seen first is not forgotten."""
    store, receipts, _ = r3_store(tmp_path, deadline=2000)
    receipts.add("a")
    store.plan_attempt("a", "0", 2, T2)
    times = iter([2001.0, 1999.0])
    store.clock = lambda: next(times, 1999.0)
    with pytest.raises(ValueError, match="deadline"):
        store.plan_attempt("a", "0", 2, T2)


def test_the_final_clock_read_of_a_replay_still_decides(tmp_path: Path) -> None:
    """pins_existing: N4 does not return; 1999 first and 2001 at the write is refused."""
    store, receipts, _ = r3_store(tmp_path, deadline=2000)
    receipts.add("a")
    store.plan_attempt("a", "0", 2, T2)
    times = iter([1999.0, 2001.0])
    store.clock = lambda: next(times, 2001.0)
    with pytest.raises(ValueError, match="deadline"):
        store.plan_attempt("a", "0", 2, T2)


# ================================================================ fifth review (fix round 5)
#
# Source: the reviewer's ``test_round5.py`` (review ``F2-review5-codex.md``, input
# ``probes-r5.txt``). Each test here is ``red_at_pretip`` against ``301d6fa`` unless it says
# otherwise (``out/F2-fix5-red.log``).


# ---------------------------------------------------------------- R5-1


@pytest.mark.parametrize("latest", ["completed", "running"])
@pytest.mark.parametrize(
    "older", ["blocked", "running_blocked", "uncertain", "unavailable", "conflicting"]
)
def test_older_current_block_or_unknown_must_not_disappear(
    tmp_path: Path, latest: str, older: str
) -> None:
    """red_at_pretip: R5-1, the reviewer's ten two-attempt variants."""
    store, receipts, _ = r3_store(tmp_path)
    store.record_raised("a", "local refusal")
    store.plan_attempt("a", "0", 2, T2)
    new = receipts.add("a.a2", T2, latest)
    store.record_submitted("a.a2", new["id"])
    live = older in {"running_blocked", "unavailable", "conflicting"}
    row = receipts.add("a", T1, "running" if live else older)
    store.record_submitted("a", row["id"])
    if older == "unavailable":
        receipts.errors["a"] = OSError("old attempt read failed")
    elif older == "conflicting":
        row["message"] = "different work"
    facts: dict[str, Any] = {
        "a.a2": {"task_id": new["id"], "core_blocked": False, "stalled": False}
    }
    if older == "running_blocked":
        facts["a"] = {"task_id": row["id"], "core_blocked": True, "stalled": False}
    view = store.get("a", assessment=facts)
    expected = "blocked" if older in {"blocked", "running_blocked"} else "needs_decision"
    assert view["status"] == expected
    assert [item["attempt"] for item in view["unfinished_attempts"]][0] == 1


def test_real_receipts_older_permission_and_completed_successor(tmp_path: Path) -> None:
    """red_at_pretip: R5-1 with real `TaskStore` operations only."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = r3_store(tmp_path, receipts=tasks)
    store.record_raised("a", "refusal")
    store.plan_attempt("a", "0", 2, T2)
    second, _ = tasks.reserve(T2.target, MESSAGE, "a.a2", {"kind": "hc"})
    tasks.update(second["id"], "completed", result="result")
    first, _ = tasks.reserve(T1.target, MESSAGE, "a", {"kind": "hc"})
    tasks.update(first["id"], "blocked")
    view = store.get("a")
    assert view["status"] == "blocked"
    assert view["slots"][0]["winner"] == 2


def test_middle_attempt_block_survives_winner_and_clear_latest(tmp_path: Path) -> None:
    """red_at_pretip: R5-1, the reviewer's three-attempt case."""
    store, receipts, _ = r3_store(tmp_path)
    store.record_raised("a", "refusal")
    store.plan_attempt("a", "0", 2, T2)
    store.record_raised("a.a2", "refusal")
    store.plan_attempt("a", "0", 3, T3)
    first = receipts.add("a", T1, "completed")
    middle = receipts.add("a.a2", T2, "running")
    last = receipts.add("a.a3", T3, "running")
    view = store.get(
        "a",
        assessment={
            "a": {"task_id": first["id"], "core_blocked": False, "stalled": False},
            "a.a2": {"task_id": middle["id"], "core_blocked": True, "stalled": False},
            "a.a3": {"task_id": last["id"], "core_blocked": False, "stalled": False},
        },
    )
    assert view["status"] == "blocked"


@pytest.mark.parametrize("damage", ["unavailable", "conflicting", "future_state", "running"])
@pytest.mark.parametrize("core", [None, False, True])
def test_latest_loser_in_won_slot_keeps_its_block_state(
    tmp_path: Path, damage: str, core: bool | None
) -> None:
    """red_at_pretip: c2, the earlier uncertain completion also needs a Core observation."""
    store, receipts, _ = r3_store(tmp_path)
    store.record_raised("a", "refusal")
    store.plan_attempt("a", "0", 2, T2)
    second = receipts.add("a.a2", T2, "running")
    store.record_submitted("a.a2", second["id"])
    receipts.add("a", T1, "completed")
    store.get("a")
    if damage == "unavailable":
        receipts.errors["a.a2"] = OSError("no read")
    elif damage == "conflicting":
        second["message"] = "other work"
    else:
        second["status"] = damage
    given: dict[str, Any] = {"task_id": second["id"], "stalled": False}
    if core is not None:
        given["core_blocked"] = core
    view = store.get("a", assessment={"a.a2": given})
    expected = "blocked" if core is True else "needs_decision"
    assert view["status"] == expected
    assert view["slots"][0]["winner"] == 1 and view["slots"][0]["late_results"] == []


def test_current_final_and_stale_earlier_facts_control(tmp_path: Path) -> None:
    """pins_existing: the reviewer's control; settled history with unrelated stale facts
    still allows satisfied."""
    store, receipts, _ = r3_store(tmp_path)
    first = receipts.add("a", T1, "failed")
    first["metadata"]["delivery"] = "not_attempted"
    store.record_submitted("a", first["id"])
    store.plan_attempt("a", "0", 2, T2)
    last = receipts.add("a.a2", T2, "completed")
    view = store.get(
        "a",
        assessment={
            "a": {"task_id": "not-a-task", "core_blocked": True},
            "a.a2": {"task_id": last["id"], "core_blocked": False, "stalled": False},
        },
    )
    assert view["status"] == "satisfied"
    assert view["slots"][0]["winner"] == 2 and view["unfinished_attempts"] == []


def test_a_current_core_block_on_ended_history_is_retained(tmp_path: Path) -> None:
    """red_at_pretip: c3 retains the block and identifies the ended attempt's contradiction."""
    store, receipts, _ = r3_store(tmp_path)
    first = receipts.add("a", T1, "failed")
    store.record_submitted("a", first["id"])
    store.plan_attempt("a", "0", 2, T2)
    last = receipts.add("a.a2", T2, "completed")
    view = store.get("a", assessment={"a": {"task_id": first["id"], "core_blocked": True}})
    assert view["status"] == "blocked"
    assert view["slots"][0]["assessment_contradictions"] == []
    assert view["slots"][0]["assessment_contradiction_attempts"] == [
        {"attempt": 1, "member_key": "a", "facts": ["core_blocked"]}
    ]
    assert last["id"] == view["slots"][0]["attempts"][1]["task_id"]


# ---------------------------------------------------------------- R5-2


def test_expired_not_sent_successor_must_not_advertise_eligibility(tmp_path: Path) -> None:
    """red_at_pretip: R5-2."""
    store, _, clock = r3_store(tmp_path, deadline=2000)
    store.record_raised("a", "refusal")
    store.plan_attempt("a", "0", 2, T2)
    store.record_raised("a.a2", "refusal")
    clock.now = 2000
    facts = {"a.a2": {"task_id": None, "verdict": "auto", "no_candidate": False}}
    slot = store.get("a", assessment=facts)["slots"][0]
    with pytest.raises(ValueError, match="deadline"):
        store.plan_attempt("a", "0", 2, T2)
    assert slot["status"] == "not_sent" and slot["retry_eligible"] is False


@pytest.mark.parametrize("mode", ["off", "cap", "first_expired"])
def test_not_sent_same_key_refill_controls(tmp_path: Path, mode: str) -> None:
    """pins_existing: the reviewer's controls; attempt 1's same-key refill stays allowed."""
    receipts, clock = R3Receipts(), R3Clock()
    store = AssignmentStore(tmp_path / "assignments.sqlite", receipts, clock=clock)
    store.create(
        "a",
        kind="single",
        recipients=[T1],
        message=MESSAGE,
        failover="off" if mode == "off" else "never_started",
        max_attempts=1 if mode == "cap" else 3,
        deadline=2000,
    )
    store.record_raised("a", "refusal")
    if mode == "first_expired":
        clock.now = 2001
    facts = {"a": {"task_id": None, "verdict": "auto", "no_candidate": False}}
    assert store.get("a", assessment=facts)["slots"][0]["retry_eligible"] is True
    assert store.plan_attempt("a", "0", 1, T1)[0]["sendable"] is True


@pytest.mark.parametrize(
    ("count", "position"), [(count, position) for count in (1, 2, 3) for position in range(count)]
)
@pytest.mark.parametrize("state", ["failed", "cancelled"])
@pytest.mark.parametrize("path", ["metadata_on_final", "update_after_invalidation"])
@pytest.mark.parametrize("core", [None, False, True])
def test_rw2_invalidated_final_receipt_never_proves_ended(
    tmp_path: Path, count: int, position: int, state: str, path: str, core: bool | None
) -> None:
    """red_at_pretip (38fb3f2): both public TaskStore invalidation paths, every position.

    Bind the subject while planned to keep its admission certain. Other earlier
    attempts advance via local refusal; their late receipts are observed Core-clear.
    Their completion isolates the subject's unknownness from Q2 satisfaction.
    """
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _, _ = make_store(tmp_path, tasks, max_attempts=3)
    keys = ("review", "review.a2", "review.a3")[:count]
    peers = (T1, T2, T3)[:count]
    task_ids = {}
    for index, (key, peer) in enumerate(zip(keys, peers, strict=True)):
        if index:
            store.plan_attempt("review", "0", index + 1, peer)
        if index == position:
            task, _ = tasks.reserve(peer.target, MESSAGE, key, {"kind": "hc"})
            store.record_submitted(key, task["id"])
            tasks.update(task["id"], state)
            task_ids[key] = task["id"]
        else:
            store.record_raised(key, "refused before reservation")

    companion = 0 if position else count - 1
    for index, (key, peer) in enumerate(zip(keys, peers, strict=True)):
        if index == position:
            continue
        task, _ = tasks.reserve(peer.target, MESSAGE, key, {"kind": "hc"})
        tasks.update(
            task["id"],
            "completed" if index == companion else "failed",
            result="verified companion result" if index == companion else None,
        )
        store.record_submitted(key, task["id"])
        task_ids[key] = task["id"]

    key = keys[position]
    task_id = task_ids[key]
    if path == "metadata_on_final":
        tasks.metadata(task_id, {"identity_invalidated": True})
    else:
        tasks.invalidate_submission(task_id, "delivery identity mismatch")
        tasks.update(task_id, state)
    receipt = tasks.get(task_id)
    assert receipt["status"] == state
    assert receipt["metadata"]["identity_invalidated"] is True
    assessment = {
        key: {"task_id": task_ids[key], "core_blocked": False, "stalled": False} for key in keys
    }
    assessment[key].update(verdict="auto", no_candidate=False)
    if core is None:
        assessment[key].pop("core_blocked")
    else:
        assessment[key]["core_blocked"] = core
    view = store.get("review", assessment=assessment)
    slot = view["slots"][0]
    assert slot["attempts"][position]["admission"] == "submitted"
    assert slot["blocked_unknown"] is (core is None)
    assert any(item["task_id"] == task_id for item in view["unfinished_attempts"])
    assert slot["assessment_absent_attempts"] == (
        [{"attempt": position + 1, "member_key": key, "facts": ["core_blocked"]}]
        if core is None
        else []
    )
    assert slot["assessment_contradictions"] == []
    assert slot["assessment_contradiction_attempts"] == []
    assert view["status"] == (
        "blocked"
        if core is True
        else "satisfied"
        if count > 1 and core is False
        else "needs_decision"
    )
    if count > 1:
        assert slot["winner"] == companion + 1


# ================================================================ review 7, M-2 (row T-107)
#
# An attempt recorded `not_sent` (the adapter call raised, the key held no receipt, the
# recipient is local) whose key later holds a final receipt. The form of every state here is
# the review's probe L5 (`status/handoff/T-105-review-claude-7.md`, M-2) and the
# orchestrator's `in/t107_probe.py` of row T-107: `attack_store`, `record_raised` for attempt
# 1, `plan_attempt` for attempt 2, then `AttackReceipts.add` under attempt 1's key.

LATE_FINAL = ("failed", "cancelled", "abandoned")


def m2_stored(tmp_path: Path, member_key: str) -> tuple[str, str | None]:
    """Admission and task ID of one attempt as stored, read without the store's binding.

    `locate` only selects the row; it returns the task ID but not the admission, so the
    admission is read from the database file directly. Neither read calls a receipt lookup.
    """
    with closing(sqlite3.connect(tmp_path / "assignments.sqlite")) as db:
        row = db.execute(
            "SELECT admission, task_id FROM assignment_members WHERE member_key=?",
            (member_key,),
        ).fetchone()
    return row[0], row[1]


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("status", LATE_FINAL)
def test_replan_is_refused_after_a_late_final_receipt_for_an_earlier_not_sent_attempt(
    tmp_path: Path, status: str, read_first: bool
) -> None:
    """[no_read] red_at_pretip, [read] pins_existing: review 7, M-2, probe L5.

    The form of the states is the review's probe L5 and `in/t107_probe.py` (part 1,
    `replan-2`). Without a read the repeat of attempt 2 was returned sendable; with a read
    `get` binds attempt 1 as `uncertain` first and the repeat is refused. Both are refused.
    """
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    late = receipts.add("a", status=status)
    if read_first:
        store.get("a")
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", 2, T2)
    assert str(refused.value) == (
        f"An earlier attempt is unresolved (attempt 1 {status}); "
        "the attempt cannot be planned again"
    )
    if not read_first:
        # A refused plan writes nothing: attempt 1 stays `not_sent` and unbound.
        found = store.locate(member_key="a")
        assert found is not None and found["task_id"] is None
        assert m2_stored(tmp_path, "a") == ("not_sent", None)
        assert m2_stored(tmp_path, "a.a2") == ("planned", None)
    view = store.get("a")
    slot = view["slots"][0]
    first = slot["attempts"][0]
    assert (first["admission"], first["task_id"]) == ("uncertain", late["id"])
    assert m2_stored(tmp_path, "a") == ("uncertain", late["id"])
    # get's docstring: `may_hold` takes "every uncertain-admission final", and Q3 reads the
    # execution state from the latest attempt only (attempt 2, still planned).
    assert [item["attempt"] for item in view["unfinished_attempts"]] == [1]
    assert slot["status"] == "planned" and slot["blocked_unknown"] is True
    assert view["status"] == "admitting"


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("status", LATE_FINAL)
def test_new_successor_is_refused_after_a_late_final_receipt_for_an_older_not_sent_attempt(
    tmp_path: Path, status: str, read_first: bool
) -> None:
    """[no_read] red_at_pretip, [read] pins_existing: review 7, M-2, a path it does not name.

    The form of the states is the review's probe L5 and `in/t107_probe.py` (part 1,
    `new-3`): attempt 2 failed with admission `submitted`, and the late receipt is under
    attempt 1, the older one. Without a read a new attempt 3 was planned and sendable.
    """
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    second = receipts.add("a.a2", recipient=T2, status="failed")
    store.record_submitted("a.a2", second["id"])
    late = receipts.add("a", status=status)
    if read_first:
        store.get("a")
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", 3, T3)
    assert str(refused.value) == (
        f"An earlier attempt is unresolved (attempt 1 {status}); no successor"
    )
    assert store.locate(member_key="a.a3") is None
    with closing(sqlite3.connect(tmp_path / "assignments.sqlite")) as db:
        assert db.execute("SELECT attempt FROM assignment_members ORDER BY attempt").fetchall() == [
            (1,),
            (2,),
        ]
    if not read_first:
        assert m2_stored(tmp_path, "a") == ("not_sent", None)
    assert m2_stored(tmp_path, "a.a2") == ("submitted", second["id"])
    view = store.get("a")
    slot = view["slots"][0]
    assert (slot["attempts"][0]["admission"], slot["attempts"][0]["task_id"]) == (
        "uncertain",
        late["id"],
    )
    # get's docstring: attempt 1 is `may_hold` ("every uncertain-admission final"); Q3 reads
    # the latest attempt, a failed one with no verdict: insufficient observation.
    assert [item["attempt"] for item in view["unfinished_attempts"]] == [1]
    assert slot["status"] == "needs_decision" and slot["blocked_unknown"] is True


def m2_outcome(tmp_path: Path, path: str, status: str, read_first: bool) -> tuple[Any, ...]:
    """One case of `in/t107_probe.py` part 1, in its own store under `tmp_path`."""
    folder = tmp_path / f"{path}-{status}-{read_first}"
    folder.mkdir()
    store, receipts = attack_store(folder)
    store.record_raised("a", "refused")
    if path != "new-2":
        store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second = receipts.add("a.a2", recipient=T2, status="failed")
        store.record_submitted("a.a2", second["id"])
    receipts.add("a", status=status)
    if read_first:
        store.get("a")
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    try:
        view, created = store.plan_attempt("a", "0", attempt, peer)
    except ValueError as error:
        return ("refused", str(error))
    return ("view", view["sendable"], view["admission"], created)


@pytest.mark.parametrize("status", [*LATE_FINAL, "running", "completed"])
def test_planning_answers_do_not_depend_on_a_read_for_a_late_receipt(
    tmp_path: Path, status: str
) -> None:
    """[failed, cancelled, abandoned] red_at_pretip, [running, completed] pins_existing.

    Review 7, M-2; the whole table of `in/t107_probe.py` part 1 (five statuses, three paths).

    The form of the states is the review's probe L5 and `in/t107_probe.py`. For every path
    the outcome with and without a read first is the same kind. The texts differ in one
    place, and rightly: a first successor of the `not_sent` attempt is refused for the
    receipt before a read, and for the `uncertain` admission that the read wrote.
    """
    for path in ("replan-2", "new-2", "new-3"):
        before = m2_outcome(tmp_path, path, status, False)
        after = m2_outcome(tmp_path, path, status, True)
        assert before[0] == after[0] == "refused", (path, before, after)
        if path == "new-2" and status != "completed":
            assert before[1] == (
                "A receipt exists for the attempt recorded as not sent; no successor"
            )
            assert after[1] == "The previous attempt is uncertain; no successor"
        else:
            assert before == after, (path, before, after)


def m2_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    """Every stored attempt row, read from the store file without the store's binding."""
    with closing(sqlite3.connect(tmp_path / "assignments.sqlite")) as db:
        return db.execute("SELECT * FROM assignment_members ORDER BY attempt").fetchall()


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("path", ["replan-2", "new-3"])
@pytest.mark.parametrize("first", ["planned", "not_sent"])
def test_an_earlier_planned_attempt_with_no_receipt_is_unresolved(
    tmp_path: Path, first: str, path: str, read_first: bool
) -> None:
    """pins_existing (passes at d98fba8): review 1 of T-107, B1; the guard's first branch.

    `[planned-*]`: attempt 1 is `planned`, holds no task ID and its key lookup is absent
    while attempt 2 exists. It is built the reviewer's way (`in/review1/review_probe.py`,
    `configure`): a valid `failed` receipt under attempt 1's key, bound by nothing, lets
    attempt 2 be planned; then the double forgets that receipt. The receipt store on `main`
    deletes no row: the state is built to pin the first branch of `_unresolved` (only
    `not_sent` passes as never reserved), it models no event of today. Nobody knows whether
    attempt 1 was sent, so the repeat of attempt 2 and a new attempt 3 are refused, with or
    without a `get` first. `[not_sent-*]` is the control: the same two paths are sendable.
    """
    store, receipts = attack_store(tmp_path)
    if first == "planned":
        receipts.add("a", status="failed")
        store.plan_attempt("a", "0", 2, T2)
        del receipts.rows["a"]
    else:
        store.record_raised("a", "refused")
        store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second = receipts.add("a.a2", recipient=T2, status="failed")
        store.record_submitted("a.a2", second["id"])
    assert m2_stored(tmp_path, "a") == (first, None)
    if read_first:
        store.get("a")
        assert m2_stored(tmp_path, "a") == (first, None)  # the read bound nothing
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    if first == "not_sent":
        view, created = store.plan_attempt("a", "0", attempt, peer)
        assert view["sendable"] is True and view["admission"] == "planned"
        assert created is (path == "new-3")
        return
    before = m2_rows(tmp_path)
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", attempt, peer)
    assert str(refused.value) == (
        "An earlier attempt is unresolved (attempt 1 absent); "
        + ("no successor" if path == "new-3" else "the attempt cannot be planned again")
    )
    assert m2_rows(tmp_path) == before
    assert store.locate(member_key="a.a3") is None
    assert m2_stored(tmp_path, "a") == ("planned", None)


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("path", ["replan-2", "new-3"])
@pytest.mark.parametrize("damage", ["conflicting", "unavailable"])
def test_a_bad_receipt_under_an_earlier_not_sent_attempt_is_not_bound_and_refuses(
    tmp_path: Path, damage: str, path: str, read_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (passes at d98fba8): review 1 of T-107, B4; `_unresolved`'s docstring.

    Attempt 1 is recorded `not_sent` (the form of `in/t107_probe.py`), attempt 2 exists, and
    then attempt 1's key holds a receipt for another target (`conflicting`, the reviewer's
    `DOC_NOT_SENT_CONFLICT_*` in `in/review1/inputs.log`) or its lookup raises `OSError`
    (`unavailable`). `get` binds neither; the guard refuses with and without that read.
    """
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second = receipts.add("a.a2", recipient=T2, status="failed")
        store.record_submitted("a.a2", second["id"])
    if damage == "conflicting":
        receipts.add("a", recipient=T3, status="failed")
    else:
        lookup = receipts.by_request

        def by_request(key: str) -> Any:
            if key == "a":
                raise OSError("attempt 1's receipt cannot be read")
            return lookup(key)

        monkeypatch.setattr(receipts, "by_request", by_request)
    if read_first:
        first = store.get("a")["slots"][0]["attempts"][0]
        assert (first["lookup"], first["admission"], first["task_id"]) == (damage, "not_sent", None)
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    before = m2_rows(tmp_path)
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", attempt, peer)
    assert str(refused.value) == (
        f"An earlier attempt is unresolved (attempt 1 {damage}); "
        + ("no successor" if path == "new-3" else "the attempt cannot be planned again")
    )
    assert m2_rows(tmp_path) == before
    store.get("a")
    assert m2_stored(tmp_path, "a") == ("not_sent", None)
    with pytest.raises(ValueError, match=r"attempt 1 (conflicting|unavailable)"):
        store.plan_attempt("a", "0", attempt, peer)


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("path", ["replan-2", "new-3"])
@pytest.mark.parametrize("first", ["uncertain", "not_sent"])
def test_an_earlier_uncertain_attempt_with_no_receipt_is_unresolved(
    tmp_path: Path, first: str, path: str, read_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (passes at d98fba8): review 2 of T-107, R1; the guard's first branch.

    `[uncertain-*]`: attempt 1 is recorded `uncertain`, holds no task ID and its key lookup
    is absent while attempt 2 exists. The form is review 2's steps 1 to 7
    (`in/T-107-review-codex-2.md`, R1), all through the store's public calls, with no stored
    row changed by hand and no receipt deleted: `record_raised` with an absent lookup
    (`not_sent`), `plan_attempt` for attempt 2, then a second `record_raised` for attempt 1
    while the lookup by its key raises `OSError` once (`uncertain`, bound to nothing); after
    that the lookup works again and finds nothing. Nobody knows whether attempt 1 was sent,
    so the repeat of attempt 2 and a new attempt 3 are refused, with or without a `get`
    first. `[not_sent-*]` is the control: the same two paths are sendable.
    """
    store, receipts = attack_store(tmp_path)
    store.record_raised("a", "refused")
    store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second = receipts.add("a.a2", recipient=T2, status="failed")
        store.record_submitted("a.a2", second["id"])
    if first == "uncertain":
        lookup = receipts.by_request
        raised: list[str] = []

        def by_request(key: str) -> Any:
            if not raised:
                raised.append(key)
                raise OSError("the receipt store cannot be read")
            return lookup(key)

        monkeypatch.setattr(receipts, "by_request", by_request)
        store.record_raised("a", "raised again")
        assert raised == ["a"]  # one failed lookup, under attempt 1's key
    assert m2_stored(tmp_path, "a") == (first, None)
    if read_first:
        seen = store.get("a")["slots"][0]["attempts"][0]
        assert (seen["lookup"], seen["admission"], seen["task_id"]) == ("absent", first, None)
        assert m2_stored(tmp_path, "a") == (first, None)  # the read bound nothing
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    if first == "not_sent":
        view, created = store.plan_attempt("a", "0", attempt, peer)
        assert view["sendable"] is True and view["admission"] == "planned"
        assert created is (path == "new-3")
        return
    before = m2_rows(tmp_path)
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", attempt, peer)
    assert str(refused.value) == (
        "An earlier attempt is unresolved (attempt 1 absent); "
        + ("no successor" if path == "new-3" else "the attempt cannot be planned again")
    )
    assert m2_rows(tmp_path) == before
    assert store.locate(member_key="a.a3") is None
    assert [row[3] for row in before] == [1, 2]  # column 3 is `attempt`: no row for attempt 3
    assert m2_stored(tmp_path, "a") == ("uncertain", None)


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("path", ["replan-2", "new-3"])
def test_a_late_receipt_under_an_earlier_uncertain_attempt_leaves_it_uncertain(
    tmp_path: Path, path: str, read_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (passes at d98fba8): review 3 of T-107, B1; the admission `get` writes.

    The continuation of the R1 test above, with a real `TaskStore` and the review's steps 1
    to 6 (`in/T-107-review-codex-3.md`, B1), all through public calls: attempt 1 is recorded
    `uncertain` and bound to nothing (a second `record_raised` while the lookup by its key
    raises `OSError` once); then a receipt for the same work is reserved under its key and
    fails. A `get` binds that receipt and must leave the admission `uncertain`: a `failed`
    receipt under an attempt that nobody knows was sent settles nothing. So the repeat of
    attempt 2 and a new attempt 3 are refused, with the same message with and without a
    `get` first. A `get` that wrote `submitted` here would make both sendable after a read.
    """
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store, _ = attack_store(tmp_path, tasks)
    store.record_raised("a", "not reserved")
    store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second, _ = tasks.reserve(T2.target, MESSAGE, "a.a2", {"kind": T2.provider})
        tasks.update(second["id"], "failed")
        store.record_submitted("a.a2", second["id"])
    lookup = tasks.by_request
    raised: list[str] = []

    def by_request(key: str) -> Any:
        if not raised:
            raised.append(key)
            raise OSError("the receipt store cannot be read")
        return lookup(key)

    monkeypatch.setattr(tasks, "by_request", by_request)
    store.record_raised("a", "raised again")
    assert raised == ["a"]  # one failed lookup, under attempt 1's key
    assert m2_stored(tmp_path, "a") == ("uncertain", None)
    late, created = tasks.reserve(T1.target, MESSAGE, "a", {"kind": T1.provider})
    assert created is True
    assert tasks.update(late["id"], "failed")["status"] == "failed"
    assert m2_stored(tmp_path, "a") == ("uncertain", None)  # the receipt store wrote no row here
    if read_first:
        seen = store.get("a")["slots"][0]["attempts"][0]
        assert (seen["lookup"], seen["admission"], seen["task_id"], seen["receipt_status"]) == (
            "valid",
            "uncertain",
            late["id"],
            "failed",
        )
        assert m2_stored(tmp_path, "a") == ("uncertain", late["id"])  # bound, still uncertain
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    before = m2_rows(tmp_path)
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", attempt, peer)
    assert str(refused.value) == (
        "An earlier attempt is unresolved (attempt 1 failed); "
        + ("no successor" if path == "new-3" else "the attempt cannot be planned again")
    )
    assert m2_rows(tmp_path) == before
    assert store.locate(member_key="a.a3") is None
    assert [row[3] for row in before] == [1, 2]  # column 3 is `attempt`: no row for attempt 3
    assert m2_stored(tmp_path, "a") == ("uncertain", late["id"] if read_first else None)


# ---------------------------------------------------------------- T-107, fix round 3: the table
#
# One case per value that the second branch of the planning guard could wrongly accept or
# wrongly refuse (the hand-back's table, "Fix round 3"). Attempt 1 holds a bound receipt
# with a certain admission; attempt 2 exists; the receipt of attempt 1 then takes the form
# under test. The live statuses are the module's `UNFINISHED`; the final ones its
# `TERMINAL_RECEIPT`; the flags are the two that the module reads from a receipt.

EARLIER_RECEIPT_CASES = [
    # (id, status when attempt 2 is planned, status afterwards, flag afterwards, sendable)
    ("pending", "failed", "pending", None, False),
    ("queued", "failed", "queued", None, False),
    ("submitted", "failed", "submitted", None, False),
    ("running", "failed", "running", None, False),
    ("failed-blocked_seen", "failed", "failed", "blocked_seen", False),
    ("failed", "failed", "failed", None, True),
    ("cancelled", "cancelled", "cancelled", None, True),
    ("abandoned", "abandoned", "abandoned", None, True),
    ("failed-invalidated", "failed", "failed", "identity_invalidated", True),
    ("completed-invalidated", "failed", "completed", "identity_invalidated", True),
]


@pytest.mark.parametrize("read_first", [False, True], ids=["no_read", "read"])
@pytest.mark.parametrize("path", ["replan-2", "new-3"])
@pytest.mark.parametrize(
    ("planned_at", "status", "flag", "sendable"),
    [pytest.param(*case[1:], id=case[0]) for case in EARLIER_RECEIPT_CASES],
)
def test_the_receipt_of_an_earlier_attempt_decides_new_work(
    tmp_path: Path,
    planned_at: str,
    status: str,
    flag: str | None,
    sendable: bool,
    path: str,
    read_first: bool,
) -> None:
    """pins_existing (passes at d98fba8): fix round 3 of T-107; the guard's second branch.

    Refused: a receipt of attempt 1 that is live (`pending`, `queued`, `submitted`,
    `running`) or that waited for permission (`blocked_seen`). Sendable: one that is
    `failed`, `cancelled` or `abandoned`, and a final one that was invalidated (the guard
    lets it pass; `completed` and invalidated is no valid completion, so the slot is not
    satisfied either). Where the status differs from the one at planning time, the double's
    row is changed as in the N1 test above (`test_bad_older_receipt_...`). A real
    `TaskStore` reaches all ten forms through its public calls (run in fix round 4, review
    3 B3, for the repeat of attempt 2): `update` alone leaves a final receipt as it is, but
    `invalidate_submission` makes it `uncertain`, and an `update` then moves it to each of
    the four live statuses, to `failed` and to `completed`; `metadata` sets `blocked_seen`
    on a final receipt and sets `identity_invalidated` back to false. So no form here
    needs a receipt store that answers differently on a later read; the double only
    reaches each form in one step.
    The flags are set as `TaskStore.metadata` stores them, under `metadata`. Both paths,
    with and without a `get` first.
    """
    store, receipts, _ = r3_store(tmp_path)
    first = receipts.add("a", status=planned_at)
    store.record_submitted("a", first["id"])
    store.plan_attempt("a", "0", 2, T2)
    if path == "new-3":
        second = receipts.add("a.a2", T2)
        store.record_submitted("a.a2", second["id"])
    first["status"] = status
    if flag is not None:
        first["metadata"][flag] = True
    assert m2_stored(tmp_path, "a") == ("submitted", first["id"])
    if read_first:
        seen = store.get("a")["slots"][0]["attempts"][0]
        assert (seen["lookup"], seen["admission"], seen["receipt_status"]) == (
            "valid",
            "submitted",
            status,
        )
    attempt, peer = (3, T3) if path == "new-3" else (2, T2)
    before = m2_rows(tmp_path)
    if sendable:
        view, created = store.plan_attempt("a", "0", attempt, peer)
        assert view["sendable"] is True and view["admission"] == "planned"
        assert created is (path == "new-3")
        return
    with pytest.raises(ValueError) as refused:
        store.plan_attempt("a", "0", attempt, peer)
    assert str(refused.value) == (
        f"An earlier attempt is unresolved (attempt 1 {status}); "
        + ("no successor" if path == "new-3" else "the attempt cannot be planned again")
    )
    assert m2_rows(tmp_path) == before
    assert store.locate(member_key="a.a3") is None
