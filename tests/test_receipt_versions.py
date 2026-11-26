"""Row T-137: a receipt records the bridge version that created it and the one that ended it."""

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from composition import contributed
from store_helpers import register_builtin_store_hooks

import answerback_bridge
from answerback_bridge import receipt_version, tasks
from answerback_bridge.config import Config, public_task
from answerback_bridge.rex_mailbox import RexMailbox
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import STATES, TERMINAL, TaskStore

CREATED = receipt_version.CREATED
ENDED = receipt_version.ENDED
OTHER = "9.9.9+other"
THIRD = "7.7.7+third"
STALE = "8.8.8+stale"
CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
# A receipt kind no loaded plugin claims: no store hooks in either composition.
FIXTURE_KIND = "fixtureprov"
# The installed plugin's reconciliation through its store hooks (tests/composition.py).
PLUGIN_RECONCILIATION = contributed("RECONCILIATION", None)
AUDIT_KEYS = {
    "reason",
    "previous_status",
    "previous_result",
    "previous_detail",
    "previous_updated",
    "previous_turn_id",
    "abandoned_at",
}
PUBLIC_KEYS = {
    "id",
    "request_key",
    "subject",
    "target",
    "status",
    "created",
    "updated",
    "metadata",
    "result",
    "detail",
    "turn_id",
}
COUNT_QUERY = """
SELECT count(*)
FROM tasks
WHERE status IN ('completed', 'failed', 'abandoned', 'cancelled')
  AND json_type(metadata, '$.bridge_version_created') = 'text'
  AND json_type(metadata, '$.bridge_version_ended') = 'text'
  AND json_extract(metadata, '$.bridge_version_created') = 'VERSION'
  AND json_extract(metadata, '$.bridge_version_ended') = 'VERSION';
"""
DOC_VERSION = "0.15.0+internal"
PRECHECK = "SELECT count(*) FROM tasks WHERE NOT json_valid(metadata);"
OLDEST_STAMPED = """
SELECT min(created) FROM tasks WHERE json_type(metadata, '$.bridge_version_created') = 'text';
"""
UNSTAMPED = """
SELECT count(*), max(created) FROM tasks
WHERE json_type(metadata, '$.bridge_version_created') IS NOT 'text';
"""


def real() -> str:
    version = answerback_bridge.__version__
    assert isinstance(version, str)
    return version


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return register_builtin_store_hooks(TaskStore(tmp_path / "tasks.sqlite"))


def use(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    """The version double: every stamp reads receipt_version.current at call time."""
    monkeypatch.setattr(receipt_version, "current", lambda: version)


def text(store: TaskStore, task_id: str) -> str:
    with store.connection() as db:
        return db.execute("SELECT metadata FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


def meta(store: TaskStore, task_id: str) -> Any:
    return json.loads(text(store, task_id))


def row(store: TaskStore, task_id: str) -> tuple:
    with store.connection() as db:
        return tuple(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def status(store: TaskStore, task_id: str) -> str:
    with store.connection() as db:
        return db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


def sql(store: TaskStore, statement: str, *parameters: Any) -> None:
    with store.connection() as db:
        db.execute(statement, parameters)


def put(store: TaskStore, task_id: str, value: Any, *, state: str | None = None) -> None:
    """Write the metadata column as an outside writer would, without any stamp."""
    sql(store, "UPDATE tasks SET metadata=? WHERE id=?", json.dumps(value), task_id)
    if state is not None:
        sql(store, "UPDATE tasks SET status=? WHERE id=?", state, task_id)


def put_key(store: TaskStore, task_id: str, key: str, value: Any) -> None:
    put(store, task_id, {**meta(store, task_id), key: value})


def rest(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in receipt_version.KEYS}


def reserve(store: TaskStore, kind: str = "herdr", key: str = "one", **more: Any) -> str:
    task, created = store.reserve(f"{kind}:{key}", "Work", key, {"kind": kind, **more})
    assert created
    return task["id"]


def binding(**changes: object) -> dict:
    return {
        "endpoint": "unix:///tmp/rex.sock",
        "server_instance_id": "server-1",
        "server_host_id": "host-1",
        "session_id": "session:11111111-1111-1111-1111-111111111111",
        "block_id": "block:22222222-2222-2222-2222-222222222222",
        "block_host_id": "host-1",
        "foreground_pid": 42,
        "foreground_name": "python",
        "foreground_path": None,
        "foreground_script": None,
        "foreground_cwd": "/project",
        **changes,
    }


def rex(store: TaskStore, *, acknowledged: bool = True) -> tuple[RexMailbox, str, str]:
    mailbox = RexMailbox(store)
    peer = mailbox.enroll("reviewer", binding(), "enroll-1")["peer_id"]
    task_id = mailbox.submit(peer, "work", "work-1")["id"]
    if acknowledged:
        mailbox.acknowledge(peer, task_id)
    return mailbox, peer, task_id


def desktop_failed(store: TaskStore, key: str = "ping") -> str:
    task, _ = store.reserve(f"desktop:{CHAT}", "Ping", key, {"kind": "desktop", "thread_id": CHAT})
    store.update(task["id"], "failed", result="Codex turn interrupted", turn_id="ping-turn")
    return task["id"]


def plugin_uncertain(store: TaskStore) -> str:
    assert PLUGIN_RECONCILIATION is not None
    return PLUGIN_RECONCILIATION.uncertain(store)


def reconcile(store: TaskStore, expected: dict[str, Any]) -> dict[str, Any]:
    assert PLUGIN_RECONCILIATION is not None
    return PLUGIN_RECONCILIATION.reconcile(store, expected)


def hc_provisional(store: TaskStore) -> str:
    task_id = reserve(store, "hc")
    assert store.complete_worker(task_id, "early")["status"] == "pending"
    return task_id


Ending = tuple[str, Callable[[], Any], set[str], set[str]]


def ending(path: str, store: TaskStore) -> Ending:
    """Prepare one receipt; return its ID, the ending, and the keys the path adds and removes."""
    if path.startswith("e1-"):
        state = path[3:]
        task_id = reserve(store)
        result = "done" if state == "completed" else None
        return task_id, lambda: store.update(task_id, state, result=result), set(), set()
    if path == "e2":
        task_id = reserve(store)
        return task_id, lambda: store.complete_worker(task_id, "done"), set(), set()
    if path == "e3":
        task_id = hc_provisional(store)
        return (
            task_id,
            lambda: store.confirm_delivery(task_id, {"started": True}, expected_kind="hc"),
            {"delivery", "delivery_verified"},
            {"provisional_result"},
        )
    if path == "e4":
        task_id = desktop_failed(store)
        return (
            task_id,
            lambda: store.reconcile(
                store.get(task_id), result="PONG", evidence={"completed_at": 105}
            ),
            {"desktop_reconciliation"},
            set(),
        )
    if path == "e5":
        assert PLUGIN_RECONCILIATION is not None
        task_id = plugin_uncertain(store)
        return (
            task_id,
            lambda: reconcile(store, store.get(task_id)),
            set(PLUGIN_RECONCILIATION.added_keys),
            set(),
        )
    if path == "e6":
        task_id = reserve(store, FIXTURE_KIND)
        return (
            task_id,
            lambda: store.abandon(store.get(task_id), "Reviewed"),
            {"abandonment"},
            set(),
        )
    assert path == "e7"
    mailbox, peer, task_id = rex(store)
    return task_id, lambda: mailbox.complete(peer, task_id, "done"), set(), set()


# e5 is a reconciliation through an installed plugin's hooks; it runs in the internal
# composition only (tests/composition.py). e4 covers the kernel's own path.
PATHS = ["e1-completed", "e1-failed", "e1-abandoned", "e1-cancelled"] + [
    f"e{number}" for number in range(2, 8) if number != 5 or PLUGIN_RECONCILIATION is not None
]
EARLIER = "the detail from before the ending"
END_STATE = {
    "e1-failed": "failed",
    "e1-abandoned": "abandoned",
    "e1-cancelled": "cancelled",
    "e6": "abandoned",
}


# 1
def test_01_own_version() -> None:
    assert receipt_version.current() == answerback_bridge.__version__
    assert receipt_version._BOUND.fullmatch(real())
    assert receipt_version.END_STATES == tasks.TERMINAL


BAD_VERSIONS = [5, None, "", "v" * 65, "a b"]


# 2
@pytest.mark.parametrize("bad", BAD_VERSIONS)
def test_02_invalid_own_version_is_no_version(monkeypatch: pytest.MonkeyPatch, bad: Any) -> None:
    monkeypatch.setattr(receipt_version, "__version__", bad)
    assert receipt_version.current() is None


@pytest.mark.parametrize("bad", BAD_VERSIONS)
def test_02_invalid_own_version_in_the_store(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    monkeypatch.setattr(receipt_version, "__version__", bad)
    task_id = reserve(store)
    assert CREATED not in meta(store, task_id)
    put_key(store, task_id, ENDED, STALE)
    assert store.update(task_id, "failed")["status"] == "failed"
    assert ENDED not in meta(store, task_id)
    assert public_task(store.get(task_id))["bridge_versions"] == {
        "created": "not recorded",
        "ended": "not recorded",
    }


# 3
def test_03_creation_is_recorded(store: TaskStore) -> None:
    task, _ = store.reserve("herdr:w1", "Work", "one", {"kind": "herdr", "target": "w1"})
    assert task["metadata"][CREATED] == receipt_version.current()
    assert meta(store, task["id"]) == {
        "kind": "herdr",
        "target": "w1",
        CREATED: receipt_version.current(),
    }
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        queued, created = store.reserve_in_transaction(
            db, "rex:p", "Work", "two", {"kind": "rex"}, initial_state="queued"
        )
    assert created and queued["status"] == "queued"
    assert meta(store, queued["id"]) == {"kind": "rex", CREATED: receipt_version.current()}
    empty, _ = store.reserve("herdr:w3", "Work", "three")
    assert meta(store, empty["id"]) == {CREATED: receipt_version.current()}


# 4
@pytest.mark.parametrize("key", receipt_version.KEYS)
def test_04_reservation_refuses_the_keys(store: TaskStore, key: str) -> None:
    with pytest.raises(ValueError, match="written by the store"):
        store.reserve("herdr:w1", "Work", "one", {"kind": "herdr", key: "9.9.9"})
    assert store.by_request("one") is None
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="written by the store"):
            store.reserve_in_transaction(db, "herdr:w1", "Work", "one", {key: None})
    assert store.by_request("one") is None


# 5, with ruling S1
@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("key", receipt_version.KEYS)
@pytest.mark.parametrize("pairs", [False, True])
def test_05_metadata_writes_refuse_the_keys(
    store: TaskStore, ended: bool, key: str, pairs: bool
) -> None:
    task_id = reserve(store)
    if ended:
        store.update(task_id, "failed")
    before = row(store, task_id)
    for value in ("9.9.9", receipt_version.current(), meta(store, task_id).get(key), None):
        changes: Any = {key: value, "note": "x"}
        if pairs:
            changes = list(changes.items())
        with pytest.raises(ValueError, match="written by the store"):
            store.metadata(task_id, changes)
        assert row(store, task_id) == before


def test_05_kind_given_as_pairs_and_ordinary_changes(store: TaskStore) -> None:
    task_id = reserve(store)
    before = row(store, task_id)
    with pytest.raises(ValueError, match="immutable"):
        store.metadata(task_id, [("kind", FIXTURE_KIND)])  # type: ignore[arg-type]
    assert row(store, task_id) == before
    assert store.metadata(task_id, {"note": "x", "kind": "herdr"})["metadata"]["note"] == "x"
    assert store.metadata(task_id, [("pair", 1)])["metadata"]["pair"] == 1  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown task ID"):
        store.metadata("missing", {CREATED: "9.9.9"})


# 6
def test_06_a_retry_keeps_the_creator(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = store.reserve("herdr:w1", "Work", "one", {"kind": "herdr"})
    use(monkeypatch, OTHER)
    again, created = store.reserve("herdr:w1", "Work", "one", {"kind": "herdr"})
    assert not created and again["id"] == first["id"]
    assert meta(store, first["id"])[CREATED] == real()


# 7
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("ender", [None, OTHER])
def test_07_every_ending_records_its_version(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, path: str, ender: str | None
) -> None:
    task_id, end, added, removed = ending(path, store)
    rewritten = path.startswith("e1-") or path in ("e2", "e7")
    if rewritten:
        # The statements this row wrote a second time: give them something to overwrite.
        sql(store, "UPDATE tasks SET detail=?, updated=updated-60 WHERE id=?", EARLIER, task_id)
    before = meta(store, task_id)
    earlier = store.get(task_id)
    if ender is not None:
        use(monkeypatch, ender)
    end()
    after = meta(store, task_id)
    if rewritten:
        assert earlier["detail"] == EARLIER and earlier["result"] is None
        written = store.get(task_id)
        assert written["result"] == (None if path in END_STATE else "done")
        assert written["detail"] is None
        assert written["updated"] > earlier["updated"]
        assert written["updated"] > written["created"]
    assert status(store, task_id) == END_STATE.get(path, "completed")
    assert after[ENDED] == (ender or real())
    assert after[CREATED] == real() == before[CREATED]
    assert set(rest(after)) - set(rest(before)) == added
    assert set(rest(before)) - set(rest(after)) == removed
    for key in set(rest(before)) - removed:
        assert after[key] == before[key]


# 8
@pytest.mark.parametrize("failer", [OTHER, THIRD])
def test_08_a_later_ending_by_the_creator_replaces(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, failer: str
) -> None:
    task, _ = store.reserve(f"desktop:{CHAT}", "Ping", "ping", {"kind": "desktop"})
    use(monkeypatch, failer)
    store.update(task["id"], "failed", result="Codex turn interrupted", turn_id="ping-turn")
    assert meta(store, task["id"])[ENDED] == failer
    monkeypatch.undo()
    store.reconcile(store.get(task["id"]), result="PONG", evidence={"completed_at": 105})
    assert meta(store, task["id"])[ENDED] == real()
    assert meta(store, task["id"])[CREATED] == real()


def test_08_an_ending_after_reopening_replaces(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = reserve(store)
    store.update(task_id, "failed")
    assert meta(store, task_id)[ENDED] == real()
    store.invalidate_submission(task_id, "invalidated")
    use(monkeypatch, OTHER)
    store.update(task_id, "completed", result="done")
    assert meta(store, task_id)[ENDED] == OTHER


# 9
def test_09_non_end_moves_never_stamp(tmp_path: Path) -> None:
    def fresh(name: str) -> TaskStore:
        return register_builtin_store_hooks(TaskStore(tmp_path / name / "tasks.sqlite"))

    for state in STATES:
        if state in TERMINAL:
            continue
        one = fresh("update-" + state)
        task_id = reserve(one)
        assert one.update(task_id, state)["status"] == state
        assert ENDED not in meta(one, task_id)

    one = fresh("recover")
    task_id = reserve(one)
    assert one.recover_pending() == 1
    assert status(one, task_id) == "uncertain" and ENDED not in meta(one, task_id)

    one = fresh("invalidate")
    task_id = reserve(one)
    one.invalidate_submission(task_id, "invalidated")
    assert status(one, task_id) == "uncertain" and ENDED not in meta(one, task_id)

    one = fresh("metadata")
    task_id = reserve(one)
    one.metadata(task_id, {"note": "x"})
    assert ENDED not in meta(one, task_id)

    one = fresh("turn")
    task_id = reserve(one, session_id="s1")
    one.bind_native_turn(task_id, "s1", "turn-1")
    assert meta(one, task_id)["native_turn_id"] == "turn-1" and ENDED not in meta(one, task_id)

    one = fresh("provisional")
    task_id = hc_provisional(one)
    assert "provisional_result" in meta(one, task_id) and ENDED not in meta(one, task_id)

    for name, delivery in (("yes", {"started": True}), ("no", {"started": False}), ("none", {})):
        one = fresh("delivery-" + name)
        task_id = reserve(one, "hc")
        confirmed = one.confirm_delivery(task_id, delivery, expected_kind="hc")
        assert confirmed["status"] not in TERMINAL
        assert ENDED not in meta(one, task_id)

    one = fresh("rex")
    mailbox, peer, task_id = rex(one)
    assert status(one, task_id) == "submitted" and ENDED not in meta(one, task_id)
    mailbox.invalidate(peer, "gone")
    assert status(one, task_id) == "uncertain" and ENDED not in meta(one, task_id)


# 10
def test_10_a_stale_stamp_is_not_shown(store: TaskStore) -> None:
    task_id = reserve(store)
    store.update(task_id, "failed")
    reopened = store.invalidate_submission(task_id, "invalidated")
    public = public_task(reopened)
    assert public["bridge_versions"]["ended"] is None
    assert meta(store, task_id)[ENDED] == real()
    assert receipt_version.read({"status": "running", "metadata": {ENDED: "1.0"}})["ended"] is None


# 11
def uncertain_fixture(store: TaskStore) -> dict[str, Any]:
    task_id = reserve(store, FIXTURE_KIND, proof="old")
    return store.update(
        task_id, "uncertain", result="partial", detail="correlation failed", turn_id="t1"
    )


def assert_both_retries(store: TaskStore, old: dict[str, Any], reason: str) -> None:
    current = store.get(old["id"])
    before = row(store, old["id"])
    assert store.abandon(current, reason) == current
    assert store.abandon(old, reason) == current
    assert row(store, old["id"]) == before


def test_11a_the_audit_has_its_seven_keys(store: TaskStore) -> None:
    current = store.abandon(uncertain_fixture(store), "Reviewed")
    assert set(current["metadata"]["abandonment"]) == AUDIT_KEYS


@pytest.mark.parametrize("retrier", [None, OTHER])
def test_11b_both_retry_forms_write_nothing(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, retrier: str | None
) -> None:
    old = uncertain_fixture(store)
    store.abandon(old, "Reviewed")
    if retrier is not None:
        use(monkeypatch, retrier)
    assert_both_retries(store, old, "Reviewed")


@pytest.mark.parametrize("abandoner", [None, OTHER])
def test_11c_retries_after_a_stale_ending_stamp(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, abandoner: str | None
) -> None:
    task_id = reserve(store, FIXTURE_KIND)
    store.update(task_id, "failed")
    old = store.invalidate_submission(task_id, "invalidated")
    assert old["metadata"][ENDED] == real()
    if abandoner is not None:
        use(monkeypatch, abandoner)
    store.abandon(old, "Reviewed")
    assert meta(store, task_id)[ENDED] == (abandoner or real())
    assert_both_retries(store, old, "Reviewed")


@pytest.mark.parametrize("stale", [False, True])
def test_11d_an_abandonment_written_by_an_earlier_bridge(store: TaskStore, stale: bool) -> None:
    task_id = reserve(store, FIXTURE_KIND)
    if stale:
        put_key(store, task_id, ENDED, STALE)
    old = store.update(task_id, "uncertain", detail="lost")
    audit = {
        "reason": "Reviewed",
        "previous_status": old["status"],
        "previous_result": old["result"],
        "previous_detail": old["detail"],
        "previous_updated": old["updated"],
        "previous_turn_id": old["turn_id"],
        "abandoned_at": 5.0,
    }
    sql(
        store,
        "UPDATE tasks SET status='abandoned', result=NULL, detail=?, metadata=?, updated=? "
        "WHERE id=?",
        "Reviewed",
        json.dumps({**old["metadata"], "abandonment": audit}),
        5.0,
        task_id,
    )
    assert_both_retries(store, old, "Reviewed")


def test_11e_only_the_ending_key_is_ignored(store: TaskStore) -> None:
    old = uncertain_fixture(store)
    current = store.abandon(old, "Reviewed")
    before = row(store, old["id"])
    invented = {**old, "metadata": {**old["metadata"], ENDED: "1.2.3+invented"}}
    assert store.abandon(invented, "Reviewed") == current
    assert row(store, old["id"]) == before
    for creation in ({CREATED: "1.2.3+invented"}, {CREATED: 1}):
        changed = {**old, "metadata": {**old["metadata"], **creation}}
        with pytest.raises(ValueError, match="changed during abandonment"):
            store.abandon(changed, "Reviewed")
    dropped = {**old, "metadata": rest(old["metadata"])}
    if dropped != old:
        with pytest.raises(ValueError, match="changed during abandonment"):
            store.abandon(dropped, "Reviewed")
    assert row(store, old["id"]) == before


def test_11f_edits_and_other_reasons_still_refuse(store: TaskStore) -> None:
    old = uncertain_fixture(store)
    abandoned = store.abandon(old, "Reviewed")
    with pytest.raises(ValueError, match="terminal"):
        store.abandon(abandoned, "Another reason")
    with pytest.raises(ValueError, match="changed during abandonment"):
        store.abandon(old, "Another reason")
    with pytest.raises(ValueError, match="changed during abandonment"):
        store.abandon({**old, "metadata": {**old["metadata"], "proof": "new"}}, "Reviewed")
    for wrong in ({"id": old["id"]}, {**old, "metadata": None}, {**old, "metadata": []}):
        with pytest.raises(ValueError, match="changed during abandonment"):
            store.abandon(wrong, "Reviewed")
    current = store.metadata(old["id"], {"later_note": "new information"})
    for snapshot in (old, abandoned):
        with pytest.raises(ValueError, match="changed during abandonment"):
            store.abandon(snapshot, "Reviewed")
    assert store.get(old["id"]) == current


# 12
def test_12_not_recorded(store: TaskStore) -> None:
    task_id = reserve(store)
    put(store, task_id, {"kind": "herdr"})
    assert public_task(store.get(task_id))["bridge_versions"] == {
        "created": "not recorded",
        "ended": None,
    }
    for state in TERMINAL:
        put(store, task_id, {"kind": "herdr"}, state=state)
        assert public_task(store.get(task_id))["bridge_versions"] == {
            "created": "not recorded",
            "ended": "not recorded",
        }
        put(store, task_id, {"kind": "herdr", CREATED: "1.2.3"}, state=state)
        assert public_task(store.get(task_id))["bridge_versions"] == {
            "created": "1.2.3",
            "ended": "not recorded",
        }


# 13
BAD_VALUES = ["", "a b", "q7 b", "z" * 65, 5, True, None, ["x7q"], {"a7q": 1}]
DISTINCTIVE = ("q7 b", "z" * 65, "x7q", "a7q")


@pytest.mark.parametrize("key", receipt_version.KEYS)
@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
def test_13_unreadable_values(store: TaskStore, key: str, bad: Any) -> None:
    task_id = reserve(store)
    other = ENDED if key == CREATED else CREATED
    put(store, task_id, {"kind": "herdr", key: bad, other: "1.2.3"}, state="failed")
    public = public_task(store.get(task_id))
    shown = {CREATED: "created", ENDED: "ended"}
    assert public["bridge_versions"][shown[key]] == "unreadable"
    assert public["bridge_versions"][shown[other]] == "1.2.3"
    assert public["metadata"] == {"kind": "herdr"}
    for part in DISTINCTIVE:
        assert part not in json.dumps(public)


def test_13_a_bad_stale_value_is_not_kept_by_an_abandonment(store: TaskStore) -> None:
    task_id = reserve(store, FIXTURE_KIND)
    put_key(store, task_id, ENDED, "bad stale q9")
    public = public_task(store.abandon(store.get(task_id), "Reviewed"))
    assert set(public["metadata"]["abandonment"]) == AUDIT_KEYS
    assert "q9" not in json.dumps(public)
    assert "q9" not in text(store, task_id)
    assert public["bridge_versions"] == {"created": real(), "ended": real()}


# 14
def test_14_the_public_receipt(store: TaskStore) -> None:
    given = {"kind": "herdr", "target": "w1", "terminal_id": "t"}
    private = {
        "provisional_result": "early p3",
        "result_token": "token-t3",
        "result_path": "/results/token-t3.md",
    }
    task, _ = store.reserve("herdr:w1", "Work", "one", {**given, **private})
    public = public_task(store.update(task["id"], "completed", result="done"))
    stored = meta(store, task["id"])
    assert stored == {**given, **private, CREATED: real(), ENDED: real()}
    for key in (*private, CREATED, ENDED):
        assert key in stored and key not in public["metadata"]
    for value in private.values():
        assert value not in json.dumps(public)
    assert public["metadata"]["target"] == "w1"
    assert set(public) == PUBLIC_KEYS | {"bridge_versions"}
    assert public["metadata"] == given
    assert public["bridge_versions"] == {"created": real(), "ended": real()}
    two = store.get(reserve(store, FIXTURE_KIND, "two"))
    abandoned = public_task(store.abandon(two, "Reviewed"))
    assert set(abandoned["metadata"]) == {"kind", "abandonment"}
    assert public_task(store.get(reserve(store, FIXTURE_KIND, "three")))["bridge_versions"] == {
        "created": real(),
        "ended": None,
    }


# 15
async def test_15_views_without_metadata(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        store = bridge.store
        done, _ = store.reserve(f"{FIXTURE_KIND}:one", "Work", "first", {"kind": FIXTURE_KIND})
        store.update(done["id"], "completed", result="done")
        gone, _ = store.reserve(f"{FIXTURE_KIND}:two", "Work", "second", {"kind": FIXTURE_KIND})
        store.abandon(store.get(gone["id"]), "Reviewed")
        listed = bridge.task_list()["tasks"]
        assert len(listed) == 2
        for entry in listed:
            assert set(entry) == {
                "id",
                "request_key",
                "target",
                "status",
                "created",
                "updated",
                "subject",
                "has_result",
                "result_available",
                "detail",
            }
        snapshot = store.snapshot_read(deadline=time.monotonic() + 5)
        assert set(snapshot) == {"errors", "counts", "needs_person", "recent"}
        for entry in snapshot["recent"]:
            assert set(entry) == {
                "id",
                "request_key",
                "target",
                "status",
                "created",
                "updated",
                "detail",
            }
        assert store.counts() == {"completed": 1, "abandoned": 1}
        assert len(bridge.task_list(FIXTURE_KIND)["tasks"]) == 2
        for word in (real(), "bridge_version", "internal"):
            assert bridge.task_list(word)["tasks"] == []
    finally:
        await bridge.close()


# 16
def test_16_pruning_keeps_both(tmp_path: Path) -> None:
    from answerback_bridge.maintenance import Maintenance
    from answerback_bridge.retention import migrate_tasks

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:one", "private text\n", "one", {"kind": "kiro", "cwd": "/p"})
    store.update(task["id"], "completed", result="private done")
    with store.connection() as db:
        migrate_tasks(db)
        db.execute("UPDATE tasks SET created=1,updated=2 WHERE id=?", (task["id"],))
        db.execute("UPDATE retention_records SET changed_at=3 WHERE row_id=?", (task["id"],))
    before = public_task(store.get(task["id"]))
    stored = text(store, task["id"])
    assert json.loads(stored)[CREATED] == real() and json.loads(stored)[ENDED] == real()
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert maintenance.apply(maintenance.plan())["status"] == "applied"
    after = public_task(store.get(task["id"]))
    assert text(store, task["id"]) == stored
    assert (
        after["bridge_versions"]
        == before["bridge_versions"]
        == {
            "created": real(),
            "ended": real(),
        }
    )
    assert after["result"] is None and after["content_pruned"] is True
    assert after["has_result"] is True and after["result_available"] is False


# 17
def normal(value: str) -> str:
    return " ".join(value.split())


def count(store: TaskStore, version: str) -> int:
    with store.connection() as db:
        return db.execute(COUNT_QUERY.replace("VERSION", version)).fetchone()[0]


def precheck(store: TaskStore) -> int:
    with store.connection() as db:
        return db.execute(PRECHECK).fetchone()[0]


def test_17_the_count_is_in_the_guide() -> None:
    guide = normal((Path(__file__).parents[1] / "docs" / "MAINTENANCE.md").read_text())
    assert normal(COUNT_QUERY.replace("VERSION", DOC_VERSION)) in guide
    assert normal(PRECHECK) in guide


def test_17_the_count(store: TaskStore) -> None:
    version = real()
    store.update(reserve(store, key="counted-1"), "completed", result="done")
    store.abandon(store.get(reserve(store, FIXTURE_KIND, "counted-2")), "Reviewed")
    reserve(store, key="open")
    odd: list[tuple[str, Any, str]] = [
        ("other-creator", {CREATED: OTHER, ENDED: version}, "completed"),
        ("other-ender", {CREATED: version, ENDED: OTHER}, "failed"),
        ("no-ending", {CREATED: version}, "completed"),
        ("stale", {CREATED: version, ENDED: version}, "uncertain"),
        ("number", {CREATED: 5, ENDED: version}, "completed"),
        ("number-end", {CREATED: version, ENDED: 5}, "completed"),
        ("array", {CREATED: [version], ENDED: [version]}, "cancelled"),
        ("list", [1], "completed"),
        ("word", version, "completed"),
        ("old", {"kind": "herdr"}, "completed"),
    ]
    for key, value, state in odd:
        put(store, reserve(store, key=key), value, state=state)
    assert precheck(store) == 0
    assert count(store, version) == 2
    assert count(store, OTHER) == 0


def aid(store: TaskStore) -> tuple[Any, int, Any]:
    """The oldest stamped creation time; the count and the newest time of the unstamped."""
    with store.connection() as db:
        oldest = db.execute(OLDEST_STAMPED).fetchone()[0]
        number, newest = db.execute(UNSTAMPED).fetchone()
    return oldest, number, newest


def test_17_the_aid_queries(store: TaskStore) -> None:
    guide = normal((Path(__file__).parents[1] / "docs" / "MAINTENANCE.md").read_text())
    assert normal(OLDEST_STAMPED) in guide
    assert normal(UNSTAMPED) in guide

    def at(task_id: str, created: float) -> None:
        sql(store, "UPDATE tasks SET created=? WHERE id=?", created, task_id)

    assert aid(store) == (None, 0, None)
    at(reserve(store, key="stamped-open"), 300.0)
    ended = reserve(store, key="stamped-ended")
    store.update(ended, "completed", result="done")
    at(ended, 100.0)
    at(reserve(store, FIXTURE_KIND, "stamped-other"), 200.0)
    assert aid(store) == (100.0, 0, None)

    older = reserve(store, key="unstamped-older")
    put(store, older, {"kind": "herdr"}, state="completed")
    at(older, 50.0)
    oldest, number, newest = aid(store)
    assert (oldest, number, newest) == (100.0, 1, 50.0)
    assert newest < oldest

    younger = reserve(store, key="unstamped-younger")
    put(store, younger, {"kind": "herdr", CREATED: 5})
    at(younger, 250.0)
    oldest, number, newest = aid(store)
    assert (oldest, number, newest) == (100.0, 2, 250.0)
    assert newest > oldest

    put(store, younger, [real()])
    assert aid(store) == (100.0, 2, 250.0)


def test_17_the_precheck_finds_malformed_rows(store: TaskStore) -> None:
    store.update(reserve(store, key="counted"), "failed")
    waiting = reserve(store, key="open")
    sql(store, "UPDATE tasks SET metadata='{bad' WHERE id=?", waiting)
    assert precheck(store) == 1
    ended = reserve(store, key="ended")
    sql(store, "UPDATE tasks SET metadata='{bad', status='completed' WHERE id=?", ended)
    assert precheck(store) == 2
    with pytest.raises(sqlite3.Error, match="(?i)malformed JSON"):
        count(store, real())


# 18
def test_18_ordinary_metadata_writes_keep_both(tmp_path: Path) -> None:
    def fresh(name: str) -> TaskStore:
        return register_builtin_store_hooks(TaskStore(tmp_path / name / "tasks.sqlite"))

    def stale(one: TaskStore, task_id: str) -> None:
        put_key(one, task_id, ENDED, STALE)

    def kept(one: TaskStore, task_id: str) -> None:
        after = meta(one, task_id)
        assert after[CREATED] == real()
        assert after[ENDED] == STALE

    one = fresh("metadata")
    task_id = reserve(one)
    stale(one, task_id)
    assert one.metadata(task_id, {"note": "x"})["metadata"]["note"] == "x"
    kept(one, task_id)

    one = fresh("turn")
    task_id = reserve(one, session_id="s1")
    stale(one, task_id)
    earlier = meta(one, task_id)
    assert "native_turn_id" not in earlier
    one.bind_native_turn(task_id, "s1", "turn-1")
    assert meta(one, task_id) == {**earlier, "native_turn_id": "turn-1"}
    assert status(one, task_id) == "pending"
    kept(one, task_id)

    one = fresh("provisional")
    task_id = reserve(one, "hc")
    stale(one, task_id)
    one.complete_worker(task_id, "early")
    assert meta(one, task_id)["provisional_result"] == "early"
    kept(one, task_id)

    one = fresh("delivery")
    task_id = reserve(one, "hc")
    stale(one, task_id)
    assert (
        one.confirm_delivery(task_id, {"started": True}, expected_kind="hc")["status"]
        == "submitted"
    )
    kept(one, task_id)

    one = fresh("invalidate")
    task_id = reserve(one)
    stale(one, task_id)
    sql(one, "UPDATE tasks SET result='a claim from before' WHERE id=?", task_id)
    earlier = meta(one, task_id)
    assert "identity_invalidated" not in earlier
    invalid = one.invalidate_submission(task_id, "invalidated")
    assert invalid["status"] == "uncertain" and invalid["detail"] == "invalidated"
    assert invalid["result"] is None
    assert meta(one, task_id) == {**earlier, "identity_invalidated": True}
    kept(one, task_id)

    one = fresh("rex")
    mailbox, peer, task_id = rex(one, acknowledged=False)
    stale(one, task_id)
    assert mailbox.acknowledge(peer, task_id)["status"] == "submitted"
    kept(one, task_id)


# 19
INFINITE = '{"kind": "herdr", "n": 1e400}'


def raw(store: TaskStore, task_id: str, expression: str, *parameters: Any) -> None:
    sql(store, f"UPDATE tasks SET metadata={expression} WHERE id=?", *parameters, task_id)


def blob(store: TaskStore, task_id: str) -> bytes:
    with store.connection() as db:
        return db.execute(
            "SELECT CAST(metadata AS BLOB) FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]


def valid(store: TaskStore, task_id: str) -> int:
    with store.connection() as db:
        return db.execute(
            "SELECT json_valid(metadata) FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]


def test_19a_unknown_id(store: TaskStore) -> None:
    with pytest.raises(ValueError, match="Unknown task ID"):
        store.update("no-such-receipt", "failed")


def test_19b_an_ended_receipt_stays(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = reserve(store)
    first = store.update(task_id, "failed", detail="first")
    assert first["metadata"][ENDED] == real()
    before = row(store, task_id)
    assert store.update(task_id, "completed", result="late") == first
    use(monkeypatch, OTHER)
    assert store.update(task_id, "cancelled") == first
    assert row(store, task_id) == before


@pytest.mark.parametrize("stored", ["[1]", '"x"'])
def test_19c_metadata_that_is_no_object(store: TaskStore, stored: str) -> None:
    task_id = reserve(store)
    raw(store, task_id, "?", stored)
    assert store.update(task_id, "failed")["metadata"] == json.loads(stored)
    assert status(store, task_id) == "failed"
    assert text(store, task_id) == stored
    assert public_task({**store.get(task_id), "metadata": {}})["status"] == "failed"


def test_19d_malformed_json(store: TaskStore) -> None:
    task_id = reserve(store)
    raw(store, task_id, "?", "{bad")
    with pytest.raises(json.JSONDecodeError):
        store.update(task_id, "failed")
    assert status(store, task_id) == "failed"
    assert text(store, task_id) == "{bad"


def raiser(error: type[Exception]) -> Callable[[Any], Any]:
    def stamp(metadata: Any) -> Any:
        raise error("the stamp is broken")

    return stamp


ERRORS = [ValueError, RuntimeError]


@pytest.mark.parametrize("error", ERRORS)
def test_19e_a_broken_stamp_never_stops_e1(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    task_id = reserve(store)
    assert CREATED in meta(store, task_id)
    before = text(store, task_id)
    monkeypatch.setattr(receipt_version, "stamp_ended", raiser(error))
    ended = store.update(task_id, "failed", detail="broken")
    assert ended["status"] == "failed" and ended["detail"] == "broken"
    assert text(store, task_id) == before
    assert public_task(ended)["bridge_versions"] == {"created": real(), "ended": "not recorded"}


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("path", ["e2", "e7"])
def test_19e_a_broken_stamp_never_stops_e2_and_e7(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, error: type[Exception], path: str
) -> None:
    task_id, end, _, _ = ending(path, store)
    assert CREATED in meta(store, task_id)
    before = text(store, task_id)
    monkeypatch.setattr(receipt_version, "stamp_ended", raiser(error))
    end()
    assert status(store, task_id) == "completed"
    assert text(store, task_id) == before
    assert public_task(store.get(task_id))["bridge_versions"] == {
        "created": real(),
        "ended": "not recorded",
    }


def test_19f_two_stores_on_one_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = TaskStore(tmp_path / "tasks.sqlite")
    second = TaskStore(tmp_path / "tasks.sqlite")
    task_id = reserve(first)
    use(monkeypatch, OTHER)
    ended = first.update(task_id, "failed", detail="first")
    use(monkeypatch, THIRD)
    assert second.update(task_id, "completed", result="second") == ended
    assert status(second, task_id) == "failed"
    assert meta(second, task_id)[ENDED] == OTHER


def test_19f_update_waits_for_another_writer(store: TaskStore) -> None:
    task_id = reserve(store)
    held = threading.Event()

    def hold() -> None:
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tasks SET metadata=json_set(metadata, '$.holder', 'held q5') WHERE id=?",
                (task_id,),
            )
            held.set()
            time.sleep(0.3)
            db.execute("COMMIT")

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    started = time.monotonic()
    ended = store.update(task_id, "completed", result="done")
    waited = time.monotonic() - started
    thread.join()
    assert waited >= 0.25
    assert ended["status"] == "completed" and ended["result"] == "done"
    after = meta(store, task_id)
    assert after == ended["metadata"]
    assert after["holder"] == "held q5"
    assert after[CREATED] == real()
    assert after[ENDED] == real()


# Ruling R1 on review 1: the stated limit of the second kind.
@pytest.mark.parametrize("case", ["a number outside a double", "a stamp that raises"])
def test_19g_a_failed_stamp_keeps_an_earlier_ending_stamp(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    use(monkeypatch, OTHER)
    task_id = reserve(store)
    store.update(task_id, "failed")
    store.invalidate_submission(task_id, "invalidated")
    monkeypatch.undo()
    assert status(store, task_id) == "uncertain"
    assert meta(store, task_id)[CREATED] == OTHER and meta(store, task_id)[ENDED] == OTHER
    if case == "a number outside a double":
        raw(store, task_id, "?", text(store, task_id)[:-1] + ', "n": 1e400}')
        assert valid(store, task_id) == 1
        kept = meta(store, task_id)
        assert kept[CREATED] == OTHER and kept[ENDED] == OTHER
        assert kept["kind"] == "herdr" and kept["identity_invalidated"] is True
    else:
        monkeypatch.setattr(receipt_version, "stamp_ended", raiser(RuntimeError))
    before = text(store, task_id)
    assert receipt_version.current() == real() != OTHER
    ended = store.update(task_id, "completed", result="done")
    assert ended["status"] == "completed" and status(store, task_id) == "completed"
    assert text(store, task_id) == before
    assert meta(store, task_id)[ENDED] == OTHER
    assert public_task(store.get(task_id))["bridge_versions"]["ended"] == OTHER
    assert count(store, OTHER) == 1
    assert count(store, real()) == 0


def test_19_s3_text_that_is_not_utf8(store: TaskStore) -> None:
    task_id = reserve(store)
    raw(store, task_id, "CAST(X'80' AS TEXT)")
    with pytest.raises(sqlite3.OperationalError, match="Could not decode"):
        store.update(task_id, "failed")
    assert status(store, task_id) == "failed"
    assert blob(store, task_id) == b"\x80"


def test_19_s3_a_number_python_reads_as_infinity_e1(store: TaskStore) -> None:
    task_id = reserve(store)
    raw(store, task_id, "?", INFINITE)
    assert valid(store, task_id) == 1
    assert store.update(task_id, "failed")["status"] == "failed"
    assert text(store, task_id) == INFINITE
    assert valid(store, task_id) == 1


def test_19_s3_a_number_python_reads_as_infinity_e2(store: TaskStore) -> None:
    task_id = reserve(store)
    raw(store, task_id, "?", INFINITE)
    assert store.complete_worker(task_id, "done")["status"] == "completed"
    assert text(store, task_id) == INFINITE
    assert valid(store, task_id) == 1


def test_19_s3_a_number_python_reads_as_infinity_e7(store: TaskStore) -> None:
    mailbox, peer, task_id = rex(store)
    stored = json.dumps(rest(meta(store, task_id)))[:-1] + ', "n": 1e400}'
    raw(store, task_id, "?", stored)
    assert valid(store, task_id) == 1
    assert mailbox.complete(peer, task_id, "done")["status"] == "completed"
    assert text(store, task_id) == stored
    assert valid(store, task_id) == 1


# 20
def test_20_refused_operations_leave_the_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fresh(name: str) -> TaskStore:
        return register_builtin_store_hooks(TaskStore(tmp_path / name / "tasks.sqlite"))

    def stamped(one: TaskStore, task_id: str) -> tuple:
        assert meta(one, task_id)[ENDED] in (real(), STALE)
        return row(one, task_id)

    # abandon with a changed receipt
    one = fresh("changed")
    task_id = reserve(one, FIXTURE_KIND)
    put_key(one, task_id, ENDED, STALE)
    old = one.get(task_id)
    one.metadata(task_id, {"note": "later"})
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    with pytest.raises(ValueError, match="changed during abandonment"):
        one.abandon(old, "Reviewed")
    assert row(one, task_id) == before
    monkeypatch.undo()

    # abandon of an ended receipt
    one = fresh("ended")
    task_id = reserve(one, FIXTURE_KIND)
    one.update(task_id, "failed")
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    with pytest.raises(ValueError, match="terminal"):
        one.abandon(one.get(task_id), "Reviewed")
    assert row(one, task_id) == before
    monkeypatch.undo()

    # desktop reconciliation with a stale expected
    one = fresh("desktop")
    task_id = desktop_failed(one)
    stale = one.get(task_id)
    sql(one, "UPDATE tasks SET detail='moved' WHERE id=?", task_id)
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    with pytest.raises(ValueError, match="changed during reconciliation"):
        one.reconcile(stale, result="PONG", evidence={"completed_at": 105})
    assert row(one, task_id) == before
    monkeypatch.undo()

    # A plugin's reconciliation with a stale expected: internal only.
    if PLUGIN_RECONCILIATION is not None:
        one = fresh("plugin")
        task_id = plugin_uncertain(one)
        put_key(one, task_id, ENDED, STALE)
        stale = one.get(task_id)
        sql(one, "UPDATE tasks SET detail='moved' WHERE id=?", task_id)
        before = stamped(one, task_id)
        use(monkeypatch, OTHER)
        with pytest.raises(ValueError, match="changed during reconciliation"):
            reconcile(one, stale)
        assert row(one, task_id) == before
        monkeypatch.undo()

    # complete_worker on an ended receipt
    one = fresh("worker")
    task_id = reserve(one)
    one.update(task_id, "failed")
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    with pytest.raises(ValueError, match="no longer active"):
        one.complete_worker(task_id, "done")
    assert row(one, task_id) == before
    monkeypatch.undo()

    one = fresh("worker-done")
    task_id = reserve(one)
    one.complete_worker(task_id, "done")
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    assert one.complete_worker(task_id, "done")["metadata"][ENDED] == real()
    with pytest.raises(ValueError, match="different completed result"):
        one.complete_worker(task_id, "other")
    assert row(one, task_id) == before
    monkeypatch.undo()

    # Rex complete, retried
    one = fresh("rex")
    mailbox, peer, task_id = rex(one)
    mailbox.complete(peer, task_id, "done")
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    assert mailbox.complete(peer, task_id, "done")["bridge_versions"]["ended"] == real()
    with pytest.raises(ValueError, match="different completed result"):
        mailbox.complete(peer, task_id, "other")
    assert row(one, task_id) == before
    monkeypatch.undo()

    # confirm_delivery on an ended receipt
    one = fresh("delivery")
    task_id = reserve(one, "hc")
    ended = one.update(task_id, "failed")
    before = stamped(one, task_id)
    use(monkeypatch, OTHER)
    assert one.confirm_delivery(task_id, {"started": True}, expected_kind="hc") == ended
    assert row(one, task_id) == before


# 21, ruling R5 on review 1
def test_21_read_for_metadata_that_is_no_dict() -> None:
    listed = [CREATED, ENDED, "1.2.3"]
    for state in TERMINAL:
        assert receipt_version.read({"status": state, "metadata": listed}) == {
            "created": "not recorded",
            "ended": "not recorded",
        }
        assert receipt_version.read({"status": state}) == {
            "created": "not recorded",
            "ended": "not recorded",
        }
    for state in STATES:
        if state in TERMINAL:
            continue
        assert receipt_version.read({"status": state, "metadata": listed}) == {
            "created": "not recorded",
            "ended": None,
        }
        assert receipt_version.read({"status": state}) == {
            "created": "not recorded",
            "ended": None,
        }
    assert receipt_version.read({"metadata": listed}) == {"created": "not recorded", "ended": None}
    assert receipt_version.read({}) == {"created": "not recorded", "ended": None}
