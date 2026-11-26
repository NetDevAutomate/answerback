from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest


def contracts():
    from codex_herdr_bridge.adapters import contracts

    return contracts


def test_peer_snapshot_is_immutable_and_native_copies_cannot_change_identity() -> None:
    c = contracts()
    native = {
        "peer_id": "kiro:worker",
        "provider": "kiro",
        "target": "worker",
        "project": "/work",
        "binding": {"generation": 1},
    }
    peer = c.Peer.from_record(native)
    native["binding"]["generation"] = 2
    assert peer.as_record()["binding"]["generation"] == 1
    copy = peer.as_record()
    copy["binding"]["generation"] = 3
    assert peer.as_record()["binding"]["generation"] == 1
    assert peer.provider == "kiro" and peer.project == "/work"
    with pytest.raises(FrozenInstanceError):
        peer.peer_id = "kiro:other"


@pytest.mark.parametrize(
    "change",
    [
        {"peer_id": ""},
        {"provider": 1},
        {"target": None},
        {"project": []},
    ],
)
def test_peer_rejects_malformed_common_fields(change: dict) -> None:
    c = contracts()
    with pytest.raises(ValueError):
        c.Peer.from_record(
            {
                "peer_id": "kiro:worker",
                "provider": "kiro",
                "target": "worker",
                "project": "/work",
                **change,
            }
        )


def test_receipt_validates_state_and_preserves_opaque_metadata() -> None:
    c = contracts()
    record = {
        "id": "task-one",
        "request_key": "request-one",
        "target": "kiro:worker:/work",
        "status": "uncertain",
        "metadata": {"kind": "kiro", "native": {"sequence": 1}},
        "message": "private",
        "result": None,
    }
    receipt = c.TaskReceipt.from_record(record)
    assert receipt.task_id == "task-one" and receipt.provider == "kiro"
    assert receipt.status == "uncertain"
    assert receipt.as_record() == record
    with pytest.raises(ValueError):
        c.TaskReceipt.from_record({**record, "status": "looks_done"})


def test_registry_selects_declared_aliases_and_rejects_duplicate_or_absent_providers() -> None:
    from codex_herdr_bridge.adapters.registry import AdapterRegistry

    c = contracts()
    adapter = SimpleNamespace(name="desktop", aliases=("codex",), controls=None)
    registry = AdapterRegistry([adapter])
    peer = c.Peer.from_record(
        {
            "peer_id": "desktop:one",
            "provider": "desktop",
            "target": "one",
        }
    )
    assert registry.for_peer(peer) is adapter
    assert registry.get("codex") is adapter
    with pytest.raises(ValueError, match="Unknown"):
        registry.get("rex")
    with pytest.raises(ValueError, match="Duplicate"):
        AdapterRegistry([adapter, adapter])
    with pytest.raises(ValueError, match="support"):
        registry.controls("desktop")


def test_discovery_page_freezes_collections_and_rejects_invalid_receipt_state() -> None:
    c = contracts()
    peers = [
        c.Peer.from_record(
            {
                "peer_id": "kiro:worker",
                "provider": "kiro",
                "target": "worker",
            }
        )
    ]
    errors = [["kiro", "partial"]]
    page = c.DiscoveryPage(peers=peers, errors=errors)
    peers.clear()
    errors[0][1] = "changed"
    assert len(page.peers) == 1
    assert page.errors == (("kiro", "partial"),)
    with pytest.raises(ValueError):
        c.TaskReceipt.from_record(
            {
                "id": "one",
                "request_key": "request",
                "target": "kiro:worker",
                "status": [],
            }
        )
