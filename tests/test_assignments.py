"""Assignment store (plan row F2): slots and attempts over ordinary receipts.

Labels. Every test carries ``red_at_pretip`` or ``pins_existing`` in its docstring. Round 1
wrote these tests together with the module, so they all failed at ``5c135ad`` on import.
Fix round 1 (start ``3fa106c``) changed the interface (``Recipient``), so at ``3fa106c``
every test fails on import as well; a label added or changed in fix round 1 says whether
the *behaviour* was wrong at ``3fa106c`` (``red_at_pretip``, shown by the reviewer's
probes in ``out/F2-fix1-red.log``) or already right (``pins_existing``).

The receipt side is driven by ``FakeReceipts``. Its rows have the shape that
``TaskStore.decode`` returns (``tasks.py`` ``reserve`` and ``decode_task``): ``id``,
``request_key``, ``target``, ``message``, ``status`` and a decoded ``metadata`` mapping
with ``kind``. Statuses are the ones in ``tasks.STATES``. A fake receipt has no default
target: each test names it, so a receipt for another recipient is the ordinary negative
case. Targets follow ``CoreTasks.target_key`` (``hc:<session>:<terminal>``).
"""

import hashlib
import itertools
import os
import re
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from answerback_bridge import assignments
from answerback_bridge.assignments import (
    ASSIGNMENT_STATES,
    SLOT_STATES,
    AssignmentStore,
    Recipient,
)
from answerback_bridge.tasks import STATES, TaskStore

MESSAGE = "Review the change on branch akirem/fab-f2"
KEY_RULE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def target_of(peer: str) -> str:
    """The receipt target an adapter builds for ``peer`` (``core_tasks.py`` target_key)."""
    provider, _, rest = peer.partition(":")
    return f"{provider}:default:{rest}"


def R(peer: str) -> Recipient:
    """A resolved recipient; ``node:`` peers are remote, all others local."""
    provider = peer.partition(":")[0]
    return Recipient(peer, target_of(peer), provider, not peer.startswith("node:"))


def rs(peers: list[str]) -> list[Recipient]:
    return [R(peer) for peer in peers]


class FakeReceipts:
    """Receipts in the form ``TaskStore.decode`` returns, with no bridge behind them."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.errors: set[str] = set()  # task IDs whose reads raise OSError

    def add(
        self,
        request_key: str,
        status: str,
        *,
        target: str,
        provider: str = "hc",
        message: str = MESSAGE,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert status in STATES
        assert all(row["request_key"] != request_key for row in self.rows.values())
        row = {
            "id": str(uuid.uuid4()),
            "request_key": request_key,
            "target": target,
            "message": message,
            "status": status,
            "metadata": {"kind": provider, **(metadata or {})},
        }
        self.rows[row["id"]] = row
        return dict(row)

    def set(self, task_id: str, status: str, **metadata: Any) -> None:
        assert status in STATES
        self.rows[task_id]["status"] = status
        self.rows[task_id]["metadata"].update(metadata)

    def prune(self, task_id: str) -> None:
        """What retention does to a completed receipt: the text goes, its identity stays."""
        row = self.rows[task_id]
        identity = {
            "present": True,
            "bytes": len(row["message"].encode()),
            "sha256": hashlib.sha256(row["message"].encode()).hexdigest(),
        }
        row["message"] = ""
        row["_retention"] = {"fields": {"message": identity}}

    def drop(self, task_id: str) -> None:
        del self.rows[task_id]

    def by_request(self, request_key: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if row["request_key"] == request_key:
                if row["id"] in self.errors:
                    raise OSError("receipt cannot be read")
                return dict(row, metadata=dict(row["metadata"]))
        return None

    def get(self, task_id: str) -> dict[str, Any]:
        if task_id in self.errors:
            raise OSError("receipt cannot be read")
        if task_id not in self.rows:
            raise ValueError("Unknown task ID")
        row = self.rows[task_id]
        return dict(row, metadata=dict(row["metadata"]))


@pytest.fixture
def receipts() -> FakeReceipts:
    return FakeReceipts()


@pytest.fixture
def store(tmp_path: Path, receipts: FakeReceipts) -> AssignmentStore:
    return AssignmentStore(tmp_path / "state" / "assignments.sqlite", receipts)


def single(store: AssignmentStore, **options: Any) -> dict[str, Any]:
    view, created = store.create(
        "ab-20261120-F2-single", kind="single", recipients=rs(["hc:t1"]), message=MESSAGE, **options
    )
    assert created
    return view


def fanout(store: AssignmentStore, peers: list[str], **options: Any) -> dict[str, Any]:
    view, created = store.create(
        "ab-20261120-F2-review", kind="fanout", recipients=rs(peers), message=MESSAGE, **options
    )
    assert created
    return view


def keys(view: dict[str, Any]) -> list[str]:
    return [slot["attempts"][0]["member_key"] for slot in view["slots"]]


def attempt_for(store: AssignmentStore, key: str) -> dict[str, Any]:
    found = store.locate(member_key=key)
    assert found is not None
    view = store.get(found["assignment_id"])
    return next(
        item for slot in view["slots"] for item in slot["attempts"] if item["member_key"] == key
    )


def own(
    store: AssignmentStore, receipts: FakeReceipts, key: str, status: str, **options: Any
) -> dict[str, Any]:
    """A receipt that the attempt's own adapter would reserve: its target and provider."""
    attempt = attempt_for(store, key)
    return receipts.add(
        key, status, target=attempt["target"], provider=attempt["provider"], **options
    )


def submit(store: AssignmentStore, receipts: FakeReceipts, key: str, status: str) -> str:
    """What F4 does for one member: the adapter reserves under the key, then we bind."""
    task = own(store, receipts, key, status)
    store.record_submitted(key, task["id"])
    return task["id"]


def slot_of(view: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return view["slots"][index]


def clear(store: AssignmentStore, assignment_id: str) -> dict[str, Any]:
    """`get` with Core and stall facts known false for every live latest attempt.

    Fix round 3 (N2): a live receipt reads `active` only when both facts are supplied.
    """
    view = store.get(assignment_id)
    given = {}
    for slot in view["slots"]:
        latest = slot["attempts"][-1]
        if latest["receipt_status"] in ("pending", "queued", "submitted", "running"):
            given[latest["member_key"]] = {
                "task_id": latest["task_id"],
                "core_blocked": False,
                "stalled": False,
            }
    return store.get(assignment_id, assessment=given)


# ---------------------------------------------------------------- the store file


def test_store_directory_and_file_are_private(tmp_path: Path, receipts: FakeReceipts) -> None:
    """red_at_pretip: same safety as the other stores (private directory, 0600 file)."""
    path = tmp_path / "state" / "assignments.sqlite"
    AssignmentStore(path, receipts)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    AssignmentStore(path, receipts)  # a second open migrates nothing and keeps the modes
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_refuses_a_symlinked_database(tmp_path: Path, receipts: FakeReceipts) -> None:
    """red_at_pretip"""
    (tmp_path / "real.sqlite").write_bytes(b"")
    (tmp_path / "assignments.sqlite").symlink_to(tmp_path / "real.sqlite")
    with pytest.raises(ValueError, match="symlink"):
        AssignmentStore(tmp_path / "assignments.sqlite", receipts)


def test_real_task_store_is_a_receipt_lookup(tmp_path: Path) -> None:
    """red_at_pretip: the Protocol is met by TaskStore itself, not only by the fake."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    store = AssignmentStore(tmp_path / "assignments.sqlite", tasks)
    view = single(store)
    key = keys(view)[0]
    task, created = tasks.reserve("hc:default:t1", MESSAGE, key, {"kind": "hc"})
    assert created
    store.record_submitted(key, task["id"])
    tasks.update(task["id"], "completed", result="done")
    assert store.get(view["assignment_id"])["status"] == "satisfied"


# ---------------------------------------------------------------- slot status table


def test_slot_planned_and_assignment_admitting(store: AssignmentStore) -> None:
    """red_at_pretip: slot row `planned` and assignment rule 1 `admitting`."""
    view = single(store)
    assert slot_of(view)["status"] == "planned"
    assert slot_of(view)["attempts"][0]["admission"] == "planned"
    assert view["status"] == "admitting"


@pytest.mark.parametrize("status", ["pending", "queued", "submitted", "running"])
def test_slot_active_for_each_unfinished_receipt_state(
    store: AssignmentStore, receipts: FakeReceipts, status: str
) -> None:
    """red_at_pretip: slot row `active` and assignment rule 4 `active`."""
    view = single(store)
    submit(store, receipts, keys(view)[0], status)
    view = clear(store, view["assignment_id"])
    assert slot_of(view)["status"] == "active"
    assert slot_of(view)["attempts"][0]["receipt_status"] == status
    assert view["status"] == "active"


def test_slot_blocked_and_assignment_blocked(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: slot row `blocked` (receipt `blocked`) and assignment rule 2."""
    view = single(store)
    submit(store, receipts, keys(view)[0], "blocked")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "blocked"
    assert view["status"] == "blocked"


def test_slot_satisfied_writes_the_winner_and_nothing_reads_completed(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: slot row `satisfied`; only a receipt reads `completed`."""
    view = single(store)
    task_id = submit(store, receipts, keys(view)[0], "completed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "satisfied"
    assert slot_of(view)["winner"] == 1
    assert slot_of(view)["attempts"][0]["winner"] is True
    assert slot_of(view)["attempts"][0]["task_id"] == task_id
    assert view["status"] == "satisfied"
    assert "completed" not in SLOT_STATES
    assert "completed" not in ASSIGNMENT_STATES


def test_uncertain_receipt_reads_needs_decision_even_with_no_attempt_left(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: an uncertain receipt never reads as exhausted or as never started."""
    view = single(store)  # failover off: attempt 1 is the last one
    submit(store, receipts, keys(view)[0], "uncertain")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert view["status"] == "needs_decision"
    assert slot_of(view)["status"] not in ("exhausted", "not_sent", "planned")


def test_uncertain_receipt_found_by_key_is_never_not_sent(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: the call raised, but the key lookup finds an uncertain receipt."""
    view = fanout(store, ["hc:t1", "hc:t2"])
    key = keys(view)[0]
    task = own(store, receipts, key, "uncertain")
    attempt = store.record_raised(key, "BridgeError")
    assert attempt["admission"] == "uncertain"
    assert attempt["task_id"] == task["id"]
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"


def test_call_that_raised_after_reserving_reads_needs_decision_not_active(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: admission `uncertain` on a receipt still `pending` needs a person."""
    view = single(store)
    key = keys(view)[0]
    own(store, receipts, key, "pending")
    store.record_raised(key, "RuntimeError")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert slot_of(view)["attempts"][0]["admission"] == "uncertain"


def test_call_refused_before_reservation_reads_not_sent(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: proof 1 of "never started": raised, and no receipt for the key."""
    view = fanout(store, ["hc:t1", "hc:t2"])
    key = keys(view)[0]
    attempt = store.record_raised(key, "ValueError")
    assert attempt["admission"] == "not_sent"
    assert attempt["task_id"] is None
    submit(store, receipts, keys(view)[1], "completed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "not_sent"
    assert view["status"] == "needs_decision"


@pytest.mark.parametrize("final", ["failed", "cancelled", "abandoned"])
def test_final_receipt_with_an_attempt_left_reads_needs_decision(
    store: AssignmentStore, receipts: FakeReceipts, final: str
) -> None:
    """red_at_pretip: slot row `needs_decision` (ended; a verdict is F3's to give)."""
    view = single(store, failover="never_started")
    submit(store, receipts, keys(view)[0], final)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert view["status"] == "needs_decision"


def test_final_receipt_on_the_last_attempt_reads_exhausted(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: slot row `exhausted` and assignment rule 6 `unsatisfied`."""
    view = single(store, failover="never_started", max_attempts=2)
    first = submit(store, receipts, keys(view)[0], "failed")
    second, created = store.plan_attempt(
        view["assignment_id"], "0", 2, R("hc:t9"), reason="not_attempted", verdict="auto"
    )
    assert created
    assert second["parent_task_id"] == first
    submit(store, receipts, second["member_key"], "failed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "exhausted"
    assert view["status"] == "unsatisfied"


def test_failover_off_makes_the_first_attempt_the_last(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: with failover `off` a failed attempt 1 is `exhausted`."""
    view = single(store)
    submit(store, receipts, keys(view)[0], "failed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "exhausted"
    with pytest.raises(ValueError, match="no attempts left"):
        store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t9"))


def test_missing_receipt_reads_needs_decision(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: a bound receipt that can no longer be read is never progress."""
    view = single(store)
    task_id = submit(store, receipts, keys(view)[0], "running")
    receipts.drop(task_id)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert slot_of(view)["attempts"][0]["lookup"] == "unavailable"
    assert slot_of(view)["attempts"][0]["receipt_status"] is None


# ---------------------------------------------------------------- assignment status table


def test_all_rule_satisfied_when_every_slot_is_satisfied(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: assignment rule 3 with `all`."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"])
    for key in keys(view):
        submit(store, receipts, key, "completed")
    view = store.get(view["assignment_id"])
    assert view["status"] == "satisfied"
    assert view["quorum"] == 3
    assert view["outstanding"] == []
    assert view["counts"]["satisfied"] == 3


def test_quorum_counts_completed_and_running_together(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: plan section 10, F2: 2 of 3, one completed, one running, one failed."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=2)
    done, running, failed = keys(view)
    submit(store, receipts, done, "completed")
    submit(store, receipts, running, "running")
    submit(store, receipts, failed, "failed")
    view = clear(store, view["assignment_id"])
    assert view["status"] == "active"
    assert view["status"] not in ("unsatisfied", "needs_decision")


def test_quorum_met_still_lists_the_outstanding_member(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: plan section 10, F2: two completed and one running read satisfied."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=2)
    first, second, third = keys(view)
    submit(store, receipts, first, "completed")
    submit(store, receipts, second, "completed")
    submit(store, receipts, third, "running")
    view = clear(store, view["assignment_id"])
    assert view["status"] == "satisfied"
    assert view["outstanding"] == [slot_of(view, 2)["slot"]]
    assert slot_of(view, 2)["status"] == "active"


def test_needs_decision_when_settling_a_slot_could_still_meet_the_rule(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: assignment rule 5 `needs_decision`."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"])
    first, second, third = keys(view)
    submit(store, receipts, first, "completed")
    submit(store, receipts, second, "completed")
    submit(store, receipts, third, "uncertain")
    assert store.get(view["assignment_id"])["status"] == "needs_decision"


def test_unsatisfied_when_the_rule_can_no_longer_be_met(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: assignment rule 6 `unsatisfied`, quorum 2 of 3 with two exhausted."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=2)
    first, second, third = keys(view)
    submit(store, receipts, first, "completed")
    submit(store, receipts, second, "failed")
    submit(store, receipts, third, "cancelled")
    view = store.get(view["assignment_id"])
    assert view["status"] == "unsatisfied"
    assert view["outstanding"] == []


def test_admitting_comes_before_every_other_reading(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: rule order 1 before 2: a planned slot beats a blocked one."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=1)
    first, second, _ = keys(view)
    submit(store, receipts, first, "completed")
    submit(store, receipts, second, "blocked")
    assert store.get(view["assignment_id"])["status"] == "admitting"


def test_blocked_comes_before_a_met_quorum(store: AssignmentStore, receipts: FakeReceipts) -> None:
    """red_at_pretip: rule order 2 before 3: a person's turn is never hidden."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=2)
    first, second, third = keys(view)
    submit(store, receipts, first, "completed")
    submit(store, receipts, second, "completed")
    submit(store, receipts, third, "blocked")
    assert store.get(view["assignment_id"])["status"] == "blocked"


# ---------------------------------------------------------------- identity


def test_reordered_members_give_the_same_assignment(store: AssignmentStore) -> None:
    """red_at_pretip: same group ID, same members in another order, same assignment."""
    first = fanout(store, ["hc:t1", "crew:review-kiro", "codex:review"], quorum=2)
    again, created = store.create(
        "ab-20261120-F2-review",
        kind="fanout",
        recipients=rs(["codex:review", "hc:t1", "crew:review-kiro"]),
        message=MESSAGE,
        quorum=2,
    )
    assert not created
    assert keys(again) == keys(first)
    assert [slot["peer_id"] for slot in again["slots"]] == [
        "hc:t1",
        "crew:review-kiro",
        "codex:review",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"peers": ["hc:t1", "hc:t2", "hc:t4"]},
        {"peers": ["hc:t1", "hc:t2"]},
        {"peers": ["hc:t1", "hc:t2", "hc:t3", "hc:t4"]},
        {"message": MESSAGE + "!"},
        {"quorum": 2},
        {"route": "bus"},
        {"failover": "never_started"},
        {"max_attempts": 3},
        {"independent_of": "hc:author"},
        {"recipients": [R("hc:t1"), R("hc:t2"), Recipient("hc:t3", "hc:other:t3", "hc", True)]},
        {"recipients": [R("hc:t1"), R("hc:t2"), Recipient("hc:t3", "hc:default:t3", "hc", False)]},
    ],
)
def test_changed_work_under_the_same_group_id_is_refused(
    store: AssignmentStore, change: dict[str, Any]
) -> None:
    """red_at_pretip: a changed member list, message or rule is refused, nothing written."""
    fanout(store, ["hc:t1", "hc:t2", "hc:t3"])
    options: dict[str, Any] = {"peers": ["hc:t1", "hc:t2", "hc:t3"], "message": MESSAGE}
    options.update(change)
    options.setdefault("recipients", rs(options.pop("peers")))
    options.pop("peers", None)
    with pytest.raises(ValueError, match="assignment_id already describes different work"):
        store.create("ab-20261120-F2-review", kind="fanout", **options)
    view = store.get("ab-20261120-F2-review")
    assert [slot["peer_id"] for slot in view["slots"]] == ["hc:t1", "hc:t2", "hc:t3"]


@pytest.mark.parametrize(
    ("recipient", "match"),
    [
        (Recipient("node:other-host/hc:t1", "node:other-host/hc:t1", "node", True), "never local"),
        (Recipient("hc:t1", "", "hc", True), "target"),
        (Recipient("hc:t1", "hc:default:t1\n", "hc", True), "target"),
        (Recipient("hc:t1", "hc:default:t1", "HC", True), "provider"),
        (Recipient("hc:t1", "hc:default:t1", "hc", 1), "local"),
        (("hc:t1", "hc:default:t1"), "Recipient"),
    ],
)
def test_invalid_recipients_are_refused(store: AssignmentStore, recipient: Any, match: str) -> None:
    """red_at_pretip: fix round 1 (finding 1): the resolved recipient is checked."""
    with pytest.raises(ValueError, match=match):
        store.create("ab-bad", kind="single", recipients=[recipient], message=MESSAGE)


def test_two_peers_resolving_to_one_target_are_refused(store: AssignmentStore) -> None:
    """red_at_pretip: two names for one terminal would split the one-task-per-target rule."""
    with pytest.raises(ValueError, match="duplicate"):
        store.create(
            "ab-bad",
            kind="fanout",
            recipients=[R("hc:t1"), Recipient("hc:alias", "hc:default:t1", "hc", True)],
            message=MESSAGE,
        )


@pytest.mark.parametrize(
    ("kind", "peers", "options", "match"),
    [
        ("fanout", ["hc:t1"], {}, "2 to 8"),
        ("fanout", [f"hc:t{n}" for n in range(9)], {}, "2 to 8"),
        ("fanout", ["hc:t1", "hc:t1"], {}, "duplicate"),
        ("single", ["hc:t1", "hc:t2"], {}, "exactly one"),
        ("single", ["hc:t1"], {"quorum": 1}, "quorum"),
        ("fanout", ["hc:t1", "hc:t2"], {"quorum": 3}, "quorum"),
        ("fanout", ["hc:t1", "hc:t2"], {"quorum": 0}, "quorum"),
        ("fanout", ["hc:t1", "hc:t2"], {"route": "lan"}, "route"),
        ("fanout", ["hc:t1", "hc:t2"], {"failover": "stopped"}, "failover"),
        ("fanout", ["hc:t1", "hc:t2"], {"max_attempts": 4}, "max_attempts"),
        ("fanout", ["hc:t1", "hc:t2"], {"max_attempts": 0}, "max_attempts"),
        ("fanout", ["hc:t1", "Not a peer"], {}, "peer"),
        ("fanout", ["hc:t1", "hc:t2"], {"independent_of": "hc:t2"}, "independent_of"),
        ("pair", ["hc:t1", "hc:t2"], {}, "kind"),
    ],
)
def test_invalid_assignments_are_refused(
    store: AssignmentStore,
    kind: str,
    peers: list[str],
    options: dict[str, Any],
    match: str,
) -> None:
    """red_at_pretip: plan decision 4 limits (2 to 8, no duplicates) and value checks."""
    with pytest.raises(ValueError, match=match):
        store.create("ab-bad", kind=kind, recipients=rs(peers), message=MESSAGE, **options)
    with pytest.raises(ValueError, match="Unknown assignment"):
        store.get("ab-bad")


@pytest.mark.parametrize(
    ("kind", "assignment_id"),
    [
        ("fanout", "a" * 97),
        ("single", "a" * 121),
        ("fanout", "has space"),
        ("fanout", "has#hash"),
        ("fanout", ""),
    ],
)
def test_assignment_ids_follow_the_receipt_alphabet_and_caps(
    store: AssignmentStore, kind: str, assignment_id: str
) -> None:
    """red_at_pretip: receipt rule `[A-Za-z0-9_.:-]`, 96 for a group, 120 for a single."""
    peers = ["hc:t1", "hc:t2"] if kind == "fanout" else ["hc:t1"]
    with pytest.raises(ValueError, match="assignment_id"):
        store.create(assignment_id, kind=kind, recipients=rs(peers), message=MESSAGE)


def test_message_must_be_1_to_100000_characters(store: AssignmentStore) -> None:
    """red_at_pretip: the receipt's own message limit (`tasks.py` reserve)."""
    for message in ("  ", "x" * 100_001):
        with pytest.raises(ValueError, match="Message"):
            store.create("ab-msg", kind="single", recipients=rs(["hc:t1"]), message=message)


# ---------------------------------------------------------------- keys


def test_member_keys_follow_the_receipt_rule_at_their_longest(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: integrated draft section 3 keys, checked against `tasks.py:236`."""
    group = "g" * 96
    peers = ["hc:t1", "node:other-host/hc:t12"]
    view, _ = store.create(
        group,
        kind="fanout",
        recipients=rs(peers),
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    for slot, peer in zip(view["slots"], peers, strict=True):
        assert slot["slot"] == hashlib.sha256(peer.encode()).hexdigest()[:16]
        assert slot["attempts"][0]["member_key"] == f"g:{group}:{slot['slot']}"
    first = keys(view)[0]
    submit(store, receipts, first, "failed")
    second, _ = store.plan_attempt(group, slot_of(view)["slot"], 2, R("hc:t7"))
    assert second["member_key"] == first + ".a2"
    submit(store, receipts, second["member_key"], "failed")
    third, _ = store.plan_attempt(group, slot_of(view)["slot"], 3, R("hc:t8"))
    assert third["member_key"] == first + ".a3"
    assert len(third["member_key"]) == 118

    base = "s" * 120
    view, _ = store.create(
        base,
        kind="single",
        recipients=rs(["hc:t1"]),
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    assert slot_of(view)["slot"] == "0"
    assert keys(view) == [base]
    submit(store, receipts, base, "failed")
    attempt, _ = store.plan_attempt(base, "0", 2, R("hc:t2"))
    assert attempt["member_key"] == base + ".a2"
    for key in (first, second["member_key"], third["member_key"], base, attempt["member_key"]):
        assert KEY_RULE.fullmatch(key)


def test_single_assignment_adopts_an_existing_receipt_as_attempt_1(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: failover lane section 4.4: the caller's request ID is attempt 1."""
    task = receipts.add("ab-20261120-F2-single", "running", target="hc:default:t1")
    view = single(store)
    attempt = slot_of(view)["attempts"][0]
    assert attempt["task_id"] == task["id"]
    assert attempt["admission"] == "submitted"
    assert clear(store, view["assignment_id"])["status"] == "active"


@pytest.mark.parametrize(
    "other",
    [
        {"target": "hc:default:t1", "message": "Delete the branch"},
        {"target": "hc:default:t99"},
        {"target": "hc:default:t1", "provider": "herdr"},
    ],
)
def test_a_key_that_already_holds_different_work_is_refused(
    store: AssignmentStore, receipts: FakeReceipts, other: dict[str, Any]
) -> None:
    """red_at_pretip (target and provider cases; the message case pins_existing): a receipt
    under the key for another message, recipient or provider is never adopted."""
    receipts.add("ab-20261120-F2-single", "completed", **other)
    with pytest.raises(ValueError, match="already describes different work"):
        single(store)
    with pytest.raises(ValueError, match="Unknown assignment"):
        store.get("ab-20261120-F2-single")


@pytest.mark.parametrize(
    "other",
    [
        {"target": "hc:default:t1", "message": "Something else"},
        {"target": "hc:default:t99"},
        {"target": "hc:default:t1", "provider": "crew"},
    ],
)
def test_a_receipt_that_appears_with_different_work_is_a_conflict_not_absence(
    store: AssignmentStore, receipts: FakeReceipts, other: dict[str, Any]
) -> None:
    """red_at_pretip: review finding 2. Round 1's version of this test asserted the unsafe
    reading (`planned`, then `not_sent`). A conflicting receipt under the key is never
    bound, never reads as "no receipt", and never allows a successor."""
    view = fanout(store, ["hc:t1", "hc:t2"], failover="never_started")
    key = keys(view)[0]
    receipts.add(key, "completed", **other)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert slot_of(view)["attempts"][0]["lookup"] == "conflicting"
    assert slot_of(view)["attempts"][0]["task_id"] is None
    assert view["status"] != "satisfied"
    attempt = store.record_raised(key, "ValueError")
    assert attempt["admission"] == "uncertain"
    assert attempt["task_id"] is None
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt(view["assignment_id"], slot_of(view)["slot"], 2, R("hc:t3"))


def test_record_submitted_refuses_a_receipt_for_another_key(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: attack A5 in miniature: never bind to work this key did not start."""
    view = fanout(store, ["hc:t1", "hc:t2"])
    first, second = keys(view)
    other = receipts.add("someone-else", "completed", target="hc:default:t1")
    with pytest.raises(ValueError, match="does not belong"):
        store.record_submitted(first, other["id"])
    wrong = receipts.add(first, "completed", target="hc:default:t99")
    with pytest.raises(ValueError, match="another recipient"):
        store.record_submitted(first, wrong["id"])
    receipts.drop(wrong["id"])
    task_id = submit(store, receipts, first, "running")
    store.record_submitted(first, task_id)  # repeating the same binding is a no-op
    second_task = own(store, receipts, second, "running")
    with pytest.raises(ValueError, match="does not belong"):
        store.record_submitted(first, second_task["id"])
    with pytest.raises(ValueError, match="Unknown member key"):
        store.record_submitted("g:nothing:here", task_id)


# ---------------------------------------------------------------- planned, then submit


def test_crash_after_the_plan_row_is_found_again_by_key(tmp_path: Path) -> None:
    """red_at_pretip: the adapter reserved, the bridge died before recording; a new
    store instance finds the receipt by key. The restart then turns it `uncertain`
    (`TaskStore.recover_pending`), which reads `needs_decision`."""
    tasks = TaskStore(tmp_path / "tasks.sqlite")
    path = tmp_path / "assignments.sqlite"
    view, _ = AssignmentStore(path, tasks).create(
        "ab-crash", kind="fanout", recipients=rs(["hc:t1", "hc:t2", "hc:t3"]), message=MESSAGE
    )
    first, second, third = keys(view)
    done, _ = tasks.reserve("hc:default:t1", MESSAGE, first, {"kind": "hc"})
    AssignmentStore(path, tasks).record_submitted(first, done["id"])
    tasks.reserve("hc:default:t2", MESSAGE, second, {"kind": "hc"})  # then the crash

    restarted = AssignmentStore(path, tasks)
    view = restarted.get("ab-crash")
    assert [slot["attempts"][0]["admission"] for slot in view["slots"]] == [
        "submitted",
        "submitted",
        "planned",
    ]
    assert slot_of(view, 1)["attempts"][0]["task_id"] == tasks.by_request(second)["id"]
    assert view["status"] == "admitting"
    assert tasks.by_request(third) is None

    tasks.recover_pending()
    view = restarted.get("ab-crash")
    assert slot_of(view, 1)["status"] == "needs_decision"


def test_not_sent_member_is_planned_again_with_the_same_key(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: plan section 6: a repeat fills only `not_sent` members."""
    view = fanout(store, ["hc:t1", "hc:t2"])
    first, second = keys(view)
    store.record_raised(first, "ValueError")
    submit(store, receipts, second, "running")
    slot = slot_of(view)["slot"]
    again, created = store.plan_attempt(view["assignment_id"], slot, 1, R("hc:t1"))
    assert not created
    assert again["admission"] == "planned"
    assert again["member_key"] == first
    held, _ = store.plan_attempt(view["assignment_id"], slot_of(view, 1)["slot"], 1, R("hc:t2"))
    assert held["admission"] == "submitted"
    with pytest.raises(ValueError, match="already describes different work"):
        store.plan_attempt(view["assignment_id"], slot, 1, R("hc:t9"))


# ---------------------------------------------------------------- successors


def successor_store(store: AssignmentStore, receipts: FakeReceipts, status: str, **meta: Any):
    view = single(store, failover="never_started")
    task_id = submit(store, receipts, keys(view)[0], status)
    if meta:
        receipts.set(task_id, status, **meta)
    return view["assignment_id"]


@pytest.mark.parametrize("status", ["pending", "submitted", "running", "blocked", "uncertain"])
def test_no_successor_to_an_attempt_that_is_not_settled(
    store: AssignmentStore, receipts: FakeReceipts, status: str
) -> None:
    """red_at_pretip: attack A2: no successor to `uncertain` (or live) work, ever."""
    assignment = successor_store(store, receipts, status)
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt(assignment, "0", 2, R("hc:t9"))
    assert len(slot_of(store.get(assignment))["attempts"]) == 1


def test_no_successor_while_the_previous_attempt_is_still_planned(
    store: AssignmentStore,
) -> None:
    """red_at_pretip: a planned row may be mid-call; it must settle first."""
    view = single(store, failover="never_started")
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t9"))


def test_no_successor_after_a_permission_wait(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: attack A3: `blocked_seen` (row F1) means no successor."""
    assignment = successor_store(store, receipts, "cancelled", blocked_seen=True)
    with pytest.raises(ValueError, match="permission"):
        store.plan_attempt(assignment, "0", 2, R("hc:t9"))


def test_no_successor_to_a_satisfied_slot(store: AssignmentStore, receipts: FakeReceipts) -> None:
    """red_at_pretip"""
    assignment = successor_store(store, receipts, "completed")
    with pytest.raises(ValueError, match="no successor"):
        store.plan_attempt(assignment, "0", 2, R("hc:t9"))


def test_attempt_numbers_have_no_gaps(store: AssignmentStore, receipts: FakeReceipts) -> None:
    """red_at_pretip"""
    view, _ = store.create(
        "ab-gap",
        kind="single",
        recipients=rs(["hc:t1"]),
        message=MESSAGE,
        failover="never_started",
        max_attempts=3,
    )
    submit(store, receipts, keys(view)[0], "failed")
    with pytest.raises(ValueError, match="next attempt in this slot is 2"):
        store.plan_attempt("ab-gap", "0", 3, R("hc:t9"))
    with pytest.raises(ValueError, match="slot"):
        store.plan_attempt("ab-gap", "nope", 2, R("hc:t9"))


def test_successor_never_goes_to_a_sibling_or_the_author(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: fan-out section 4.8: not a sibling's recipient, not `independent_of`."""
    view = fanout(store, ["hc:t1", "hc:t2"], failover="never_started", independent_of="hc:author")
    first, _ = keys(view)
    store.record_raised(first, "ValueError")
    slot = slot_of(view)["slot"]
    with pytest.raises(ValueError, match="another slot"):
        store.plan_attempt(view["assignment_id"], slot, 2, R("hc:t2"))
    with pytest.raises(ValueError, match="independent_of"):
        store.plan_attempt(view["assignment_id"], slot, 2, R("hc:author"))
    attempt, created = store.plan_attempt(
        view["assignment_id"],
        slot,
        2,
        R("hc:t3"),
        reason="not_sent",
        verdict="auto",
        evidence={"lookup": "no receipt"},
    )
    assert created
    assert attempt["parent_task_id"] is None
    assert attempt["reason"] == "not_sent"
    assert attempt["verdict"] == "auto"


def run_together(calls: list[Callable[[], Any]]) -> list[Any]:
    """Run each call on its own thread, released at the same moment."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            results[index] = calls[index]()
        except Exception as error:  # the outcome under test, collected for the asserts
            results[index] = error

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_two_concurrent_calls_for_attempt_2_give_exactly_one(
    tmp_path: Path, receipts: FakeReceipts
) -> None:
    """red_at_pretip: integrated draft section 9: two connections, one attempt 2."""
    path = tmp_path / "assignments.sqlite"
    view, _ = AssignmentStore(path, receipts).create(
        "ab-race",
        kind="single",
        recipients=rs(["hc:t1"]),
        message=MESSAGE,
        failover="never_started",
    )
    first = AssignmentStore(path, receipts)
    submit(first, receipts, keys(view)[0], "failed")
    stores = [AssignmentStore(path, receipts) for _ in range(4)]
    results = run_together(
        [lambda s=s: s.plan_attempt("ab-race", "0", 2, R("hc:t2")) for s in stores]
    )
    assert all(isinstance(result, tuple) for result in results), results
    assert sorted(created for _, created in results) == [False, False, False, True]
    assert {attempt["member_key"] for attempt, _ in results} == {"ab-race.a2"}
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT attempt FROM assignment_members WHERE assignment_id='ab-race'"
        ).fetchall()
    assert sorted(rows) == [(1,), (2,)]


def test_two_concurrent_calls_naming_different_recipients_give_one_attempt_2(
    tmp_path: Path, receipts: FakeReceipts
) -> None:
    """red_at_pretip: the loser is refused, as the receipt rule refuses changed work."""
    path = tmp_path / "assignments.sqlite"
    view, _ = AssignmentStore(path, receipts).create(
        "ab-race",
        kind="single",
        recipients=rs(["hc:t1"]),
        message=MESSAGE,
        failover="never_started",
    )
    submit(AssignmentStore(path, receipts), receipts, keys(view)[0], "failed")
    left, right = AssignmentStore(path, receipts), AssignmentStore(path, receipts)
    results = run_together(
        [
            lambda: left.plan_attempt("ab-race", "0", 2, R("hc:t2")),
            lambda: right.plan_attempt("ab-race", "0", 2, R("hc:t3")),
        ]
    )
    errors = [result for result in results if isinstance(result, ValueError)]
    wins = [result for result in results if isinstance(result, tuple)]
    assert len(errors) == 1 and len(wins) == 1, results
    assert "already describes different work" in str(errors[0])
    attempts = slot_of(left.get("ab-race"))["attempts"]
    assert [attempt["attempt"] for attempt in attempts] == [1, 2]
    assert attempts[1]["peer_id"] == wins[0][0]["peer_id"]


def test_concurrent_creates_of_one_group_give_one_assignment(
    tmp_path: Path, receipts: FakeReceipts
) -> None:
    """red_at_pretip"""
    path = tmp_path / "assignments.sqlite"
    stores = [AssignmentStore(path, receipts) for _ in range(3)]
    results = run_together(
        [
            lambda s=s: s.create(
                "ab-c", kind="fanout", recipients=rs(["hc:t1", "hc:t2"]), message=MESSAGE
            )
            for s in stores
        ]
    )
    assert sorted(created for _, created in results) == [False, False, True]


# ---------------------------------------------------------------- the winner


def test_winner_is_write_once_and_a_later_completion_is_a_late_result(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: failover lane section 4.4 late-result rule, stored by compare-and-set."""
    view = single(store, failover="never_started")
    first = keys(view)[0]
    store.record_raised(first, "ValueError")  # attempt 1 never reserved
    second, _ = store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t2"))
    submit(store, receipts, second["member_key"], "completed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["winner"] == 2

    # A receipt for attempt 1's key appears later and completes as well.
    own(store, receipts, first, "completed")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["winner"] == 2
    assert slot_of(view)["late_results"] == [1]
    assert slot_of(view)["status"] == "satisfied"


def test_the_database_refuses_to_overwrite_or_add_a_winner(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: write-once is enforced by the schema, not only by the code."""
    view = fanout(store, ["hc:t1", "hc:t2"])
    submit(store, receipts, keys(view)[0], "completed")
    store.get(view["assignment_id"])
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="write-once"):
            db.execute("UPDATE assignment_members SET winner=0 WHERE winner=1")
    with sqlite3.connect(store.path) as db:
        slot = slot_of(view)["slot"]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO assignment_members (assignment_id, slot, ordinal, attempt, peer_id,"
                " target, provider, local, member_key, message_identity, admission, winner,"
                " created, updated) VALUES (?, ?, 0, 2, 'hc:t5', 'hc:default:t5', 'hc', 1,"
                " 'x.a2', '{}', 'planned', 1, 0, 0)",
                (view["assignment_id"], slot),
            )


def test_a_winner_written_by_a_concurrent_reader_is_kept(
    tmp_path: Path, receipts: FakeReceipts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: compare-and-set. Reader A has seen attempt 2 completed; before A
    writes, reader B sees attempt 1 completed and writes winner 1. A must keep B's winner."""
    path = tmp_path / "assignments.sqlite"
    store = AssignmentStore(path, receipts)
    view = single(store, failover="never_started")
    first = keys(view)[0]
    store.record_raised(first, "ValueError")
    second, _ = store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t2"))
    task_id = submit(store, receipts, second["member_key"], "completed")
    other = AssignmentStore(path, receipts)
    original_get = receipts.get

    def get_then_interleave(requested: str) -> dict[str, Any]:
        if requested == task_id and receipts.by_request(first) is None:
            receipts.add(first, "completed", target="hc:default:t1")
            assert slot_of(other.get(view["assignment_id"]))["winner"] == 1
        return original_get(requested)

    monkeypatch.setattr(receipts, "get", get_then_interleave)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["winner"] == 1
    assert slot_of(view)["late_results"] == [2]


def test_a_winner_stays_when_its_receipt_is_pruned(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """pins_existing: retention prunes the text and keeps the identity; still satisfied."""
    view = single(store)
    task_id = submit(store, receipts, keys(view)[0], "completed")
    store.get(view["assignment_id"])
    receipts.prune(task_id)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["attempts"][0]["lookup"] == "valid"
    assert slot_of(view)["status"] == "satisfied"
    assert view["status"] == "satisfied"


def test_a_winner_whose_receipt_cannot_be_read_needs_a_decision(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: changed in fix round 1 (finding 6). Round 1 kept such a slot
    `satisfied`. No code in `src/` deletes a receipt (`grep -rn "DELETE FROM tasks"` finds
    nothing), so an unreadable winner is a read failure, not history. The winner stays
    stored; the slot reads `needs_decision`."""
    view = single(store)
    task_id = submit(store, receipts, keys(view)[0], "completed")
    store.get(view["assignment_id"])
    receipts.drop(task_id)
    view = store.get(view["assignment_id"])
    assert slot_of(view)["winner"] == 1
    assert slot_of(view)["status"] == "needs_decision"
    assert view["status"] == "needs_decision"


def test_unknown_assignment_is_refused(store: AssignmentStore) -> None:
    """red_at_pretip"""
    with pytest.raises(ValueError, match="Unknown assignment"):
        store.get("ab-none")
    with pytest.raises(ValueError, match="Unknown member key"):
        store.record_raised("ab-none", "ValueError")


# ---------------------------------------------------------------- fix round 2: deadline
#
# The orchestrator's reading of PLAN revision 2 (draft section 3 column `deadline`):
# a nullable, write-once fact. Every test here is red_at_pretip: at 3a09bea `create`
# has no `deadline` and `get` no `assessment` (TypeError).


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def timed(tmp_path: Path, receipts: FakeReceipts, clock: Clock) -> AssignmentStore:
    return AssignmentStore(tmp_path / "timed" / "assignments.sqlite", receipts, clock=clock)


def test_deadline_is_stored_reported_and_part_of_the_identity(timed: AssignmentStore) -> None:
    """red_at_pretip"""
    view = single(timed, failover="never_started", deadline=2000.0)
    assert view["deadline"] == 2000.0 and view["deadline_passed"] is False
    again, created = timed.create(
        "ab-20261120-F2-single",
        kind="single",
        recipients=rs(["hc:t1"]),
        message=MESSAGE,
        failover="never_started",
        deadline=2000,
    )
    assert not created and again["deadline"] == 2000.0
    for other in (None, 3000.0):
        with pytest.raises(ValueError, match="different work"):
            timed.create(
                "ab-20261120-F2-single",
                kind="single",
                recipients=rs(["hc:t1"]),
                message=MESSAGE,
                failover="never_started",
                deadline=other,
            )
    assert timed.get("ab-20261120-F2-single")["deadline"] == 2000.0


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), -1.0, 0, "2000"])
def test_invalid_deadlines_are_refused(timed: AssignmentStore, deadline: Any) -> None:
    """red_at_pretip"""
    with pytest.raises(ValueError, match="deadline"):
        single(timed, deadline=deadline)


def test_a_deadline_in_the_past_is_refused_for_a_new_assignment(timed: AssignmentStore) -> None:
    """red_at_pretip"""
    with pytest.raises(ValueError, match="already passed"):
        single(timed, deadline=999.0)


def test_the_deadline_is_write_once_in_the_schema(timed: AssignmentStore) -> None:
    """red_at_pretip"""
    view = single(timed, deadline=2000.0)
    with closing(sqlite3.connect(timed.path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="write-once"):
            db.execute(
                "UPDATE assignments SET deadline=NULL WHERE assignment_id=?",
                (view["assignment_id"],),
            )


def test_no_successor_after_the_deadline(
    timed: AssignmentStore, receipts: FakeReceipts, clock: Clock
) -> None:
    """red_at_pretip: failover lane section 4.6, after the deadline the verdict is never;
    F2 refuses the successor itself and reads the ended slot as exhausted."""
    view = single(timed, failover="never_started", max_attempts=3, deadline=2000.0)
    submit(timed, receipts, keys(view)[0], "failed")
    assert slot_of(timed.get(view["assignment_id"]))["status"] == "needs_decision"
    clock.now = 2000.0
    view = timed.get(view["assignment_id"])
    assert view["deadline_passed"] is True
    assert slot_of(view)["status"] == "exhausted"
    assert view["status"] == "unsatisfied"
    with pytest.raises(ValueError, match="deadline"):
        timed.plan_attempt(view["assignment_id"], "0", 2, R("hc:t2"))


def test_a_not_sent_successor_is_not_planned_again_after_the_deadline(
    timed: AssignmentStore, receipts: FakeReceipts, clock: Clock
) -> None:
    """red_at_pretip: re-planning a successor makes it sendable, so the deadline applies;
    attempt 1 is not a successor."""
    view = fanout(timed, ["hc:t1", "hc:t2"], failover="never_started", deadline=2000.0)
    first, _ = keys(view)
    slot = slot_of(view)["slot"]
    submit(timed, receipts, first, "failed")
    second, _ = timed.plan_attempt(view["assignment_id"], slot, 2, R("hc:t3"))
    timed.record_raised(second["member_key"], "ValueError")
    timed.record_raised(keys(view)[1], "ValueError")
    clock.now = 2500.0
    with pytest.raises(ValueError, match="deadline"):
        timed.plan_attempt(view["assignment_id"], slot, 2, R("hc:t3"))
    again, _ = timed.plan_attempt(view["assignment_id"], slot_of(view, 1)["slot"], 1, R("hc:t2"))
    assert again["admission"] == "planned"


# ---------------------------------------------------------------- fix round 2: assessment
#
# The orchestrator's reading of PLAN revision 2 (draft section 3 tables): `get` takes an
# optional per-attempt `assessment`. These tests drive every row that needs a fact F2
# cannot read, with the fact present, absent and stale.


def facts(view: dict[str, Any], index: int = 0, **values: Any) -> dict[str, Any]:
    attempt = slot_of(view, index)["attempts"][-1]
    return {attempt["member_key"]: {"task_id": attempt["task_id"], **values}}


def test_core_blocked_reads_blocked_and_blocks_the_assignment(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: slot row `blocked`, "or Core reports blocked for the terminal"."""
    view = fanout(store, ["hc:t1", "hc:t2", "hc:t3"], quorum=2)
    for key in keys(view):
        submit(store, receipts, key, "submitted")
    view = store.get(view["assignment_id"])
    view = store.get(view["assignment_id"], assessment=facts(view, 2, core_blocked=True))
    assert slot_of(view, 2)["status"] == "blocked"
    assert view["status"] == "blocked"


def test_stalled_reads_needs_decision(store: AssignmentStore, receipts: FakeReceipts) -> None:
    """red_at_pretip: slot row `needs_decision`, "stalled"."""
    view = single(store)
    submit(store, receipts, keys(view)[0], "running")
    view = store.get(view["assignment_id"])
    view = store.get(
        view["assignment_id"], assessment=facts(view, stalled=True, core_blocked=False)
    )
    assert slot_of(view)["status"] == "needs_decision"
    assert view["status"] == "needs_decision"


def test_active_reports_the_facts_it_could_not_check(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: without an assessment a live slot says what it did not know.
    Changed in fix round 3 (N2): round 2 read such a slot `active`; now `needs_decision`."""
    view = single(store)
    submit(store, receipts, keys(view)[0], "running")
    view = store.get(view["assignment_id"])
    assert slot_of(view)["status"] == "needs_decision"
    assert slot_of(view)["assessment_absent"] == ["core_blocked", "stalled"]
    assert slot_of(view)["blocked_unknown"] is True
    view = store.get(
        view["assignment_id"], assessment=facts(view, core_blocked=False, stalled=False)
    )
    assert slot_of(view)["status"] == "active"
    assert slot_of(view)["assessment_absent"] == []


@pytest.mark.parametrize(
    ("given", "expected", "absent", "eligible"),
    [
        ({}, "needs_decision", ["verdict", "no_candidate"], False),
        ({"verdict": "never"}, "needs_decision", ["no_candidate"], False),
        ({"verdict": "never", "no_candidate": False}, "needs_decision", [], False),
        ({"verdict": "auto"}, "needs_decision", ["no_candidate"], False),
        ({"verdict": "auto", "no_candidate": False}, "needs_decision", [], True),
        ({"no_candidate": True}, "exhausted", [], False),
        ({"verdict": "auto", "no_candidate": True}, "exhausted", [], False),
    ],
)
def test_ended_attempt_with_an_attempt_left_reads_by_verdict_and_candidates(
    store: AssignmentStore,
    receipts: FakeReceipts,
    given: dict[str, Any],
    expected: str,
    absent: list[str],
    eligible: bool,
) -> None:
    """red_at_pretip: slot rows `needs_decision` ("ended with a verdict other than auto")
    and `exhausted` ("no candidate left within policy"). Changed in fix round 3 (N3):
    round 2 read `auto` with a candidate as `active`, although no attempt was running;
    now the slot stays `needs_decision` and reports `retry_eligible` separately."""
    view = single(store, failover="never_started")
    submit(store, receipts, keys(view)[0], "failed")
    view = store.get(view["assignment_id"])
    view = store.get(view["assignment_id"], assessment=facts(view, **given))
    assert slot_of(view)["status"] == expected
    assert slot_of(view)["assessment_absent"] == absent
    assert slot_of(view)["retry_eligible"] is eligible
    assert (
        view["status"] == {"needs_decision": "needs_decision", "exhausted": "unsatisfied"}[expected]
    )


def test_a_stale_assessment_is_ignored(store: AssignmentStore, receipts: FakeReceipts) -> None:
    """red_at_pretip: facts about another receipt are never permission."""
    view = single(store, failover="never_started")
    submit(store, receipts, keys(view)[0], "failed")
    key = keys(view)[0]
    stale = {key: {"task_id": "another-receipt", "verdict": "auto", "no_candidate": True}}
    view = store.get(view["assignment_id"], assessment=stale)
    assert slot_of(view)["assessment_stale"] is True
    assert slot_of(view)["status"] == "needs_decision"
    assert slot_of(view)["assessment_absent"] == ["verdict", "no_candidate"]


def test_an_assessment_never_releases_an_uncertain_admission(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip"""
    view = single(store, failover="never_started")
    key = keys(view)[0]
    own(store, receipts, key, "failed")
    store.record_raised(key, "RuntimeError")
    view = store.get(view["assignment_id"])
    given = facts(view, verdict="auto", no_candidate=False, core_blocked=False, stalled=False)
    assert slot_of(store.get(view["assignment_id"], assessment=given))["status"] == (
        "needs_decision"
    )


def test_an_earlier_ended_attempts_affirmative_block_is_a_contradiction(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: rewrite c3 retains an ended predecessor's affirmative Core block."""
    view = single(store, failover="never_started", max_attempts=3)
    first = keys(view)[0]
    first_task = submit(store, receipts, first, "failed")
    second, _ = store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t2"))
    submit(store, receipts, second["member_key"], "running")
    old = {first: {"task_id": first_task, "core_blocked": True}}
    read = slot_of(store.get(view["assignment_id"], assessment=old))
    assert read["status"] == "blocked"
    assert read["assessment_absent"] == []  # latest compatibility field on a blocked result
    assert read["assessment_absent_attempts"] == [
        {"attempt": 2, "member_key": second["member_key"], "facts": ["core_blocked"]}
    ]
    assert read["assessment_contradiction_attempts"] == [
        {"attempt": 1, "member_key": first, "facts": ["core_blocked"]}
    ]


@pytest.mark.parametrize(
    ("given", "match"),
    [
        ({"g:nothing:here": {"task_id": None}}, "unknown member keys"),
        ({"KEY": {}}, "task_id"),
        ({"KEY": {"task_id": None, "core_blocked": "yes"}}, "true or false"),
        ({"KEY": {"task_id": None, "verdict": "AUTO"}}, "verdict"),
        ({"KEY": {"task_id": None, "core_status": "blocked"}}, "unknown facts"),
        ({"KEY": {"task_id": 7}}, "task_id"),
    ],
)
def test_malformed_assessments_are_refused(
    store: AssignmentStore, given: dict[str, Any], match: str
) -> None:
    """red_at_pretip"""
    view = single(store)
    key = keys(view)[0]
    given = {key if name == "KEY" else name: value for name, value in given.items()}
    with pytest.raises(ValueError, match=match):
        store.get(view["assignment_id"], assessment=given)


GOOD_FACT = {"task_id": None}


class ItemsOnly:
    """Iterates and has `items`, but is no `Mapping` (review 1 of T-107, B3: its `Duck`)."""

    def __iter__(self) -> Any:
        return iter(())

    def items(self) -> Any:
        return {}.items()


class NotText:
    """A hashable key of a type that the module does not know (fix round 3 of T-107)."""


class TextKey(str):
    """A subclass of `str`: still text (fix round 3 of T-107)."""

    __slots__ = ()


@pytest.mark.parametrize(
    ("given", "match"),
    [
        # pins_existing: still accepted; `None` and an empty mapping are "no assessment".
        pytest.param(None, None, id="None"),
        pytest.param({}, None, id="empty-mapping"),
        pytest.param({"KEY": GOOD_FACT}, None, id="good"),
        pytest.param(MappingProxyType({"KEY": GOOD_FACT}), None, id="good-mapping-proxy"),
        # red_at_pretip: falsy values that are not mappings were taken as "no assessment".
        pytest.param([], "must be a mapping", id="empty-list"),
        pytest.param("", "must be a mapping", id="empty-text"),
        pytest.param(0, "must be a mapping", id="zero"),
        pytest.param(False, "must be a mapping", id="false"),
        # red_at_pretip: other values that are not mappings raised AttributeError.
        pytest.param([1], "must be a mapping", id="list-of-int"),
        pytest.param(["KEY"], "must be a mapping", id="list-of-key"),
        pytest.param("KEY", "must be a mapping", id="text"),
        pytest.param(7, "must be a mapping", id="int"),
        # red_at_pretip: unknown keys of mixed types raised TypeError from sorted().
        pytest.param({1: GOOD_FACT, "zz": GOOD_FACT}, "member keys must be text", id="mixed"),
        # red_at_pretip (review 1, B2): a text key first, then one that is not text. At
        # d98fba8 it raised "Assessment names unknown member keys: [1]" after the read.
        pytest.param({"KEY": GOOD_FACT, 1: GOOD_FACT}, "member keys must be text", id="text-first"),
        # red_at_pretip (review 1, B3): no Mapping, though it has `items`; accepted at
        # d98fba8 as "no assessment".
        pytest.param(ItemsOnly(), "must be a mapping", id="items-not-mapping"),
        # red_at_pretip: one key that is not text raised "Assessment names unknown member
        # keys: [1]" (or "[None]") after the rows were read; now refused before any read.
        pytest.param({1: GOOD_FACT}, "member keys must be text", id="int-key"),
        pytest.param({None: GOOD_FACT}, "member keys must be text", id="none-key"),
        # red_at_pretip (review 2 of T-107, R2): a byte-string key is not text. The value is
        # the reviewer's (`in/T-107-review-codex-2.md`, R2); at d98fba8 it raised "Assessment
        # names unknown member keys: [b'a']" after the read. The whole message is matched.
        pytest.param(
            {b"a": {"task_id": None}}, "^Assessment member keys must be text$", id="bytes-key"
        ),
        # red_at_pretip (fix round 3 of T-107, the table of key types): further hashable
        # values that are not text, one per type that a widened key check could let through.
        # The values are this round's own (no bridge code builds an assessment with such a
        # key; a caller's JSON-like mapping can). At d98fba8 each raised "Assessment names
        # unknown member keys: [...]" after the read. The whole message is matched.
        pytest.param({True: GOOD_FACT}, "^Assessment member keys must be text$", id="bool-key"),
        pytest.param({1.5: GOOD_FACT}, "^Assessment member keys must be text$", id="float-key"),
        pytest.param({("a",): GOOD_FACT}, "^Assessment member keys must be text$", id="tuple-key"),
        pytest.param(
            {frozenset({"a"}): GOOD_FACT},
            "^Assessment member keys must be text$",
            id="frozenset-key",
        ),
        pytest.param(
            {NotText(): GOOD_FACT}, "^Assessment member keys must be text$", id="object-key"
        ),
        # pins_existing (fix round 3 of T-107): a key of a subclass of `str` is text and
        # names the attempt; a key check narrowed to `type(key) is str` would refuse it.
        pytest.param({TextKey("KEY"): GOOD_FACT}, None, id="good-str-subclass"),
        pytest.param({"zz": GOOD_FACT}, "unknown member keys", id="unknown-key"),
        # pins_existing (review 3 of T-107, B2): a text key is taken as it is given. The
        # existing member key with a space on each side names no member, and neither does
        # the empty text: both are text, so both pass the key check and are refused as
        # unknown member keys, with the key as given in the message, after the one read of
        # the rows. A key check that strips a key would take the first for the member; one
        # that asks for a text that is not empty would refuse the second before the read.
        pytest.param(
            {" KEY ": GOOD_FACT},
            r"^Assessment names unknown member keys: \[' KEY '\]$",
            id="padded-member-key",
        ),
        pytest.param(
            {"": GOOD_FACT},
            r"^Assessment names unknown member keys: \[''\]$",
            id="empty-text-key",
        ),
        pytest.param({"KEY": []}, "must be a mapping with task_id", id="fact-not-mapping"),
    ],
)
def test_the_outer_assessment_is_null_or_a_mapping_with_text_keys(
    store: AssignmentStore, given: Any, match: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 7, N-4 (row T-107); each case says its label. The values are those of
    `in/t107_probe.py` part 2; `KEY` stands for the attempt's member key."""
    view = single(store)
    key = keys(view)[0]
    original = given
    if isinstance(given, Mapping):
        # `type(name)(...)` keeps the key's own type: `str` or the subclass `TextKey`.
        # `KEY` inside a text key stands for the member key (`" KEY "`: a space on each side).
        given = type(given)(
            {
                type(name)(name.replace("KEY", key)) if isinstance(name, str) else name: value
                for name, value in given.items()
            }
        )
        assert [type(name) for name in given] == [type(name) for name in original]
    elif isinstance(given, list):
        given = [key if name == "KEY" else name for name in given]
    elif given == "KEY":
        given = key
    if match is None:
        assert store.get(view["assignment_id"], assessment=given)["assignment_id"]
        return
    reads: list[bool] = []
    if "unknown member keys" not in match:
        # Refused before the store is read: neither the database nor a receipt is touched.
        # (Only the unknown-key check needs the rows, so it runs after the read.)
        def untouched(*_: Any, **__: Any) -> Any:
            raise AssertionError("the store was read before the assessment was checked")

        monkeypatch.setattr(store, "_connection", untouched)
        monkeypatch.setattr(store.receipts, "by_request", untouched)
        monkeypatch.setattr(store.receipts, "get", untouched)
    else:
        connection = store._connection

        def counted() -> Any:
            reads.append(True)
            return connection()

        monkeypatch.setattr(store, "_connection", counted)
    with pytest.raises(ValueError, match=match.replace("KEY", re.escape(key))):
        store.get(view["assignment_id"], assessment=given)
    if "unknown member keys" in match:
        assert reads == [True]  # refused after one read of the rows, not before it


# ---------------------------------------------------------------- fix round 3: N2
#
# Every assessment fact against both status tables, in four cases: supplied, missing,
# stale (another task ID) and contradictory (the receipt's own state rules it out).
# Slot 0 has completed; slot 1 is the subject. `quorum=1` checks that an unknown Core
# state is not hidden behind a met quorum; `all` checks the plain table.

LIVE_FALSE = {"core_blocked": False, "stalled": False}
N2_CASES = [
    # (fact, case, receipt status, facts, stale, slot, absent, contradictions, unknown)
    (
        "core_blocked",
        "supplied",
        "running",
        {"core_blocked": True},
        False,
        "blocked",
        [],
        [],
        False,
    ),
    ("core_blocked", "supplied_false", "running", LIVE_FALSE, False, "active", [], [], False),
    (
        "core_blocked",
        "missing",
        "running",
        {"stalled": False},
        False,
        "needs_decision",
        ["core_blocked"],
        [],
        True,
    ),
    (
        "core_blocked",
        "stale",
        "running",
        {"core_blocked": True, "stalled": False},
        True,
        "needs_decision",
        ["core_blocked", "stalled"],
        [],
        True,
    ),
    # Fix round 4 (R4-1): a supplied Core block is never lost; it reads blocked and is
    # still named as a contradiction on a final receipt.
    (
        "core_blocked",
        "contradictory",
        "failed",
        {"core_blocked": True, "verdict": "never", "no_candidate": False},
        False,
        "blocked",
        [],
        ["core_blocked"],
        False,
    ),
    (
        "core_blocked",
        "contradictory_blocked",
        "blocked",
        {"core_blocked": False},
        False,
        "blocked",
        [],
        ["core_blocked"],
        False,
    ),
    (
        "stalled",
        "supplied",
        "running",
        {"core_blocked": False, "stalled": True},
        False,
        "needs_decision",
        [],
        [],
        False,
    ),
    (
        "stalled",
        "missing",
        "running",
        {"core_blocked": False},
        False,
        "needs_decision",
        ["stalled"],
        [],
        False,
    ),
    (
        "stalled",
        "stale",
        "running",
        {"core_blocked": False, "stalled": True},
        True,
        "needs_decision",
        ["core_blocked", "stalled"],
        [],
        True,
    ),
    (
        "stalled",
        "contradictory",
        "failed",
        {"stalled": True, "verdict": "never", "no_candidate": False},
        False,
        "needs_decision",
        [],
        ["stalled"],
        False,
    ),
    (
        "verdict",
        "supplied",
        "failed",
        {"verdict": "never", "no_candidate": False},
        False,
        "needs_decision",
        [],
        [],
        False,
    ),
    (
        "verdict",
        "missing",
        "failed",
        {"no_candidate": False},
        False,
        "needs_decision",
        ["verdict"],
        [],
        False,
    ),
    (
        "verdict",
        "stale",
        "failed",
        {"verdict": "never", "no_candidate": False},
        True,
        "needs_decision",
        ["verdict", "no_candidate"],
        [],
        False,
    ),
    # Fix round 4 (R4-2): inapplicable on live work, not contradictory.
    (
        "verdict",
        "inapplicable",
        "running",
        {"verdict": "never", **LIVE_FALSE},
        False,
        "active",
        [],
        [],
        False,
    ),
    (
        "no_candidate",
        "supplied",
        "failed",
        {"no_candidate": True},
        False,
        "exhausted",
        [],
        [],
        False,
    ),
    (
        "no_candidate",
        "missing",
        "failed",
        {"verdict": "never"},
        False,
        "needs_decision",
        ["no_candidate"],
        [],
        False,
    ),
    (
        "no_candidate",
        "stale",
        "failed",
        {"no_candidate": True},
        True,
        "needs_decision",
        ["verdict", "no_candidate"],
        [],
        False,
    ),
    # Fix round 4 (R4-2): inapplicable on live work, not contradictory.
    (
        "no_candidate",
        "inapplicable",
        "running",
        {"no_candidate": True, **LIVE_FALSE},
        False,
        "active",
        [],
        [],
        False,
    ),
]
ASSIGNMENT_FOR = {
    # slot 1 status (and whether its Core state is unknown) -> (quorum 1 of 2, all of 2)
    ("blocked", False): ("blocked", "blocked"),
    ("active", False): ("satisfied", "active"),
    ("needs_decision", False): ("satisfied", "needs_decision"),
    ("needs_decision", True): ("needs_decision", "needs_decision"),
    ("exhausted", False): ("satisfied", "unsatisfied"),
}


@pytest.mark.parametrize("quorum", [1, None])
@pytest.mark.parametrize(
    ("fact", "case", "status", "given", "stale", "slot", "absent", "contra", "unknown"),
    N2_CASES,
    ids=[f"{case[0]}-{case[1]}" for case in N2_CASES],
)
def test_every_assessment_fact_in_every_case_against_both_tables(
    store: AssignmentStore,
    receipts: FakeReceipts,
    quorum: int | None,
    fact: str,
    case: str,
    status: str,
    given: dict[str, Any],
    stale: bool,
    slot: str,
    absent: list[str],
    contra: list[str],
    unknown: bool,
) -> None:
    """red_at_pretip: N2. Missing or stale is unknown and never permission; contradictory
    facts read the more careful state and are named."""
    view = fanout(store, ["hc:t1", "hc:t2"], quorum=quorum, failover="never_started")
    done, subject = keys(view)
    submit(store, receipts, done, "completed")
    task_id = submit(store, receipts, subject, status)
    supplied = {subject: {"task_id": "another-receipt" if stale else task_id, **given}}
    view = store.get(view["assignment_id"], assessment=supplied)
    read = slot_of(view, 1)
    assert (read["status"], read["assessment_absent"]) == (slot, absent)
    assert read["assessment_contradictions"] == contra
    assert read["assessment_stale"] is stale
    assert read["blocked_unknown"] is unknown
    expected = ASSIGNMENT_FOR[(slot, unknown)][0 if quorum == 1 else 1]
    assert view["status"] == expected


def test_a_met_quorum_does_not_hide_an_unknown_core_state(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: N2, the reviewer's quorum case with no assessment at all."""
    view = fanout(store, ["hc:t1", "hc:t2"], quorum=1)
    done, live = keys(view)
    submit(store, receipts, done, "completed")
    submit(store, receipts, live, "submitted")
    view = store.get(view["assignment_id"])
    assert view["status"] == "needs_decision"
    assert clear(store, view["assignment_id"])["status"] == "satisfied"


# ---------------------------------------------------------------- fix round 4: R4-1, R4-2
#
# The full combination table: every receipt state of the subject attempt x Core blocked
# (true, false, missing, stale) x stalled (true, false, missing) x candidates (missing,
# auto with a candidate left, none left) x quorum met by the other slot or not. The
# expected reading comes from `table_oracle`, written from the draft's two tables
# (integrated draft section 3) and the plan's rules, not from the module's code:
#
# - "Core reports blocked" and a blocked receipt read `blocked`, and `blocked` comes before
#   `satisfied` in the assignment table, so a supplied Core block is never lost.
# - A live fact (Core blocked, stalled) applies to every receipt that is not known final
#   and to every unreadable one; it cannot apply to an attempt with no receipt (planned or
#   not sent) or to a final receipt. Such a fact is a contradiction there and is named.
# - An applicable Core state that was not supplied (missing or stale) is unknown: the slot
#   is `blocked_unknown` and the assignment cannot read `satisfied` or `active` past it.
# - Verdict and candidates apply to an ended attempt and to a `not_sent` one (plan
#   section 6, proof 1); elsewhere they are inapplicable and ignored, never contradictions.
# - `active` needs a live receipt with Core and stalled both known false.

R4_STATES = [
    "planned",
    "not_sent",
    "pending",
    "queued",
    "submitted",
    "running",
    "blocked",
    "uncertain",
    "future_state",
    "completed",
    "failed",
    "cancelled",
    "abandoned",
    "raised_pending",
    "unavailable",
    "conflicting",
    "gone",
    "remote_miss",
    "raised_failed",
    "failed_off",
    "invalidated_winner",
]
R4_LIVE = ("pending", "queued", "submitted", "running")
R4_ENDED = ("failed", "cancelled", "abandoned")
R4_CORE = ("true", "false", "missing", "stale")
R4_STALLED = ("true", "false", "missing")
R4_CANDIDATES = ("missing", "auto_left", "none_left")


def table_oracle(state: str, core: str, stalled: str, cand: str, quorum: int | None) -> dict:
    """The expected reading, from the draft's tables (see the comment above)."""
    stale = core == "stale"
    core_v = None if stale or core == "missing" else core == "true"
    stalled_v = None if stale or stalled == "missing" else stalled == "true"
    cand_given = not stale and cand != "missing"
    final = state in ("completed", "failed_off", *R4_ENDED)
    unsent = state in ("planned", "not_sent")
    applies = not final and not unsent
    known_block = state == "blocked" or core_v is True
    contradictions = []
    core_applies = applies or state == "abandoned"  # rewrite c4; stall keeps its own rule
    if core_v is True and not core_applies:
        contradictions.append("core_blocked")
    if core_v is False and state == "blocked":
        contradictions.append("core_blocked")
    if stalled_v is True and not applies:
        contradictions.append("stalled")
    unknown = core_applies and not known_block and core_v is None
    eligible = False
    if known_block:
        slot = "blocked"
    elif state == "completed":
        slot = "needs_decision" if contradictions else "satisfied"
    elif contradictions:
        slot = "needs_decision"
    elif state in ("planned", "not_sent"):
        slot = state
        eligible = state == "not_sent" and cand_given and cand == "auto_left"
    elif state in R4_LIVE:
        slot = "active" if core_v is False and stalled_v is False else "needs_decision"
    elif state == "failed_off":
        slot = "exhausted"  # failover off: attempt 1 was the last one
    elif state in R4_ENDED:
        if cand_given and cand == "none_left":
            slot = "exhausted"
        else:
            slot = "needs_decision"
            eligible = cand_given and cand == "auto_left"
    else:
        # uncertain, raised after reserving (live or final receipt), unknown, unreadable,
        # a remote miss, an invalidated winner
        slot = "needs_decision"
    # Assignment table, in order; the other slot has completed.
    satisfied = 1 + (slot == "satisfied")
    active = slot == "active"
    need = 1 if quorum == 1 else 2
    if slot == "planned":
        group = "admitting"
    elif slot == "blocked":
        group = "blocked"
    elif unknown:
        group = "needs_decision"
    elif satisfied >= need:
        group = "satisfied"
    elif satisfied + active >= need:
        group = "active"
    elif slot in ("needs_decision", "not_sent"):
        group = "needs_decision"
    else:
        group = "unsatisfied"
    return {
        "slot": slot,
        "group": group,
        "blocked_unknown": unknown,
        "contradictions": contradictions,
        "retry_eligible": eligible,
    }


def r4_subject(store: AssignmentStore, receipts: FakeReceipts, state: str, quorum: int | None):
    remote = Recipient("node:other-host/hc:t2", "node:other-host/hc:t2", "node", False)
    view, _ = store.create(
        "ab-20261120-F2-review",
        kind="fanout",
        recipients=[R("hc:t1"), remote if state == "remote_miss" else R("hc:t2")],
        message=MESSAGE,
        quorum=quorum,
        failover="off" if state == "failed_off" else "never_started",
        max_attempts=3,
    )
    done, key = keys(view)
    submit(store, receipts, done, "completed")
    task_id: str | None = None
    if state == "not_sent":
        store.record_raised(key, "AdmissionRefused")
    elif state == "raised_pending":
        task_id = own(store, receipts, key, "pending")["id"]
        store.record_raised(key, "RuntimeError")
    elif state in ("unavailable", "conflicting", "gone"):
        task_id = submit(store, receipts, key, "submitted")
        if state == "conflicting":
            receipts.rows[task_id]["message"] = "other work"
        elif state == "gone":
            receipts.drop(task_id)
        else:
            receipts.errors = {task_id}
    elif state == "remote_miss":
        store.record_raised(key, "TransportUnavailable")  # a remote miss is no proof
    elif state == "raised_failed":
        task_id = own(store, receipts, key, "failed")["id"]
        store.record_raised(key, "RuntimeError")
    elif state == "failed_off":
        task_id = submit(store, receipts, key, "failed")
    elif state == "invalidated_winner":
        task_id = submit(store, receipts, key, "completed")
        store.get(view["assignment_id"])  # stores the winner
        receipts.set(task_id, "uncertain", identity_invalidated=True)
    elif state == "future_state":
        task_id = submit(store, receipts, key, "submitted")
        receipts.rows[task_id]["status"] = "future_state"
    elif state != "planned":
        task_id = submit(store, receipts, key, state)
    return view["assignment_id"], key, task_id


def r4_facts(task_id: str | None, core: str, stalled: str, cand: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"task_id": "another-receipt" if core == "stale" else task_id}
    if core in ("true", "false"):
        facts["core_blocked"] = core == "true"
    elif core == "stale":
        facts["core_blocked"] = True
    if stalled != "missing":
        facts["stalled"] = stalled == "true"
    if cand == "auto_left":
        facts.update(verdict="auto", no_candidate=False)
    elif cand == "none_left":
        facts["no_candidate"] = True
    return facts


@pytest.mark.parametrize("quorum", [1, None], ids=["quorum_met", "all"])
@pytest.mark.parametrize("state", R4_STATES)
def test_every_combination_of_receipt_core_stall_candidates_and_quorum(
    store: AssignmentStore, receipts: FakeReceipts, state: str, quorum: int | None
) -> None:
    """red_at_pretip: R4-1 and R4-2. The review's acceptance sentence: "The new regression
    cases need to test those combinations, not just each check in isolation." 36 fact
    combinations per receipt state and quorum, 1512 readings in all."""
    assignment_id, key, task_id = r4_subject(store, receipts, state, quorum)
    wrong = []
    for core in R4_CORE:
        for stalled in R4_STALLED:
            for cand in R4_CANDIDATES:
                view = store.get(
                    assignment_id, assessment={key: r4_facts(task_id, core, stalled, cand)}
                )
                read = slot_of(view, 1)
                got = {
                    "slot": read["status"],
                    "group": view["status"],
                    "blocked_unknown": read["blocked_unknown"],
                    "contradictions": read["assessment_contradictions"],
                    "retry_eligible": read["retry_eligible"],
                }
                expected = table_oracle(state, core, stalled, cand, quorum)
                if got != expected:
                    wrong.append(((core, stalled, cand), got, expected))
    assert wrong == []


@pytest.mark.parametrize("state", R4_STATES)
def test_a_plain_read_never_hides_an_applicable_core_state(
    store: AssignmentStore, receipts: FakeReceipts, state: str
) -> None:
    """red_at_pretip: R4-1 without any assessment: wherever a Core state could apply, a
    met quorum does not read satisfied."""
    assignment_id, _, _ = r4_subject(store, receipts, state, 1)
    view = store.get(assignment_id)
    expected = table_oracle(state, "missing", "missing", "missing", 1)
    assert (view["status"], slot_of(view, 1)["blocked_unknown"]) == (
        expected["group"],
        expected["blocked_unknown"],
    )


def test_a_late_live_attempt_in_a_won_slot_carries_its_core_state(
    store: AssignmentStore, receipts: FakeReceipts
) -> None:
    """red_at_pretip: R4-1 on the satisfied path. Attempt 1 won; attempt 2 still runs.
    Its Core block reads blocked, its unknown Core state keeps the group open, and
    known-clear facts let it read satisfied."""
    view = single(store, failover="never_started")
    first = keys(view)[0]
    store.record_raised(first, "AdmissionRefused")
    second, _ = store.plan_attempt(view["assignment_id"], "0", 2, R("hc:t2"))
    task = submit(store, receipts, second["member_key"], "running")
    own(store, receipts, first, "completed")
    key = second["member_key"]
    plain = store.get(view["assignment_id"])
    assert slot_of(plain)["blocked_unknown"] is True and plain["status"] == "needs_decision"
    blocked = store.get(
        view["assignment_id"], assessment={key: {"task_id": task, "core_blocked": True}}
    )
    assert blocked["status"] == "blocked"
    clear_ = store.get(
        view["assignment_id"],
        assessment={key: {"task_id": task, "core_blocked": False, "stalled": False}},
    )
    # Rewrite c2: the earlier late completion has uncertain admission too.
    assert clear_["status"] == "needs_decision" and slot_of(clear_)["winner"] == 1
    first_item = slot_of(clear_)["attempts"][0]
    assert (
        store.get(
            view["assignment_id"],
            assessment={
                first: {"task_id": first_item["task_id"], "core_blocked": False},
                key: {"task_id": task, "core_blocked": False, "stalled": False},
            },
        )["status"]
        == "satisfied"
    )


# ---------------------------------------------------------------- fix round 5: R5-1
#
# One more dimension: the position of the unresolved attempt in its slot (older, middle,
# latest) beside a completed or a clear-running other attempt. The expected reading is the
# single-attempt oracle's logic (`table_oracle`) applied to every attempt that may still
# hold its terminal, plus the draft's sentence "A blocked receipt makes the slot and the
# assignment blocked" (integrated draft section 3): blocking and an unchecked Core state
# of any such attempt count; execution state, verdict, candidates and contradictions
# come from the latest attempt; a completion anywhere satisfies the slot. Rewrite c3:
# every affirmative Core block counts; ended history also names its contradiction.

R5_SUBJECTS = (
    "blocked",
    "running",
    "uncertain",
    "future_state",
    "unavailable",
    "conflicting",
    "gone",
    "completed",
    "failed",
)
R5_POSITIONS = ("older", "middle", "latest")
R5_OTHER = ("completed", "running")


def r5_oracle(subject: str, core: str, position: str, other: str) -> tuple[str, str, bool]:
    """(slot, assignment, blocked_unknown) for a single assignment, from the draft."""
    final = subject in ("completed", "failed")
    applies = not final
    latest = position == "latest"
    core_true = core == "true"
    blocked = subject == "blocked" or core_true
    unknown = applies and not blocked and core in ("missing", "stale")
    contradictions = latest and (
        (core_true and not applies) or (core == "false" and subject == "blocked")
    )
    if blocked:
        slot = "blocked"
    elif contradictions:
        slot = "needs_decision"
    elif subject == "completed" or other == "completed":
        slot = "satisfied"
    elif not latest:
        slot = "active"  # the latest attempt is the clear-running other one
    elif subject == "running" and core == "false":
        slot = "active"
    else:
        slot = "needs_decision"
    if slot == "blocked":
        group = "blocked"
    elif unknown:
        group = "needs_decision"
    else:
        group = {"needs_decision": "needs_decision", "satisfied": "satisfied", "active": "active"}[
            slot
        ]
    return slot, group, unknown


def r5_slot(
    store: AssignmentStore, receipts: FakeReceipts, subject: str, position: str, other: str
):
    """Build the slot through the public API; return the assessment for its attempts."""
    view = single(store, failover="never_started", max_attempts=3)
    aid = view["assignment_id"]
    count = 3 if position == "middle" else 2
    tasks = []
    for n in range(1, count + 1):
        key = keys(view)[0] if n == 1 else f"{keys(view)[0]}.a{n}"
        if n > 1:
            store.plan_attempt(aid, "0", n, R(f"hc:t{n}"))
        tasks.append((key, submit(store, receipts, key, "failed")))
    subject_index = {"older": 0, "middle": 1, "latest": count - 1}[position]
    other_index = 0 if position == "latest" else count - 1
    facts: dict[str, Any] = {}
    key, task = tasks[other_index]
    receipts.set(task, other)
    if other == "running":
        facts[key] = {"task_id": task, "core_blocked": False, "stalled": False}
    return aid, tasks[subject_index], facts


@pytest.mark.parametrize("other", R5_OTHER)
@pytest.mark.parametrize("position", R5_POSITIONS)
@pytest.mark.parametrize("subject", R5_SUBJECTS)
def test_every_position_of_an_unresolved_attempt_in_its_slot(
    store: AssignmentStore, receipts: FakeReceipts, subject: str, position: str, other: str
) -> None:
    """red_at_pretip: R5-1. 9 subjects x 3 positions x 2 other attempts x 4 Core facts."""
    aid, (key, task), base = r5_slot(store, receipts, subject, position, other)
    if subject in ("completed", "failed", "blocked", "running", "uncertain", "future_state"):
        receipts.set(task, "submitted" if subject == "future_state" else subject)
        if subject == "future_state":
            receipts.rows[task]["status"] = "future_state"
    elif subject == "unavailable":
        receipts.errors = {task}
    elif subject == "conflicting":
        receipts.rows[task]["message"] = "other work"
    elif subject == "gone":
        receipts.drop(task)
    wrong = []
    for core in ("true", "false", "missing", "stale"):
        facts = dict(base)
        if core != "missing":
            facts[key] = {
                "task_id": "another-receipt" if core == "stale" else task,
                "core_blocked": core in ("true", "stale"),
                "stalled": False,
            }
        view = store.get(aid, assessment=facts)
        got = (slot_of(view)["status"], view["status"], slot_of(view)["blocked_unknown"])
        expected = r5_oracle(subject, core, position, other)
        if got != expected:
            wrong.append((core, got, expected))
    assert wrong == []


# ---------------------------------------------------------------- rewrite after review 6


def rewrite_profiles() -> list[dict[str, Any]]:
    """All status-relevant _attempt_view shapes, including defensive lookup=None.

    Schema: four admissions, optional task ID, boolean winner. _attempt_view hides
    status/invalidated on every non-valid lookup; valid receipts expose tasks.STATES,
    missing status or an unknown state, and the boolean metadata flag. Arbitrary
    unknown JSON states share the unknown-state branch. Other projected fields are
    identifiers/labels, not status inputs. These 448 shapes are a finite quotient of
    the input domain, not a claim that all combinations can occur in a public get.
    """
    profiles = []
    seen_values = [
        None,
        *(assignments._Seen(name) for name in ("absent", "conflicting", "unavailable")),
    ]
    seen_values += [
        assignments._Seen(
            "valid",
            {"status": status, "metadata": {"identity_invalidated": invalidated}},
        )
        for status, invalidated in itertools.product((*STATES, None, "future_state"), (False, True))
    ]
    for admission, bound, winner, seen in itertools.product(
        ("planned", "submitted", "not_sent", "uncertain"),
        (False, True),
        (False, True),
        seen_values,
    ):
        row = dict.fromkeys(("parent_task_id", "reason", "verdict", "evidence", "detail"), None)
        row.update(
            assignment_id="profile",
            slot="0",
            attempt=1,
            peer_id="hc:t1",
            target="hc:default:t1",
            provider="hc",
            local=True,
            member_key="profile",
            admission=admission,
            task_id="task" if bound else None,
            winner=winner,
        )
        profiles.append(assignments.AssignmentStore._attempt_view(row, seen))
    return profiles


def rewrite_evidence(item: dict[str, Any]) -> str:
    """The table of the rewrite brief and R-4, restated here.

    It repeats the production predicate and so cannot find an error in it; the
    independent check of the class table is review 7's own table.
    """
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


def rewrite_block_profile(item: dict[str, Any]) -> tuple[str, bool, bool, bool]:
    """Q1 quotient refined by both old position predicates, to expose asymmetry.

    Once the fold uses only own evidence/facts, equal singleton contributions are
    substitutable in every multiset. The refinements retain abandoned, uncertain
    finals and invalidated final receipts as witnesses against the old code.
    """
    final = item["lookup"] == "valid" and item["receipt_status"] in (
        "completed",
        "failed",
        "cancelled",
        "abandoned",
    )
    return (
        rewrite_evidence(item),
        item["lookup"] == "valid" and item["receipt_status"] == "blocked",
        final,
        final and item["admission"] == "uncertain",
    )


def test_rewrite_blocking_is_invariant_under_every_profile_order() -> None:
    """red_at_pretip: every 1..3-profile multiset, independent tri-state Core facts.

    Rw2 is red at 38fb3f2: R-4 invalidated failed/cancelled profiles must be may_hold.
    First run uses F2_LEGACY_PROPERTY=1 and observes the same two decisions through
    old _slot_status; normal runs call the new fold directly, including diagnostics.
    Exhaust all 448 concrete shapes for singleton equivalence, then all orders of
    every multiset of their Q1 contributions. No random sampling or profile omission.
    """
    profiles = rewrite_profiles()
    assert len(profiles) == 448
    representatives = {}
    legacy = os.environ.get("F2_LEGACY_PROPERTY") == "1"
    for item in profiles:
        representatives.setdefault(rewrite_block_profile(item), item)
        if not legacy:
            for core in (None, False, True):
                facts = {} if core is None else {"core_blocked": core}
                result = assignments._blocking([item], [facts])
                evidence = rewrite_evidence(item)
                receipt_blocked = item["lookup"] == "valid" and item["receipt_status"] == "blocked"
                blocked = receipt_blocked or core is True
                assert result.blocked is blocked
                assert result.unknown is (evidence == "may_hold" and core is None and not blocked)
                assert bool(result.absent) is (evidence == "may_hold" and core is None)
                assert bool(result.contradictions) is (
                    (core is True and evidence != "may_hold") or (receipt_blocked and core is False)
                )
    tokens = list(itertools.product(representatives.values(), (None, False, True)))
    checked = 0
    for count in (1, 2, 3):
        for selection in itertools.combinations_with_replacement(range(len(tokens)), count):
            items, facts = [], []
            for identity, token in enumerate(selection, 1):
                item, core = tokens[token]
                items.append(dict(item, attempt=identity, member_key=f"key-{identity}"))
                facts.append({"stalled": False, **({} if core is None else {"core_blocked": core})})
            expected = None
            for order in itertools.permutations(range(count)):
                ordered_items = [items[i] for i in order]
                ordered_facts = [facts[i] for i in order]
                if legacy:
                    result = assignments._slot_status(ordered_items, 3, False, ordered_facts)
                    got = (result.status == "blocked", result.blocked_unknown)
                else:
                    got = assignments._blocking(ordered_items, ordered_facts)
                if expected is None:
                    expected = got
                assert got == expected, (items, facts, order, expected, got)
                checked += 1
    print(f"Q1: {len(profiles)} shapes, {len(representatives)} contributions, {checked} orders")
