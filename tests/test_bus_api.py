from pathlib import Path

import pytest
from bus_helpers import unused_server_binary

from codex_herdr_bridge.bus.api import BusAPI
from codex_herdr_bridge.bus.config import BusConfig
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


def configured(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    Config(provider_selection=["kiro"], kiro_workspaces={"worker": str(tmp_path)}).save(path)
    BusConfig.create(path, server_binary=unused_server_binary(tmp_path))
    return path


def test_offline_api_accepts_without_broker_and_never_claims_task_success(tmp_path: Path) -> None:
    path = configured(tmp_path)
    api = BusAPI(path)
    command = api.submit(
        peer_id="kiro:worker",
        project=str(tmp_path),
        message="Review",
        request_id="one",
    )
    assert command["delivery"] == "queued"
    result = api.result(command["command_id"])
    assert result["task"] is None
    assert result["delivery"] == "queued"
    assert api.status()["outbox_pending"] == 2
    assert api.cancel(command["command_id"])["delivery"] == "cancelled"


def test_unconfigured_api_has_no_side_effects(tmp_path: Path) -> None:
    api = BusAPI(tmp_path / "config.json")
    assert api.status() == {"configured": False}
    assert not (tmp_path / "bus").exists()


async def test_additive_bridge_bus_methods_preserve_direct_routes(tmp_path: Path) -> None:
    path = configured(tmp_path)
    bridge = Bridge(Config.load(path), path)
    try:
        command = await bridge.call(
            "bus_submit",
            {
                "peer_id": "kiro:worker",
                "project": str(tmp_path),
                "message": "Review",
                "request_id": "rpc-one",
            },
        )
        assert (await bridge.call("bus_result", {"command_id": command["command_id"]}))[
            "task"
        ] is None
        assert (await bridge.call("bus_status", {}))["configured"]
        assert (await bridge.call("bus_events", {}))["events"]
        assert (await bridge.call("bus_cancel", {"command_id": command["command_id"]}))[
            "delivery"
        ] == "cancelled"
        assert await bridge.call("task_list", {}) == {"tasks": [], "searched_latest": 100}
    finally:
        await bridge.close()


def test_unregistered_provider_cannot_be_admitted(tmp_path: Path) -> None:
    api = BusAPI(configured(tmp_path))
    with pytest.raises(ValueError, match="registered"):
        api.submit(
            peer_id="desktop:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            project=str(tmp_path),
            message="Review",
            request_id="one",
        )
