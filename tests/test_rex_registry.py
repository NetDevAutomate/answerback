import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_rex_mailbox import binding

from answerback_bridge.adapters.catalog import definition
from answerback_bridge.adapters.contracts import Peer, Submission
from answerback_bridge.adapters.registry import AdapterRegistry
from answerback_bridge.adapters.rex import RexAdapter, RexDiscovery
from answerback_bridge.adapters.router import AdapterRouter
from answerback_bridge.bus.config import routing_digest
from answerback_bridge.config import Config
from answerback_bridge.plugin import kernel_context
from answerback_bridge.process import BridgeError
from answerback_bridge.provider_registry import ProviderRegistry
from answerback_bridge.rex_service import RexService
from answerback_bridge.tasks import TaskStore


class Native:
    def __init__(self):
        self.binding = binding()
        self.started = None
        self.release = None

    async def terminal(self, session_id, block_id):
        if self.started:
            self.started.set()
            await self.release.wait()
        return {"binding": dict(self.binding), "foreground": {"user_id": os.getuid()}}

    async def inventory(self):
        return {"peers": [], "complete": False, "errors": [{"error": "partial"}]}


@pytest.fixture
def owner(tmp_path):
    config = Config(
        codex_enabled=False,
        provider_selection=["rex"],
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="host-1",
    )
    store = TaskStore(tmp_path / "tasks.sqlite")
    owner = SimpleNamespace(
        config=config,
        config_path=tmp_path / "config.json",
        store=store,
        provider_status={"rex": {}},
        providers=ProviderRegistry(config, tmp_path / "config.json", store),
    )
    owner.rex = RexService(owner)
    owner.rex.adapter = Native()
    owner.adapter = RexAdapter(kernel_context(config, owner.config_path, store), lambda: owner.rex)
    owner.router = AdapterRouter(AdapterRegistry([owner.adapter]), store)
    return owner


async def enrolled(owner):
    target = owner.rex.adapter.binding
    row = await owner.rex.enroll("reviewer", target["session_id"], target["block_id"], "enroll")
    return Peer.from_record(await owner.rex.peer(row["peer_id"]))


@pytest.fixture
def native_row(owner):
    b = owner.rex.adapter.binding.copy()
    b["foreground_cwd"] = "/t68-private-project"
    row = {
        "provider": "rex",
        "peer_id": f"rex:terminal:{b['session_id']}/{b['block_id']}",
        "session_id": b["session_id"],
        "block_id": b["block_id"],
        "binding": b,
        "foreground": {"user_id": os.getuid(), "pid": 42, "name": "editor"},
        "project": b["foreground_cwd"],
        "label": "t68-private-label",
        "display_name": "t68-private-display",
        "owner": {"name": "t68-private-owner"},
        "placement": "detached",
        "window_id": "t68-private-window",
        "capabilities": {"read": True, "enroll": True},
    }

    async def inventory():
        return {"peers": [row], "complete": True, "errors": []}

    owner.rex.adapter.inventory = inventory
    return row


@pytest.mark.parametrize("surface", ["service", "discovery", "standalone"])
@pytest.mark.parametrize(
    "foreground",
    [
        None,
        {},
        {"user_id": None},
        {"user_id": str(os.getuid())},
        {"user_id": True},
        {"user_id": os.getuid() + 1},
    ],
)
async def test_public_inventory_requires_foreground_owner(owner, native_row, surface, foreground):
    """red_at_base: public listings disclose no identifiers without an owned foreground."""
    native_row["foreground"] = foreground
    if surface == "service":
        result = await owner.rex.inventory()
    else:
        adapter = (
            owner.adapter
            if surface == "discovery"
            else RexDiscovery(owner.config, lambda: owner.rex.adapter)
        )
        result = (await adapter.discover()).as_record()
    assert result["peers"] == []
    assert result["complete"] is False
    # pins_existing (MM3): preserve the diagnostic consumed by public error mapping.
    diagnostic = "Rex terminals withheld: foreground ownership by this OS user is unverified"
    assert result["errors"] == (
        [{"error": diagnostic}] if surface == "service" else {"rex:0": diagnostic}
    )
    encoded = json.dumps(result)
    for value in (
        native_row["peer_id"],
        native_row["session_id"],
        native_row["block_id"],
        native_row["label"],
        native_row["project"],
        native_row["owner"]["name"],
        native_row["window_id"],
    ):
        assert value not in encoded


async def test_owned_inventory_keeps_exact_public_row(owner, native_row):
    """pins_existing: owned native metadata and capability shape stay unchanged."""
    expected = {
        **native_row,
        "target": native_row["peer_id"],
        "name": native_row["display_name"],
        "kind": "unenrolled_terminal",
        "capabilities": ["read", "enroll"],
    }
    assert await owner.rex.inventory() == {"peers": [expected], "complete": True, "errors": []}
    page = await owner.adapter.discover()
    assert [peer.as_record() for peer in page.peers] == [expected]
    assert page.errors == ()


@pytest.mark.parametrize("surface", ["service", "discovery", "standalone"])
async def test_boolean_uid_is_not_public_owner(owner, native_row, monkeypatch, surface):
    """pins_existing: True must not authorize a public row even when the OS uid is 1."""
    monkeypatch.setattr("answerback_bridge.rex.os.getuid", lambda: 1)
    await test_public_inventory_requires_foreground_owner(
        owner, native_row, surface, {"user_id": True}
    )


async def test_foreign_uid_is_reconciled_before_public_filter(owner, native_row):
    """pins_existing: filtering must not hide positive owner-change evidence."""
    peer = await enrolled(owner)
    task = await owner.rex.submit(peer.peer_id, "work", "work")
    native_row["binding"] = peer.as_record()["binding"].copy()
    native_row["foreground"] = {"user_id": os.getuid() + 1}

    result = await owner.rex.inventory()

    saved = owner.rex.mailbox.get(peer.peer_id)
    assert saved["state"] == "stale"
    receipt = owner.store.get(task["id"])
    assert receipt["detail"] == "Observed Rex terminal binding changed"
    assert receipt["status"] == "uncertain"
    assert len(result["peers"]) == 1
    assert result["peers"][0]["peer_id"] == peer.peer_id
    assert result["peers"][0]["verified"] is False
    assert result["peers"][0]["capabilities"] == []


async def test_unverified_enrollment_is_not_reported_as_withheld(owner, native_row):
    """red_at_base (round 2): a returned enrollment needs no withheld-row diagnostic."""
    peer = await enrolled(owner)
    native_row["binding"] = peer.as_record()["binding"].copy()
    native_row["foreground"] = None

    result = await owner.rex.inventory()

    assert len(result["peers"]) == 1
    assert result["peers"][0]["peer_id"] == peer.peer_id
    assert result["peers"][0]["verified"] is False
    assert result["peers"][0]["capabilities"] == []
    assert owner.rex.mailbox.get(peer.peer_id)["state"] == "active"
    assert result["complete"] is False
    assert result["errors"] == [
        {"peer_id": peer.peer_id, "error": "Rex terminal identity evidence is incomplete"}
    ]


@pytest.mark.parametrize("form", ["name", "relative", "relative_search"])
async def test_configure_pins_executable_across_path_changes(owner, tmp_path, monkeypatch, form):
    """red_at_base: configuration and restart keep executing the selected fixture."""
    selected = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    for directory, source in ((selected, "selected"), (replacement, "replacement")):
        directory.mkdir()
        executable = directory / "rex-fixture"
        executable.write_text(f"""#!/bin/sh\nprintf '%s' '{{"source":"{source}"}}'\n""")
        executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "selected" if form == "relative_search" else str(selected))
    binary = "./selected/rex-fixture" if form == "relative" else "rex-fixture"
    probed = []

    async def probe(adapter):
        probed.append(adapter.binary)
        # Change PATH during the probe, after the configuration decision.
        monkeypatch.setenv("PATH", str(replacement))
        return {"endpoint": "unix:///tmp/rex.sock", "server_host_id": "host-1"}

    monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
    result = await owner.rex.configure(binary)
    assert await owner.rex.adapter._cli("whoami", "--json") == {"source": "selected"}
    resolved = str(selected / "rex-fixture")
    assert result["binary"] == resolved  # red_at_base (MM3): expose the selected path.
    assert probed == [resolved]
    assert owner.config.rex_binary == resolved
    owner.config = Config.load(owner.config_path)
    assert owner.config.rex_binary == resolved
    assert await RexService(owner).adapter._cli("whoami", "--json") == {"source": "selected"}
    assert Path(resolved).is_absolute()


@pytest.mark.parametrize("change", ["endpoint", "host", "pinned_binary"])
@pytest.mark.parametrize("unfinished", [False, True])
async def test_configure_binding_change_keeps_task_protection(
    owner, monkeypatch: pytest.MonkeyPatch, change: str, unfinished: bool
) -> None:
    """pins_existing: server changes and replacing a pinned binary remain binding changes."""
    if change == "pinned_binary":
        owner.config.rex_binary = "/fixture/previous-rex"
        owner.rex.adapter_binding = (
            owner.config.rex_binary,
            owner.config.rex_server,
            owner.config.rex_host_id,
        )
    peer = await enrolled(owner)
    task = await owner.rex.submit(peer.peer_id, "work", "work")
    if not unfinished:
        await owner.rex.ack(peer.peer_id, task["id"])
        await owner.rex.complete(peer.peer_id, task["id"], "done")
    peers_before = owner.rex.mailbox.peers()
    task_before = owner.store.get(task["id"])
    owner.config.save(owner.config_path)
    config_before = owner.config_path.read_bytes()
    adapter_before = owner.rex.adapter
    observation = {
        "endpoint": "unix:///tmp/changed.sock" if change == "endpoint" else owner.config.rex_server,
        "server_host_id": "changed-host" if change == "host" else owner.config.rex_host_id,
    }

    async def probe(adapter):
        assert adapter.binary == "/fixture/selected-rex"
        return observation

    monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
    monkeypatch.setattr("shutil.which", lambda _: "/fixture/selected-rex")
    if unfinished:
        with pytest.raises(
            ValueError, match="^Reconcile unfinished Rex tasks before changing its binding$"
        ):
            await owner.rex.configure("selected-rex", observation["endpoint"])
        assert owner.config_path.read_bytes() == config_before
        assert owner.rex.adapter is adapter_before
        assert owner.rex.mailbox.peers() == peers_before
    else:
        await owner.rex.configure("selected-rex", observation["endpoint"])
        assert owner.rex.mailbox.get(peer.peer_id)["state"] == "stale"
        saved = Config.load(owner.config_path)
        assert saved.rex_binary == "/fixture/selected-rex"
        assert saved.rex_server == observation["endpoint"]
        assert saved.rex_host_id == observation["server_host_id"]
    assert owner.store.get(task["id"]) == task_before


async def test_configure_missing_executable_refused_before_probe(owner, monkeypatch):
    """red_at_base: a missing executable cannot be probed or persisted."""
    probed = []

    async def probe(adapter):
        probed.append(adapter.binary)
        return {"endpoint": "unix:///tmp/rex.sock", "server_host_id": "host-1"}

    monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="Rex executable is unavailable"):
        await owner.rex.configure("missing-rex-fixture")
    assert probed == []
    assert not owner.config_path.exists()


def test_opt_in_selection_and_legacy_flag():
    assert not Config().provider_enabled("rex")
    assert Config(rex_enabled=True).provider_enabled("rex")
    assert not Config(rex_enabled=True, provider_selection=[]).provider_enabled("rex")
    assert definition("rex").worker_reports is False
    assert definition("rex").recovery_states == ()


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_enablement_is_strict(flag):
    with pytest.raises(ValueError):
        Config(rex_enabled=flag)


@pytest.mark.parametrize(
    "endpoint", ["http://localhost", "unix://remote/tmp/a", "unix:///tmp/a?x=1", "unix:///tmp/a#x"]
)
def test_endpoint_is_strict(endpoint):
    with pytest.raises(ValueError):
        Config(rex_server=endpoint)


def test_routing_digest_covers_rex_fields(owner):
    initial = routing_digest(owner.config)
    for field, value in (
        ("rex_binary", "/different/rex"),
        ("rex_server", "unix:///tmp/new.sock"),
        ("rex_host_id", "different"),
        ("rex_enabled", True),
    ):
        assert routing_digest(replace(owner.config, **{field: value})) != initial


def test_generic_registration_requires_probe_setup(owner):
    with pytest.raises(ValueError, match="rex-configure"):
        owner.providers.register("rex")


async def test_router_exact_retry_and_cooperative_result(owner):
    peer = await enrolled(owner)
    assert (await owner.adapter.resolve_exact(peer.peer_id)).peer_id == peer.peer_id
    prepared = await owner.router.prepare(peer, expected_project="/project")
    submission = Submission("Unicode assignment 🦊\n", "exact-request")
    receipt = await owner.router.submit(prepared, submission)
    inbox = await owner.rex.receive(peer.peer_id)
    assert inbox["tasks"][0]["message"] == submission.message
    assert inbox["tasks"][0]["id"] == receipt.task_id
    with pytest.raises(ValueError, match="acknowledgment"):
        await owner.rex.complete(peer.peer_id, receipt.task_id, "🦊 résultat\n")
    with pytest.raises(ValueError):
        owner.store.complete_worker(receipt.task_id, "bypass")
    await owner.rex.ack(peer.peer_id, receipt.task_id)
    completed = await owner.rex.complete(peer.peer_id, receipt.task_id, "🦊 résultat\n")
    owner.config.select_provider("rex", False)
    assert owner.router.previous(peer.peer_id, submission).task_id == receipt.task_id
    assert (await owner.rex.complete(peer.peer_id, receipt.task_id, "🦊 résultat\n")) == completed
    with pytest.raises(ValueError):
        await owner.rex.complete(peer.peer_id, receipt.task_id, "changed")
    assert owner.adapter.matches(peer, receipt)
    assert owner.adapter.controls is None
    await owner.adapter.close()


async def test_raw_rows_keep_unknown_project_and_refuse_queue(owner):
    b = owner.rex.adapter.binding
    raw = {
        "peer_id": f"rex:terminal:{b['session_id']}/{b['block_id']}",
        "binding": b,
        "foreground": {"user_id": os.getuid()},
        "project": None,
        "capabilities": {"read": True, "enroll": False},
    }

    async def inventory():
        return {"peers": [raw], "complete": False, "errors": [{"error": "metadata unknown"}]}

    owner.rex.adapter.inventory = inventory
    page = await owner.adapter.discover()
    assert page.peers[0].project == ""
    assert page.errors and page.as_record()["complete"] is False
    assert page.peers[0].as_record()["capabilities"] == ["read"]
    with pytest.raises(ValueError, match="enroll"):
        await owner.adapter.prepare(page.peers[0])


@pytest.mark.parametrize(
    "change",
    [
        lambda config: config.select_provider("rex", False),
        lambda config: setattr(config, "rex_binary", "/replacement/rex"),
        lambda config: setattr(config, "rex_server", "unix:///tmp/replaced.sock"),
        lambda config: setattr(config, "rex_host_id", "replaced"),
    ],
)
async def test_final_native_await_rechecks_admission(owner, change):
    peer = await enrolled(owner)
    prepared = await owner.router.prepare(peer)
    native = owner.rex.adapter
    native.started, native.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(owner.router.submit(prepared, Submission("work", "race")))
    await native.started.wait()
    change(owner.config)
    native.release.set()
    with pytest.raises(ValueError):
        await task
    assert owner.store.by_request("race") is None
    # A configuration race is not evidence that the old enrollment disappeared.
    assert owner.rex.mailbox.get(peer.peer_id)["state"] == "active"


async def test_prepared_binding_and_final_guard(owner):
    peer = await enrolled(owner)
    prepared = await owner.router.prepare(peer)
    native = owner.rex.adapter
    native.started, native.release = asyncio.Event(), asyncio.Event()
    guards = []
    task = asyncio.create_task(
        owner.router.submit(
            prepared, Submission("work", "guard"), before_admission=lambda: guards.append(1)
        )
    )
    await native.started.wait()
    assert guards
    native.release.set()
    await task
    assert len(guards) >= 2
    native.binding["foreground_cwd"] = "/changed"
    with pytest.raises(ValueError):
        await owner.router.submit(prepared, Submission("next", "cwd"))
    assert owner.store.by_request("cwd") is None


async def test_transient_failure_does_not_invalidate(owner):
    peer = await enrolled(owner)

    async def unavailable(*args):
        raise BridgeError("temporarily offline")

    owner.rex.adapter.terminal = unavailable
    with pytest.raises(BridgeError):
        await owner.rex.receive(peer.peer_id)
    assert owner.rex.mailbox.get(peer.peer_id)["state"] == "active"


async def test_enroll_native_await_rechecks_configuration(owner):
    native = owner.rex.adapter
    native.started, native.release = asyncio.Event(), asyncio.Event()
    target = native.binding
    task = asyncio.create_task(
        owner.rex.enroll("reviewer", target["session_id"], target["block_id"], "race-enroll")
    )
    await native.started.wait()
    owner.config.select_provider("rex", False)
    native.release.set()
    with pytest.raises(ValueError):
        await task
    assert owner.rex.mailbox.peers() == []


async def test_transaction_helper_requires_begun_transaction_and_rolls_back(owner):
    with owner.store.connection() as db:
        with pytest.raises(ValueError, match="transaction"):
            owner.store.reserve_in_transaction(db, "target", "work", "outside")
    with pytest.raises(RuntimeError):
        with owner.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row, created = owner.store.reserve_in_transaction(
                db, "target", "work", "rollback", initial_state="queued"
            )
            assert created and row["status"] == "queued"
            raise RuntimeError("rollback")
    assert owner.store.by_request("rollback") is None


@pytest.mark.parametrize(
    "field,value", [("session_id", "session:short"), ("block_id", "block:short")]
)
def test_mailbox_requires_full_ids(owner, field, value):
    with pytest.raises((ValueError, BridgeError)):
        owner.rex.mailbox.enroll("reviewer", binding(**{field: value}), "short")


@pytest.mark.parametrize("endpoint", ["unix:///tmp/a?x=1", "unix:///tmp/a#x"])
def test_native_endpoint_rejects_query_fragment(endpoint):
    from answerback_bridge.rex import Rex

    with pytest.raises(BridgeError):
        Rex(server=endpoint)


async def test_rebind_before_operation_refuses_stale_native(owner):
    peer = await enrolled(owner)
    owner.config.rex_binary = "/replacement/rex"
    with pytest.raises(ValueError, match="configuration|binding"):
        await owner.rex.receive(peer.peer_id)


async def test_configure_does_not_overwrite_concurrent_rebind(owner, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class Probe:
        def __init__(self, binary, server):
            pass

        async def probe(self):
            started.set()
            await release.wait()
            return {"endpoint": "unix:///tmp/proposed.sock", "server_host_id": "new"}

    monkeypatch.setattr("answerback_bridge.rex_service.Rex", Probe)
    monkeypatch.setattr("answerback_bridge.rex.shutil.which", lambda binary: "/fixture/" + binary)
    task = asyncio.create_task(owner.rex.configure("new"))
    await started.wait()
    owner.config.rex_server = "unix:///tmp/concurrent.sock"
    release.set()
    with pytest.raises(ValueError, match="changed"):
        await task
    assert not owner.config_path.exists()
    assert owner.config.rex_server == "unix:///tmp/concurrent.sock"


def test_mailbox_retry_rejects_non_rex_receipt(owner):
    peer = owner.rex.mailbox.enroll("reviewer", binding(), "enroll")
    owner.store.reserve(peer["peer_id"], "work", "cross-provider", {"kind": "hc"})
    with pytest.raises(ValueError, match="different work"):
        owner.rex.mailbox.submit(peer["peer_id"], "work", "cross-provider")


async def test_wait_cancellation_releases_receiver_capacity(owner):
    peer = await enrolled(owner)
    waiting = asyncio.create_task(owner.rex.receive(peer.peer_id, 15))
    await asyncio.sleep(0)
    assert peer.peer_id in owner.rex.waiters
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert owner.rex.waiters == {}
    assert owner.rex.mailbox.inbox(peer.peer_id)["tasks"] == []
    await owner.rex.close()


def test_queued_or_submitted_unfinished_work_prevents_rebind_unregister(owner):
    peer = owner.rex.mailbox.enroll("reviewer", binding(), "enroll")
    receipt = owner.rex.mailbox.submit(peer["peer_id"], "work", "active")
    for acknowledged in (False, True):
        if acknowledged:
            owner.rex.mailbox.acknowledge(peer["peer_id"], receipt["id"])
        with pytest.raises(ValueError, match="unfinished"):
            owner.providers.unregister("rex")
    assert owner.config.provider_enabled("rex")


async def test_complete_absence_invalidates_only_unfinished_receipts(owner):
    peer = await enrolled(owner)
    history = await owner.rex.submit(peer.peer_id, "history", "history")
    await owner.rex.ack(peer.peer_id, history["id"])
    await owner.rex.complete(peer.peer_id, history["id"], "immutable")
    active = await owner.rex.submit(peer.peer_id, "active", "active")
    b = peer.as_record()["binding"]

    async def inventory():
        return {
            "peers": [],
            "complete": True,
            "errors": [],
            "absence_scope": {
                field: b[field] for field in ("endpoint", "server_host_id", "server_instance_id")
            },
        }

    owner.rex.adapter.inventory = inventory
    await owner.adapter.discover()
    assert owner.rex.mailbox.get(peer.peer_id)["state"] == "stale"
    assert owner.store.get(active["id"])["status"] == "uncertain"
    assert owner.store.get(history["id"])["result"] == "immutable"
    assert (await owner.rex.complete(peer.peer_id, history["id"], "immutable"))[
        "status"
    ] == "completed"


@pytest.mark.parametrize("scope", [None, {}, {"server_instance_id": "other"}])
async def test_unknown_absence_scope_preserves_enrollment(owner, scope):
    peer = await enrolled(owner)
    active = await owner.rex.submit(peer.peer_id, "active", "active")

    async def inventory():
        return {"peers": [], "complete": True, "errors": [], "absence_scope": scope}

    owner.rex.adapter.inventory = inventory
    await owner.adapter.discover()
    assert owner.rex.mailbox.get(peer.peer_id)["state"] == "active"
    assert owner.store.get(active["id"])["status"] == "queued"


async def test_old_inventory_cannot_invalidate_new_enrollment(owner):
    old = await enrolled(owner)
    started, release = asyncio.Event(), asyncio.Event()
    b = old.as_record()["binding"]

    async def inventory():
        started.set()
        await release.wait()
        return {
            "peers": [],
            "complete": True,
            "errors": [],
            "absence_scope": {
                field: b[field] for field in ("endpoint", "server_host_id", "server_instance_id")
            },
        }

    owner.rex.adapter.inventory = inventory
    discovery = asyncio.create_task(owner.adapter.discover())
    await started.wait()
    await owner.rex.retire(old.peer_id)
    replacement = await owner.rex.enroll("replacement", b["session_id"], b["block_id"], "replace")
    receipt = await owner.rex.submit(replacement["peer_id"], "new", "new")
    release.set()
    page = await discovery
    row = page.peers[0].as_record()
    assert row["peer_id"] == replacement["peer_id"] and row["verified"] is False
    assert owner.rex.mailbox.get(replacement["peer_id"])["state"] == "active"
    assert owner.store.get(receipt["id"])["status"] == "queued"


async def test_rex_uses_generic_bus_gateway(owner):
    import time
    from uuid import uuid4

    from answerback_bridge.bus.gateway import BridgeGateway
    from answerback_bridge.bus.model import Command
    from answerback_bridge.discovery import Directory

    peer = await enrolled(owner)
    registry = AdapterRegistry([owner.adapter])
    owner.directory = lambda: Directory(owner.config, None, None, registry=registry)
    now = time.time()
    command = Command(
        1,
        str(uuid4()),
        "bus-authorized",
        peer.peer_id,
        "/project",
        routing_digest(owner.config),
        "Bus assignment 🦊",
        now,
        now + 60,
    )
    gateway = BridgeGateway(owner)
    prepared = await gateway.prepare(command)
    receipt = await gateway.send(prepared, command)
    assert receipt.target == peer.peer_id and receipt.request_id == command.adapter_request_id
    assert gateway.lookup(command).task_id == receipt.task_id
    await owner.rex.ack(peer.peer_id, receipt.task_id)
    await owner.rex.complete(peer.peer_id, receipt.task_id, "Bus result 🦊")
    assert gateway.get(receipt.task_id).as_record()["result"] == "Bus result 🦊"
    assert owner.adapter.recovery_states == ()


async def test_replaced_native_inventory_cannot_claim_complete_discovery(owner):
    peer = await enrolled(owner)
    started, release = asyncio.Event(), asyncio.Event()

    async def inventory():
        started.set()
        await release.wait()
        return {"peers": [], "complete": True, "errors": []}

    owner.rex.adapter.inventory = inventory
    discovery = asyncio.create_task(owner.adapter.discover())
    await started.wait()
    owner.rex.adapter = Native()
    release.set()
    page = await discovery
    assert page.as_record()["complete"] is False
    assert page.errors
    assert page.peers[0].peer_id == peer.peer_id
    assert page.peers[0].as_record()["verified"] is False
