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
        if self.reserve_in_thread:
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
    assert "task_subjects_deleted" in names
    TaskStore(path)  # a second open migrates nothing and does not fail


def test_deleting_a_task_removes_its_subject(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("t", "Review", "r1", {"kind": "fixture"}, subject=SHA)
    with store.connection() as db:
        db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
    assert store.by_subject(SHA) == ([], 0)


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
        assert again["id"] == sent["id"] and adapter.sends == 1
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
            listed = await client.call_tool("task_list", {"subject": SHA})
            assert [task["id"] for task in listed.structured_content["tasks"]] == [receipt["id"]]
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
