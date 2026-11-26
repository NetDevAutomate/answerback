"""A receipt's `subject`: what the sender says a hand-off was about (T-277)."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from answerback_bridge.adapters.base import StoredAdapter
from answerback_bridge.adapters.contracts import (
    DiscoveryPage,
    Guard,
    Peer,
    PreparedPeer,
    Submission,
    TaskReceipt,
)
from answerback_bridge.adapters.registry import AdapterRegistry
from answerback_bridge.config import Config
from answerback_bridge.service import Bridge
from answerback_bridge.subjects import admission_subject, validate_subject
from answerback_bridge.tasks import TaskStore

SHA = "0123456789abcdef" * 2 + "01234567"  # a 40-character commit SHA
DIFFERENT = "^request_id already describes different work$"
INVALID = "^subject must be 1-256 printable characters without leading or trailing spaces$"


class SubjectAdapter(StoredAdapter):
    """Reserves like a real adapter: it never passes a subject itself."""

    name = registration = "fixture"
    recovery_states = ("running",)
    sends = 0
    reserve_in_thread = False
    background_request: str | None = None
    # When set, reserve on an executor thread that waits for `release` first.
    release: threading.Event | None = None
    waiting = threading.Event()
    finished = threading.Event()

    def enabled(self) -> bool:
        return True

    async def discover(self) -> DiscoveryPage:
        return DiscoveryPage(
            (
                Peer.from_record(
                    {
                        "peer_id": "fixture:one",
                        "provider": self.name,
                        "target": "one",
                        "project": str(self.config_path.parent),
                    }
                ),
            )
        )

    async def prepare(self, peer: Peer) -> PreparedPeer:
        return PreparedPeer("fixture:one", peer)

    def _reserve(self, prepared: PreparedPeer, submission: Submission) -> dict:
        row, created = self.store.reserve(
            prepared.target_key, submission.message, submission.request_id, {"kind": self.name}
        )
        if created:
            self.sends += 1
        return row

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        guard()
        if self.release is not None:
            release = self.release

            def held() -> dict:
                self.waiting.set()
                try:
                    release.wait(5)
                    return self._reserve(prepared, submission)
                finally:
                    self.finished.set()

            row = await asyncio.to_thread(held)
        elif self.reserve_in_thread:
            box: list[dict] = []
            thread = threading.Thread(
                target=lambda: box.append(self._reserve(prepared, submission))
            )
            thread.start()
            thread.join()
            row = box[0]
        else:
            row = self._reserve(prepared, submission)
        if self.background_request is not None:
            # A task created during submit copies the context; its own reservation for
            # another request must not inherit this submission's subject.
            other = self.background_request

            async def later() -> None:
                self.store.reserve("fixture:two", "Other work", other, {"kind": self.name})

            await asyncio.create_task(later())
        return TaskReceipt.from_record(row)

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return receipt.provider == self.name and receipt.target == peer.peer_id


def environment(path: Path) -> tuple[Bridge, SubjectAdapter]:
    bridge = Bridge(Config(provider_selection=[]), path)
    adapter = SubjectAdapter(bridge.config, path, bridge.store)
    bridge.adapters = AdapterRegistry([adapter])
    return bridge, adapter


@pytest.mark.parametrize(
    "value",
    [SHA, "v0.16.2", "sha256:" + "a" * 64, "doc 42", "é" * 256, "x"],
)
def test_valid_subjects_are_returned_unchanged(value: str) -> None:
    assert validate_subject(value) == value
    assert validate_subject(None) is None


@pytest.mark.parametrize(
    "value",
    ["", " ", "x" * 257, " abc", "abc ", "a\nb", "a\tb", "a\x00b", "a\u202eb", "a\x1bb", 7, b"x"],
)
def test_invalid_subjects_are_refused(value: object) -> None:
    with pytest.raises(ValueError, match=INVALID):
        validate_subject(value)


def test_reserve_stores_the_subject_beside_the_request_key(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, created = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    assert created and task["subject"] == SHA
    assert "subject" not in task["metadata"]
    assert store.get(task["id"])["subject"] == SHA
    plain, _ = store.reserve("t2", "Review", "r2", {"kind": "fixture"})
    assert plain["subject"] is None


def test_reserve_refuses_an_invalid_subject_before_storing(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    with pytest.raises(ValueError, match=INVALID):
        store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject="bad\n")
    assert store.recent() == []


@pytest.mark.parametrize(("first", "second"), [(SHA, "other"), (SHA, None), (None, SHA)])
def test_same_request_with_another_subject_is_different_work(
    tmp_path: Path, first: str | None, second: str | None
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=first)
    with pytest.raises(ValueError, match=DIFFERENT):
        store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=second)
    again, created = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=first)
    assert not created and again["id"] == task["id"]
    assert len(store.recent()) == 1


def test_admission_subject_applies_only_to_its_own_request(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    with admission_subject(store.path, "r1", SHA):
        mine, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"})
        other, _ = store.reserve("t2", "Review", "r2", {"kind": "fixture"})
        with pytest.raises(ValueError, match="^subject conflicts with the admission subject$"):
            store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject="else")
        elsewhere = TaskStore(tmp_path / "other" / "tasks.sqlite")
        assert elsewhere.reserve("t", "Review", "r1", {"kind": "fixture"})[0]["subject"] is None
    assert (mine["subject"], other["subject"]) == (SHA, None)
    after, _ = store.reserve("t3", "Review", "r3", {"kind": "fixture"})
    assert after["subject"] is None


@pytest.mark.parametrize("second", ["else", None])
def test_a_concurrent_admission_with_another_subject_is_refused(
    tmp_path: Path, second: str | None
) -> None:
    path = tmp_path / "tasks.sqlite"
    with admission_subject(path, "r1", SHA):
        with pytest.raises(ValueError, match=DIFFERENT):
            with admission_subject(path, "r1", second):
                pass
        with admission_subject(path, "r1", SHA):
            pass
        # The inner admission of the same subject did not end the outer one.
        store = TaskStore(path)
        assert store.reserve("t", "Review", "r1", {"kind": "fixture"})[0]["subject"] == SHA
    with admission_subject(path, "r1", None):
        with pytest.raises(ValueError, match=DIFFERENT):
            with admission_subject(path, "r1", SHA):
                pass


def test_a_direct_update_of_the_subject_is_aborted(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    with pytest.raises(sqlite3.IntegrityError, match="Task subject is immutable"):
        with store.connection() as db:
            db.execute("UPDATE task_subjects SET subject='forged' WHERE task_id=?", (task["id"],))
    assert store.get(task["id"])["subject"] == SHA


def test_subject_is_never_changed_by_metadata_writes(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    changed = store.metadata(task["id"], {"subject": "forged"})
    assert changed["subject"] == SHA and changed["metadata"]["subject"] == "forged"
    ended = store.update(task["id"], "completed", result="ok")
    assert ended["subject"] == SHA


def test_an_old_store_gains_the_subject_table_and_keeps_its_task_columns(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    TaskStore(path)
    with sqlite3.connect(path) as db:
        before = [row[1] for row in db.execute("PRAGMA table_info(tasks)")]
        db.execute("DROP TRIGGER task_subjects_immutable")
        db.execute("DROP TRIGGER task_subjects_deleted")
        db.execute("DROP TRIGGER task_subjects_kept")
        db.execute("DROP TABLE task_subjects")
        # A positional insert of the eleven task columns, as older code wrote them.
        db.execute(
            "INSERT INTO tasks VALUES ('old','old-1','t','m','completed',1,1,'{}',NULL,NULL,NULL)"
        )
    db.close()
    store = TaskStore(path)
    assert store.get("old")["subject"] is None
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(tasks)")]
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        version = db.execute("PRAGMA user_version").fetchone()[0]
    db.close()
    assert columns == before and "subject" not in columns and version == 0
    assert {"task_subjects", "task_subjects_subject", "task_subjects_immutable"} <= names
    assert {"task_subjects_deleted", "task_subjects_kept"} <= names
    TaskStore(path)  # a second open migrates nothing and does not fail


def test_deleting_a_task_removes_its_subject(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    with store.connection() as db:
        db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
    assert store.by_subject(SHA) == ([], 0)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM task_subjects").fetchone()[0] == 0
    db.close()


def test_a_subject_cannot_be_deleted_and_replaced_while_its_task_exists(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    with pytest.raises(sqlite3.IntegrityError, match="Task subject is immutable"):
        with store.connection() as db:
            db.execute("DELETE FROM task_subjects WHERE task_id=?", (task["id"],))
            db.execute(
                "INSERT INTO task_subjects (task_id, subject) VALUES (?, 'forged')", (task["id"],)
            )
    assert store.get(task["id"])["subject"] == SHA


def test_by_subject_searches_the_whole_store_newest_first(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    first, _ = store.reserve("a", "Review", "first", {"kind": "fixture"}, subject=SHA)
    store.update(first["id"], "completed", result="ok")
    for index in range(105):
        row, _ = store.reserve(f"t{index}", "Noise", f"noise-{index}", {"kind": "fixture"})
        store.update(row["id"], "completed", result="ok")
    second, _ = store.reserve("b", "Review", "second", {"kind": "fixture"}, subject=SHA)
    store.reserve("c", "Review", "prefix", {"kind": "fixture"}, subject=SHA[:7])
    store.reserve("d", "Review", "case", {"kind": "fixture"}, subject=SHA.upper())
    found, total = store.by_subject(SHA, limit=100)
    assert [task["id"] for task in found] == [second["id"], first["id"]] and total == 2
    one, total = store.by_subject(SHA, limit=1)
    assert [task["id"] for task in one] == [second["id"]] and total == 2
    with pytest.raises(ValueError, match=INVALID):
        store.by_subject(" ", limit=1)


async def test_send_stamps_the_receipt_and_task_shows_it(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    try:
        sent = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert sent["subject"] == SHA
        assert (await bridge.get_task(sent["id"]))["subject"] == SHA
        listed = bridge.task_list()["tasks"]
        assert listed[0]["subject"] == SHA
        again = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert again["id"] == sent["id"] and again["subject"] == SHA and adapter.sends == 1
        for other in ("else", None):
            with pytest.raises(ValueError, match=DIFFERENT):
                await bridge.peer_submit("fixture:one", "Review", "r1", subject=other)
        assert adapter.sends == 1
    finally:
        await bridge.close()


async def test_invalid_subject_is_refused_before_any_reservation(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    try:
        with pytest.raises(ValueError, match=INVALID):
            await bridge.peer_submit("fixture:one", "Review", "r1", subject="a\nb")
        assert adapter.sends == 0 and bridge.store.recent() == []
    finally:
        await bridge.close()


async def test_a_background_task_does_not_inherit_the_subject(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    adapter.background_request = "other"
    try:
        await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert bridge.store.by_request("other")["subject"] is None
    finally:
        await bridge.close()


async def test_an_adapter_that_reserves_on_a_thread_keeps_the_subject(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    adapter.reserve_in_thread = True
    try:
        sent = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert sent["subject"] == SHA
    finally:
        await bridge.close()


async def run_cancelled(bridge: Bridge, adapter: SubjectAdapter) -> None:
    """Cancel an admission while its executor job waits to reserve, then let it reserve."""
    adapter.release, adapter.waiting = threading.Event(), threading.Event()
    adapter.finished = threading.Event()
    sending = asyncio.create_task(bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA))
    assert await asyncio.to_thread(adapter.waiting.wait, 5)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    adapter.release.set()
    assert await asyncio.to_thread(adapter.finished.wait, 5)


async def test_an_executor_job_that_outlives_a_cancelled_admission_keeps_the_subject(
    tmp_path: Path,
) -> None:
    import answerback_bridge.subjects as subjects

    bridge, adapter = environment(tmp_path / "config.json")
    try:
        await run_cancelled(bridge, adapter)
        # The reviewer's harness printed `cancelled-thread-subject None` here.
        assert bridge.store.by_request("r1")["subject"] == SHA
        # The reservation settled the kept subject; the store's own rule answers from here.
        assert not subjects._KEPT
        adapter.release = None
        with pytest.raises(ValueError, match=DIFFERENT):
            await bridge.peer_submit("fixture:one", "Review", "r1", subject="else")
    finally:
        await bridge.close()


async def test_a_cancelled_admission_keeps_its_subject_against_another(tmp_path: Path) -> None:
    import answerback_bridge.subjects as subjects

    path = tmp_path / "tasks.sqlite"
    with admission_subject(path, "r1", SHA):
        subjects.keep_after_cancel(path, "r1")
    try:
        with pytest.raises(ValueError, match=DIFFERENT):
            with admission_subject(path, "r1", None):
                pass
        assert (
            TaskStore(path).reserve("t", "Review", "r1", {"kind": "fixture"})[0]["subject"] == SHA
        )
        assert not subjects._KEPT
    finally:
        subjects.settled(path, "r1")


async def test_a_cancelled_admission_that_had_reserved_keeps_nothing(tmp_path: Path) -> None:
    import answerback_bridge.subjects as subjects

    bridge, adapter = environment(tmp_path / "config.json")
    try:
        # The receipt exists before the cancellation: the store's stored subject decides.
        bridge.store.reserve("fixture:one", "Review", "r1", {"kind": "fixture"}, subject=SHA)
        await run_cancelled(bridge, adapter)
        assert not subjects._KEPT
        assert bridge.store.by_request("r1")["subject"] == SHA
    finally:
        await bridge.close()


async def test_an_adapter_that_loses_the_subject_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import answerback_bridge.tasks as tasks

    # As an adapter that reserves through a store the router did not register with.
    monkeypatch.setattr(tasks, "admitted_subject", lambda store, key, subject: subject)
    bridge, adapter = environment(tmp_path / "config.json")
    try:
        with pytest.raises(ValueError, match="^Adapter reserved the receipt without its subject"):
            await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert bridge.store.by_request("r1")["subject"] is None
    finally:
        await bridge.close()


async def test_task_list_filters_by_exact_subject(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    try:
        sent = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        bridge.store.update(sent["id"], "completed", result="ok")
        await bridge.peer_submit("fixture:one", "Review", "r2", subject=SHA[:7])
        listed = bridge.task_list(subject=SHA)
        assert [task["id"] for task in listed["tasks"]] == [sent["id"]]
        assert listed["subject"] == SHA and listed["subject_matches"] == 1
        assert "searched_latest" not in listed
        assert bridge.task_list("nomatch", subject=SHA)["tasks"] == []
        assert bridge.task_list(subject="absent") == {
            "tasks": [],
            "subject": "absent",
            "subject_matches": 0,
        }
        with pytest.raises(ValueError, match=INVALID):
            bridge.task_list(subject="")
    finally:
        await bridge.close()


def test_submission_validates_its_subject() -> None:
    assert Submission("m", "r").subject is None
    assert Submission("m", "r", SHA).subject == SHA
    with pytest.raises(ValueError, match=INVALID):
        Submission("m", "r", "bad\n")


def test_cli_send_and_tasks_take_subject() -> None:
    from answerback_bridge.cli import parser

    sent = parser().parse_args(["send", "x:y", "--message", "m", "--subject", SHA])
    assert sent.subject == SHA
    assert parser().parse_args(["send", "x:y", "--message", "m"]).subject is None
    listed = parser().parse_args(["tasks", "--subject", SHA])
    assert listed.subject == SHA


async def test_mcp_peer_submit_and_task_list_carry_subject(tmp_path: Path) -> None:
    from mcp import Client

    from answerback_bridge.mcp_server import create_server

    bridge, _ = environment(tmp_path / "config.json")
    try:
        async with Client(create_server(bridge, modules=())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("peer_submit", "task_list"):
                assert "subject" in tools[name].input_schema["properties"]
                assert "subject" not in tools[name].input_schema.get("required", [])
            sent = await client.call_tool(
                "peer_submit",
                {
                    "selector": "fixture:one",
                    "message": "Review",
                    "request_id": "m1",
                    "subject": SHA,
                },
            )
            receipt = sent.structured_content
            assert receipt["subject"] == SHA
            got = await client.call_tool("task_get", {"task_id": receipt["id"]})
            assert got.structured_content["subject"] == SHA
            bridge.store.update(receipt["id"], "completed", result="ok")
            await client.call_tool(
                "peer_submit",
                {"selector": "fixture:one", "message": "Other", "request_id": "m2", "subject": "x"},
            )
            listed = (await client.call_tool("task_list", {"subject": SHA})).structured_content
            assert [task["id"] for task in listed["tasks"]] == [receipt["id"]]
            assert listed["subject"] == SHA and listed["subject_matches"] == 1
            assert "searched_latest" not in listed
    finally:
        await bridge.close()


def test_maintenance_prunes_a_receipt_with_a_subject_and_keeps_the_subject(
    tmp_path: Path,
) -> None:
    from answerback_bridge.maintenance import Maintenance

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("kiro:one", "private", "r1", {"kind": "kiro", "cwd": "/p"}, subject=SHA)
    store.update(task["id"], "completed", result="private done")
    with store.connection() as db:
        db.execute("UPDATE tasks SET created=1,updated=2 WHERE id=?", (task["id"],))
        db.execute("UPDATE retention_records SET changed_at=3 WHERE row_id=?", (task["id"],))
    maintenance = Maintenance(tmp_path / "bridge.json")
    plan = maintenance.plan()
    assert [action["id"] for action in plan["actions"]] == [task["id"]]
    assert maintenance.apply(plan)["status"] == "applied"
    pruned = store.get(task["id"])
    assert pruned["message"] == "" and pruned["result"] is None and pruned["subject"] == SHA
    assert [found["id"] for found in store.by_subject(SHA)[0]] == [task["id"]]


def test_overlapping_admissions_keep_the_subject_until_the_last_one_ends(tmp_path: Path) -> None:
    from contextlib import ExitStack

    path = tmp_path / "tasks.sqlite"
    store = TaskStore(path)
    first, second = ExitStack(), ExitStack()
    first.enter_context(admission_subject(path, "r1", SHA))
    second.enter_context(admission_subject(path, "r1", SHA))
    first.close()  # A ends (failed before reserving) while B is still admitting.
    with pytest.raises(ValueError, match=DIFFERENT):
        with admission_subject(path, "r1", "else"):
            pass
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"})
    assert task["subject"] == SHA
    second.close()
    with admission_subject(path, "r1", "else"):  # nothing is held any more
        pass


async def test_a_repeat_admitted_while_the_first_ends_keeps_the_subject(tmp_path: Path) -> None:
    """B enters before A reserves and reserves only after A has ended (red when the first
    admission's exit drops the registration B still relies on)."""
    bridge, adapter = environment(tmp_path / "config.json")
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.submit
    calls = 0

    async def interleaved(
        prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        nonlocal calls
        calls += 1
        if calls == 1:
            await entered.wait()  # A waits until B is inside its admission
        else:
            entered.set()
            await release.wait()  # B waits until A has finished
        return await original(prepared, submission, guard=guard)

    adapter.submit = interleaved  # type: ignore[method-assign]
    try:
        first = asyncio.create_task(bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA))
        second = asyncio.create_task(bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA))
        a = await first
        release.set()
        b = await second
        assert a["id"] == b["id"] and b["subject"] == SHA and adapter.sends == 1
    finally:
        await bridge.close()


async def test_a_held_repeat_is_answered_only_for_the_same_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import answerback_bridge.service as service

    bridge, adapter = environment(tmp_path / "config.json")
    try:
        sent = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        monkeypatch.setattr(service, "hold_for", lambda pressure, peer: object())
        monkeypatch.setattr(
            service, "backoff_result", lambda held, pressure: {"status": "provider_backoff"}
        )
        reached: list[str | None] = []
        repeated = bridge._repeated

        async def spy(peer: dict, message: str, request_id: str, subject: str | None = None):
            reached.append(subject)
            return await repeated(peer, message, request_id, subject)

        monkeypatch.setattr(bridge, "_repeated", spy)
        again = await bridge.peer_submit("fixture:one", "Review", "r1", subject=SHA)
        assert again["id"] == sent["id"] and again["subject"] == SHA
        for other in ("else", None):
            with pytest.raises(ValueError, match=DIFFERENT):
                await bridge.peer_submit("fixture:one", "Review", "r1", subject=other)
        new = await bridge.peer_submit("fixture:one", "Review", "r2", subject=SHA)
        assert new == {"status": "provider_backoff"} and adapter.sends == 1
        # Every repeat was answered by the held path, not by an earlier check.
        assert reached == [SHA, "else", None, SHA]
    finally:
        await bridge.close()


async def test_receipts_without_a_subject_show_null(tmp_path: Path) -> None:
    bridge, _ = environment(tmp_path / "config.json")
    try:
        sent = await bridge.peer_submit("fixture:one", "Review", "r1")
        assert "subject" in sent and sent["subject"] is None
        assert (await bridge.get_task(sent["id"]))["subject"] is None
        summary = bridge.task_list()["tasks"][0]
        assert "subject" in summary and summary["subject"] is None
    finally:
        await bridge.close()


def test_task_list_takes_the_newest_100_subject_matches_and_counts_all(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    store = bridge.store
    ids = []
    for index in range(102):
        target = "findme:old" if index == 0 else f"t{index}"
        row, _ = store.reserve(target, "Review", f"m{index}", {"kind": "f"}, subject=SHA)
        store.update(row["id"], "completed", result="ok")
        ids.append(row["id"])
    for index in range(100):
        row, _ = store.reserve(f"n{index}", "Noise", f"n{index}", {"kind": "f"})
        store.update(row["id"], "completed", result="ok")
        ids.append(row["id"])
    with store.connection() as db:
        for created, ident in enumerate(ids, start=1):
            db.execute("UPDATE tasks SET created=? WHERE id=?", (float(created), ident))
    newest_first = list(reversed(ids[:102]))
    listed = bridge.task_list(limit=100, subject=SHA)
    assert [task["id"] for task in listed["tasks"]] == newest_first[:100]
    assert listed["subject_matches"] == 102
    few = bridge.task_list(limit=3, subject=SHA)
    assert [task["id"] for task in few["tasks"]] == newest_first[:3]
    assert few["subject_matches"] == 102
    # The keyword query narrows the newest 100 subject matches; the oldest two are not among
    # them, so a query that only the oldest matches finds nothing, and the count is unchanged.
    old = bridge.task_list("findme", subject=SHA)
    assert old["tasks"] == [] and old["subject_matches"] == 102
    # Control: the same query does find that receipt when it is among the searched ones.
    with store.connection() as db:
        db.execute("UPDATE tasks SET created=1000 WHERE id=?", (ids[0],))
    found = bridge.task_list("findme", subject=SHA)
    assert [task["id"] for task in found["tasks"]] == [ids[0]]
    # Without a subject the latest 100 receipts are searched: all noise, so none match.
    with store.connection() as db:
        db.execute("UPDATE tasks SET created=1 WHERE id=?", (ids[0],))
    assert bridge.task_list("findme")["tasks"] == []
    assert store.by_subject(SHA, limit=100)[1] == 102


def test_an_admission_that_raises_releases_its_subject(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    with pytest.raises(RuntimeError):
        with admission_subject(path, "r1", SHA):
            raise RuntimeError("adapter failed")
    with admission_subject(path, "r1", "else"):
        pass


@pytest.mark.parametrize(
    ("argv", "method", "expected"),
    [
        (
            ["send", "fixture:one", "--message", "m", "--request-id", "r", "--subject", SHA],
            "peer_submit",
            SHA,
        ),
        (["send", "fixture:one", "--message", "m", "--request-id", "r"], "peer_submit", None),
        (["tasks", "--subject", SHA], "task_list", SHA),
        (["tasks"], "task_list", None),
    ],
)
async def test_cli_forwards_subject_only_when_given(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], method: str, expected: str | None
) -> None:
    import answerback_bridge.cli as cli

    calls: list[tuple[str, dict]] = []

    class Recording:
        def __init__(self, config: object) -> None:
            pass

        async def call(self, name: str, params: dict) -> dict:
            calls.append((name, params))
            return {}

    monkeypatch.setattr(cli, "RemoteBridge", Recording)
    await cli.dispatch(cli.parser().parse_args(argv))
    assert [name for name, _ in calls] == [method]
    params = calls[0][1]
    if expected is None:
        assert "subject" not in params
    else:
        assert params["subject"] == expected
