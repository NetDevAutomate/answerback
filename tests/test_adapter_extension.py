from pathlib import Path

import pytest
from bus_helpers import unused_server_binary

from codex_herdr_bridge.adapters.base import StoredAdapter
from codex_herdr_bridge.adapters.contracts import (
    DiscoveryPage,
    Guard,
    Peer,
    PreparedPeer,
    Submission,
    TaskReceipt,
)
from codex_herdr_bridge.adapters.registry import AdapterRegistry
from codex_herdr_bridge.bus.config import routing_digest
from codex_herdr_bridge.bus.coordinator import Coordinator
from codex_herdr_bridge.bus.gateway import BridgeGateway
from codex_herdr_bridge.bus.store import BusStore
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


class FixtureAdapter(StoredAdapter):
    name = registration = "fixture"
    recovery_states = ("running",)
    sends = 0
    bad_receipt = ""

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

    async def submit(
        self,
        prepared: PreparedPeer,
        submission: Submission,
        *,
        guard: Guard,
    ) -> TaskReceipt:
        guard()
        row, created = self.store.reserve(
            prepared.target_key,
            submission.message,
            submission.request_id,
            {"kind": self.name},
        )
        if created:
            self.sends += 1
            row = self.store.update(row["id"], "completed", result="FIXTURE_RESULT")
        if self.bad_receipt == "missing":
            row = {**row, "id": "nonexistent"}
        elif self.bad_receipt == "request":
            row = {**row, "request_key": "another-request"}
        elif self.bad_receipt == "target":
            row = {**row, "target": "fixture:another"}
        return TaskReceipt.from_record(row)

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return receipt.provider == self.name and receipt.target == peer.peer_id


def environment(path: Path) -> tuple[Bridge, FixtureAdapter]:
    bridge = Bridge(Config(provider_selection=[]), path)
    adapter = FixtureAdapter(bridge.config, path, bridge.store)
    bridge.adapters = AdapterRegistry([adapter])
    return bridge, adapter


async def test_registered_extension_works_through_discovery_dispatch_bus_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    bridge, adapter = environment(path)
    bus = BusStore(tmp_path / "bus.sqlite")
    try:
        found = await bridge.peer_search()
        assert found["peers"][0]["peer_id"] == "fixture:one"
        direct = await bridge.peer_submit("fixture:one", "Review", "direct")
        assert direct["result"] == "FIXTURE_RESULT"
        command = bus.submit(
            peer_id="fixture:one",
            project=str(tmp_path),
            request_id="bus",
            message="Review through bus",
            routing_digest=routing_digest(bridge.config),
        )
        raw = bus.command(command["command_id"]).to_bytes()
        await Coordinator(bus, BridgeGateway(bridge)).process(raw)
        assert bus.get(command["command_id"])["delivery"] == "bound"
        assert adapter.sends == 2
        with pytest.raises(ValueError, match="support"):
            bridge.task_permissions(direct["id"])
    finally:
        await bridge.close()
    reopened, fresh = environment(path)
    try:
        await Coordinator(bus, BridgeGateway(reopened)).process(raw)
        assert fresh.sends == 0
        task = await reopened.get_task(bus.get(command["command_id"])["task_id"])
        assert task["result"] == "FIXTURE_RESULT"
        assert (await reopened.task_for_peer("fixture:one"))["id"] == task["id"]
    finally:
        await reopened.close()


@pytest.mark.parametrize("kind", ["missing", "request", "target"])
async def test_uncorrelated_adapter_receipts_are_refused(tmp_path: Path, kind: str) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    adapter.bad_receipt = kind
    try:
        with pytest.raises(ValueError):
            await bridge.peer_submit("fixture:one", "Review", "correlation")
        assert adapter.sends == 1
    finally:
        await bridge.close()


def test_recovery_policy_supports_registered_extensions_without_store_switches(
    tmp_path: Path,
) -> None:
    bridge, _ = environment(tmp_path / "config.json")
    row, _ = bridge.store.reserve("fixture:one", "Review", "recovery", {"kind": "fixture"})
    bridge.store.update(row["id"], "running")
    assert bridge.recover_pending() == 1
    assert bridge.store.get(row["id"])["status"] == "uncertain"


async def test_duplicate_fast_path_validates_persisted_request_identity(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    row, _ = bridge.store.reserve("fixture:one", "Different work", "other", {"kind": "fixture"})
    adapter.previous = lambda *_: TaskReceipt.from_record(row)
    try:
        with pytest.raises(ValueError, match="uncorrelated"):
            await bridge.peer_submit("fixture:one", "Review", "wanted")
        assert adapter.sends == 0
    finally:
        await bridge.close()


async def test_observation_cannot_change_persisted_provider_identity(tmp_path: Path) -> None:
    bridge, adapter = environment(tmp_path / "config.json")
    row, _ = bridge.store.reserve("fixture:one", "Review", "observe", {"kind": "fixture"})

    async def changed(receipt, *, refresh):
        bridge.store.metadata(receipt.task_id, {"kind": "kiro"})
        return TaskReceipt.from_record(bridge.store.get(receipt.task_id))

    adapter.observe = changed
    try:
        with pytest.raises(ValueError, match="identity"):
            await bridge.get_task(row["id"])
    finally:
        await bridge.close()


async def test_catalog_registration_admits_bus_api_and_worker_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_herdr_bridge import config as configuration
    from codex_herdr_bridge.adapters import catalog
    from codex_herdr_bridge.bus.api import BusAPI
    from codex_herdr_bridge.bus.config import BusConfig

    entry = catalog.ProviderDefinition("fixture", "fixture", worker_reports=True)
    monkeypatch.setattr(catalog, "DEFINITIONS", (*catalog.DEFINITIONS, entry))
    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, "fixture"))
    path = tmp_path / "config.json"
    config = Config(provider_selection=["fixture"])
    config.save(path)
    BusConfig.create(path, server_binary=unused_server_binary(tmp_path))
    bridge, adapter = environment(path)
    bridge.config.provider_selection = ["fixture"]
    api = BusAPI(path)
    try:
        command = api.submit("fixture:one", str(tmp_path), "Review", "catalog-admission")
        await Coordinator(api.store(), BridgeGateway(bridge)).process(
            api.store().command(command["command_id"]).to_bytes()
        )
        assert api.result(command["command_id"])["task"]["result"] == "FIXTURE_RESULT"
        task, _ = bridge.store.reserve(
            "fixture:one", "Manual worker report", "worker-report", {"kind": "fixture"}
        )
        assert bridge.complete(task["id"], "WORKER_RESULT")["status"] == "completed"
        assert adapter.sends == 1
    finally:
        await bridge.close()
