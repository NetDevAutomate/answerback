"""Pruned task content refuses comparisons while identities and projections survive."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from store_helpers import register_builtin_store_hooks
from test_maintenance import fixture_bus
from test_rex_mailbox import binding

from answerback_bridge.bus.coordinator import Coordinator
from answerback_bridge.bus.model import Command
from answerback_bridge.client_mailbox import ClientMailbox
from answerback_bridge.config import Config, public_task
from answerback_bridge.maintenance import Maintenance
from answerback_bridge.plugin import kernel_context
from answerback_bridge.providers.herdr.core_tasks import CoreTasks
from answerback_bridge.retention import (
    content_matches,
    migrate_content,
    migrate_tasks,
    task_retention,
)
from answerback_bridge.rex_mailbox import RexMailbox
from answerback_bridge.tasks import TaskStore

MESSAGE = "résumé e\u0301\n \t終"
RESULT = "完成\n with spaces "
PRUNED_REFUSAL = "Pruned task content cannot be verified; inspect the existing receipt"


def age(store: TaskStore) -> None:
    with store.connection() as db:
        migrate_tasks(db)
        db.execute("UPDATE tasks SET created=1,updated=2")
        db.execute("UPDATE retention_records SET changed_at=3")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='rex_peers'").fetchone():
            db.execute("UPDATE rex_peers SET created=1,updated=2")


def pruned_task(
    tmp_path: Path,
    target: str = "herdr:one",
    metadata: dict | None = None,
    request: str = "request",
) -> tuple[TaskStore, str]:
    store = register_builtin_store_hooks(TaskStore(tmp_path / "tasks.sqlite"))
    task, _ = store.reserve(target, MESSAGE, request, metadata or {"kind": "herdr"})
    store.update(task["id"], "completed", result=RESULT)
    age(store)
    plan = Maintenance(tmp_path / "bridge.json").plan()
    assert plan["actions"]
    Maintenance(tmp_path / "bridge.json").apply(plan)
    return store, task["id"]


@pytest.mark.parametrize("field", ["message", "result", "detail"])
@pytest.mark.parametrize("original", [MESSAGE, "", None])
def test_pruned_content_refuses_every_guess(field: str, original: str | None) -> None:
    """red_at_base: exact and different guesses receive the same refusal for every field."""
    task = {field: original}
    retained = {field: None, "_retention": task_retention(task, 1.0)}
    messages = []
    for candidate in (original, "different content"):
        with pytest.raises(ValueError) as error:
            content_matches(retained, field, candidate)
        messages.append(str(error.value))
    assert messages == [PRUNED_REFUSAL, PRUNED_REFUSAL]


@pytest.mark.parametrize("original", [MESSAGE, "", None])
def test_live_content_keeps_exact_comparison(original: str | None) -> None:
    """pins_existing: unpruned content still supports exact idempotency."""
    for field in ("message", "result", "detail"):
        assert content_matches({field: original}, field, original)
        assert not content_matches({field: original}, field, "different content")


def test_task_retry_completion_and_public_presence_do_not_restore_bodies(tmp_path: Path) -> None:
    """red_at_base: pruned retries refuse without changing receipts or disclosure."""
    store, ident = pruned_task(tmp_path)
    before = store.get(ident)
    for message in (MESSAGE, MESSAGE + " ", MESSAGE.replace("e\u0301", "é")):
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            store.reserve("herdr:one", message, "request", {"kind": "herdr"})
    for result in (RESULT, RESULT.rstrip()):
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            store.complete_worker(ident, result)
    assert store.get(ident) == before
    receipt = public_task(before)
    assert receipt["content_pruned"] is True
    assert receipt["has_result"] is True and receipt["result_available"] is False
    assert receipt["result"] is None and "message" not in receipt and "_retention" not in receipt
    for target, message, metadata in [
        ("herdr:two", MESSAGE, {"kind": "herdr"}),
        ("herdr:one", MESSAGE, {"kind": "crew"}),
    ]:
        with pytest.raises(ValueError, match="different"):
            store.reserve(target, message, "request", metadata)
    assert len(store.recent()) == 1


async def test_core_previous_and_submit_skip_native_after_pruning(tmp_path: Path) -> None:
    """red_at_base: both Core retry seams refuse before native work."""
    store, ident = pruned_task(tmp_path, "hc:default:one", {"kind": "hc"})
    core = SimpleNamespace(session=None, peer=AsyncMock(side_effect=AssertionError("native call")))
    service = CoreTasks(store, tmp_path / "bridge.json")
    for message in (MESSAGE, MESSAGE + " "):
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            service.previous(core, "one", message, "request")
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            await service.submit(core, "one", message, "request")
    assert store.get(ident)["result"] is None and len(store.recent()) == 1
    core.peer.assert_not_called()


async def test_codex_previous_router_previous_and_submit_skip_native(tmp_path: Path) -> None:
    """red_at_base: Codex and generic router retries refuse without native reads."""
    from answerback_bridge.adapters.codex import CodexAdapter
    from answerback_bridge.adapters.contracts import Peer, PreparedPeer, Submission
    from answerback_bridge.adapters.registry import AdapterRegistry
    from answerback_bridge.adapters.router import AdapterRouter

    chat = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    store, ident = pruned_task(tmp_path, "desktop:" + chat, {"kind": "desktop", "thread_id": chat})
    config = Config(provider_selection=["codex"], desktop_threads={"one": chat})
    from answerback_bridge.desktop import DesktopIndex

    native = DesktopIndex("fixture-no-native")
    native.read = AsyncMock(side_effect=AssertionError("native call"))
    adapter = CodexAdapter(
        kernel_context(config, tmp_path / "bridge.json", store, locks=[asyncio.Lock()]),
        lambda: native,
    )
    router = AdapterRouter(AdapterRegistry([adapter]), store)
    submission = Submission(MESSAGE, "request")
    with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
        adapter.previous("desktop:one", submission)
    with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
        router.previous("desktop:one", submission)
    peer = Peer.from_record(
        {
            "peer_id": "desktop:one",
            "provider": "desktop",
            "target": "one",
            "thread_id": chat,
            "kind": "paired_desktop",
        }
    )
    with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
        await router.submit(PreparedPeer("desktop:" + chat, peer), submission)
    assert store.get(ident)["result"] is None and len(store.recent()) == 1
    native.read.assert_not_called()


async def test_bus_replay_projection_capacity_and_lookup_survive_pruning(tmp_path: Path) -> None:
    """red_at_base: projections survive, but content-based task lookup refuses."""
    from answerback_bridge.adapters.contracts import TaskReceipt
    from answerback_bridge.bus.gateway import BridgeGateway

    bus, tasks, command, task = fixture_bus(tmp_path)
    envelope = bus.command(command)
    previous_snapshot = tasks.get(task)
    Maintenance(tmp_path / "bridge.json").apply(Maintenance(tmp_path / "bridge.json").plan())
    from answerback_bridge.adapters.contracts import PreparedPeer
    from answerback_bridge.service import Bridge

    class FixtureGateway:
        calls = 0

        async def prepare(self, command: Command) -> PreparedPeer:
            self.calls += 1
            raise AssertionError("redispatch")

        async def send(self, target: PreparedPeer, command: Command) -> TaskReceipt:
            raise AssertionError("redispatch")

        def lookup(self, command: Command) -> TaskReceipt | None:
            return None

        def get(self, task_id: str) -> TaskReceipt:
            return TaskReceipt.from_record(tasks.get(task_id))

    gateway = FixtureGateway()
    coordinator = Coordinator(bus, gateway)
    coordinator.project_receipts()
    assert not bus.project_task(command, previous_snapshot)
    assert bus.events()["events"] == [] and bus.pending_outbox() == []
    await coordinator.process(envelope.to_bytes())
    assert gateway.calls == 0
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "bridge.json")
    try:
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            BridgeGateway(bridge).lookup(envelope)
        assert bridge.store.get(task)["result"] is None
    finally:
        await bridge.close()
    altered = json.loads(envelope.to_bytes())
    altered["message"] += "!"
    with pytest.raises(ValueError, match="match"):
        await coordinator.process(Command.from_bytes(json.dumps(altered).encode()).to_bytes())
    with pytest.raises(ValueError, match="content"):
        bus.command(command)
    repeated = bus.submit(
        request_id="cmd",
        peer_id="kiro:one",
        project="/project",
        routing_digest="a" * 64,
        message="work",
        ttl_seconds=60,
    )
    assert repeated["command_id"] == command
    for changes in (
        {"message": "work "},
        {"ttl_seconds": 61},
        {"routing_digest": "b" * 64},
        {"project": "/project-two"},
        {"peer_id": "kiro:two"},
    ):
        values = {
            "request_id": "cmd",
            "peer_id": "kiro:one",
            "project": "/project",
            "routing_digest": "a" * 64,
            "message": "work",
            "ttl_seconds": 60,
            **changes,
        }
        with pytest.raises(ValueError, match="different"):
            bus.submit(**values)
    bus.max_commands = 1
    assert (
        bus.submit(
            request_id="new",
            peer_id="kiro:two",
            project="/project",
            routing_digest="a" * 64,
            message="next",
        )["command_id"]
        != command
    )


def test_acknowledged_notification_retry_does_not_reappear(tmp_path: Path) -> None:
    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    notice = mailbox.notify("codex", MESSAGE, "one", "testing")
    mailbox.acknowledge("codex", [notice["id"]])
    with mailbox._connect() as db:
        migrate_content(db, "messages")
        db.execute("UPDATE messages SET created=1,acknowledged=2")
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert maintenance.apply(maintenance.plan())["status"] == "applied"
    assert mailbox.notify("codex", MESSAGE, "one", "testing")["id"] == notice["id"]
    assert mailbox.pending("codex")["messages"] == []
    for message, topic in ((MESSAGE + " ", "testing"), (MESSAGE, "other")):
        with pytest.raises(ValueError, match="different"):
            mailbox.notify("codex", message, "one", topic)


def test_rex_retry_completion_and_enrollment_survive_pruning(tmp_path: Path) -> None:
    """red_at_base: enrollment persists; pruned submission/completion retries refuse."""
    mailbox = RexMailbox(TaskStore(tmp_path / "tasks.sqlite"))
    peer = mailbox.enroll("test", binding(), "enroll")
    task = mailbox.submit(peer["peer_id"], MESSAGE, "request")
    mailbox.acknowledge(peer["peer_id"], task["id"])
    mailbox.complete(peer["peer_id"], task["id"], RESULT)
    mailbox.retire(peer["peer_id"])
    age(mailbox.store)
    maintenance = Maintenance(tmp_path / "bridge.json")
    maintenance.apply(maintenance.plan())
    for message in (MESSAGE, MESSAGE + " "):
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            mailbox.submit(peer["peer_id"], message, "request")
    for result in (RESULT, RESULT + " "):
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            mailbox.complete(peer["peer_id"], task["id"], result)
    assert mailbox.store.get(task["id"])["result"] is None
    assert mailbox.get(peer["peer_id"])["state"] == "retired"
    assert mailbox.enroll("test", binding(), "enroll")["peer_id"] == peer["peer_id"]


async def test_bus_result_monitor_and_task_list_disclose_pruned_results(tmp_path: Path) -> None:
    from answerback_bridge.bus.api import BusAPI
    from answerback_bridge.bus.config import BusConfig
    from answerback_bridge.bus.monitor import Monitor
    from answerback_bridge.service import Bridge

    bus, tasks, command, task = fixture_bus(tmp_path)
    config_path = tmp_path / "bridge.json"
    bus_config = BusConfig(config_path, 4222, "/fixture/no-native")
    (tmp_path / "bus" / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "port": 4222,
                "server_binary": "/fixture/no-native",
                "namespace": bus_config.namespace,
            }
        )
    )
    (tmp_path / "bus" / "token").write_text("a" * 64)
    for name in ("config.json", "token"):
        (tmp_path / "bus" / name).chmod(0o600)
    high = bus.events()["events"][-1]["seq"]
    maintenance = Maintenance(config_path)
    maintenance.apply(maintenance.plan())
    receipt = BusAPI(config_path).result(command)
    assert receipt["content_pruned"] is True
    assert receipt["task"]["has_result"] is True
    assert receipt["task"]["result_available"] is False
    page = bus.events(after=0)
    assert page["retention_gap"] is True and page["latest_cursor"] == high
    frame = await Monitor(config_path).snapshot(after=0)
    assert frame["available"] is True
    assert frame["retention_gap"] is True and frame["latest_cursor"] == high
    assert frame["stats"]["retained_commands"] == 1
    bridge = Bridge(Config(provider_selection=[]), config_path)
    try:
        summary = bridge.task_list()["tasks"][0]
        assert summary["has_result"] is True and summary["result_available"] is False
    finally:
        await bridge.close()


def test_real_invalidation_clears_frozen_projection_and_current_completion(tmp_path: Path) -> None:
    bus, tasks, command, task = fixture_bus(tmp_path)
    maintenance = Maintenance(tmp_path / "bridge.json")
    maintenance.apply(maintenance.plan())
    corrected = tasks.invalidate_submission(task, "late identity contradiction")
    receipt = public_task(corrected)
    assert receipt["status"] == "uncertain" and receipt["has_result"] is False
    assert receipt["result_available"] is False
    assert bus.project_task(command, corrected)
    event = bus.events()["events"][-1]
    assert event["type"] == "task.uncertain"
    assert event["payload"]["result_ref"] is None
    assert maintenance.plan()["actions"] == []
    with pytest.raises(ValueError):
        tasks.complete_worker(task, "done")


def test_concurrent_exact_apply_and_task_retry_never_split_identity(tmp_path: Path) -> None:
    """pins_existing: a race either retries before pruning or refuses after it."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("herdr:one", MESSAGE, "request", {"kind": "herdr"})
    store.update(task["id"], "completed", result=RESULT)
    age(store)
    maintenance = Maintenance(tmp_path / "bridge.json", lock_timeout=5)
    plan = maintenance.plan()
    gate = Barrier(6)

    def run(number: int) -> str:
        gate.wait()
        if number % 2:
            try:
                retry, created = store.reserve("herdr:one", MESSAGE, "request", {"kind": "herdr"})
            except ValueError as error:
                assert str(error) == PRUNED_REFUSAL
                return "pruned"
            assert not created and retry["id"] == task["id"]
            return "retry"
        return maintenance.apply(plan)["status"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert results.count("applied") == 1
    assert results.count("already_applied") == 2
    assert results.count("retry") + results.count("pruned") == 3
    assert len(store.recent()) == 1
    assert store.get(task["id"])["result"] is None


def test_notification_races_keep_one_acknowledged_identity(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    mailbox = ClientMailbox(tmp_path / "client-mailbox.sqlite")
    notice = mailbox.notify("codex", MESSAGE, "one")
    mailbox.acknowledge("codex", [notice["id"]])
    with mailbox._connect() as db:
        migrate_content(db, "messages")
        db.execute("UPDATE messages SET created=1,acknowledged=2")
    maintenance = Maintenance(tmp_path / "bridge.json", lock_timeout=5)
    plan = maintenance.plan()
    gate = Barrier(3)

    def run(number: int) -> str:
        gate.wait()
        if number == 0:
            return maintenance.apply(plan)["status"]
        if number == 1:
            assert mailbox.notify("codex", MESSAGE, "one")["id"] == notice["id"]
        else:
            mailbox.acknowledge("codex", [notice["id"]])
        return "retry"

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert "applied" in list(pool.map(run, range(3)))
    assert mailbox.pending("codex")["messages"] == []
    with mailbox._connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_unused_sqlite_sequence_numbers_are_not_reported_as_retention(tmp_path: Path) -> None:
    from answerback_bridge.bus.store import BusStore

    bus = BusStore(tmp_path / "bus" / "ledger.sqlite", clock=lambda: 100)
    bus.submit(
        request_id="one",
        peer_id="kiro:one",
        project="/project",
        routing_digest="a" * 64,
        message="work",
    )
    with bus.connection() as db:
        row = db.execute("SELECT id,command_id,type,payload,created FROM events").fetchone()
        db.execute(
            "INSERT OR IGNORE INTO events(id,command_id,type,payload,created) VALUES (?,?,?,?,?)",
            tuple(row),
        )
    assert bus.events()["retention_gap"] is False


async def test_rex_adapter_and_service_retries_never_probe_native_after_pruning(
    tmp_path: Path,
) -> None:
    """red_at_base: every Rex task retry seam refuses before native observation."""
    from answerback_bridge.adapters.contracts import Submission
    from answerback_bridge.service import Bridge

    config = Config(
        provider_selection=["rex"],
        rex_enabled=True,
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="host-1",
    )
    bridge = Bridge(config, tmp_path / "bridge.json")
    mailbox = bridge.rex.mailbox
    peer = mailbox.enroll("test", binding(), "enroll")
    task = mailbox.submit(peer["peer_id"], MESSAGE, "request")
    mailbox.acknowledge(peer["peer_id"], task["id"])
    mailbox.complete(peer["peer_id"], task["id"], RESULT)
    age(bridge.store)
    maintenance = Maintenance(tmp_path / "bridge.json")
    maintenance.apply(maintenance.plan())
    native = AsyncMock(side_effect=AssertionError("native retry"))
    bridge.rex.adapter.terminal = native
    try:
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            bridge.rex.previous(peer["peer_id"], MESSAGE, "request")
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            bridge.router.previous(peer["peer_id"], Submission(MESSAGE, "request"))
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            await bridge.rex.submit(peer["peer_id"], MESSAGE, "request")
        with pytest.raises(ValueError, match=f"^{PRUNED_REFUSAL}$"):
            await bridge.rex.complete(peer["peer_id"], task["id"], RESULT)
        assert bridge.store.get(task["id"])["result"] is None and len(bridge.store.recent()) == 1
        native.assert_not_called()
    finally:
        await bridge.close()
