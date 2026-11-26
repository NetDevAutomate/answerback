import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from codex_herdr_bridge.cli import dispatch, parser
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


async def test_mcp_lifecycle_registers_heartbeats_and_leaves_after_bridge_restart() -> None:
    from codex_herdr_bridge.client_registry import client_presence
    from codex_herdr_bridge.process import BridgeError

    methods: list[str] = []
    recovered = asyncio.Event()

    class RestartingBridge:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            methods.append(method)
            if method == "client_heartbeat" and methods.count(method) == 1:
                raise BridgeError("Lease lost on service restart")
            if method == "client_register" and methods.count(method) == 2:
                recovered.set()
            return {"heartbeat_interval": 1}

    async with client_presence(RestartingBridge(), "kiro"):
        async with asyncio.timeout(5):
            await recovered.wait()
    assert methods[:3] == ["client_register", "client_heartbeat", "client_register"]
    assert methods[-1] == "client_disconnect"


async def test_slow_poll_does_not_block_heartbeat() -> None:
    from codex_herdr_bridge.client_registry import client_presence

    beat = asyncio.Event()
    polling = asyncio.Event()

    class SlowMailbox:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "client_poll":
                polling.set()
                await asyncio.Event().wait()
            if method == "client_heartbeat":
                assert polling.is_set()
                beat.set()
            return {"heartbeat_interval": 1}

    async with client_presence(SlowMailbox(), "kiro", inbox={}):
        async with asyncio.timeout(3):
            await beat.wait()


async def test_polling_inbox_is_durable_until_explicit_acknowledgement(tmp_path: Path) -> None:
    from mcp import Client

    from codex_herdr_bridge.mcp_server import create_server

    path = tmp_path / "config.json"
    config = Config(provider_selection=[], client_registrations=["kiro"])
    config.save(path)
    bridge = Bridge(config, path)
    notification = bridge.client_notify("kiro", "Ready", "ready-one", "bridge.status")
    async with Client(create_server(bridge, client_name="kiro")) as client:
        read = await client.call_tool("read_inbox", {})
        assert read.structured_content["messages"][0]["id"] == notification["id"]
        assert bridge.mailbox.pending("kiro")["messages"]
        await client.call_tool("acknowledge_inbox", {"ids": [notification["id"]]})
        assert not bridge.mailbox.pending("kiro")["messages"]
    await bridge.close()


async def test_mcp_server_hooks_register_and_leave_without_a_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp import Client

    from codex_herdr_bridge.mcp_server import create_server

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bridge = Bridge(Config.load(path), path)
    registered = asyncio.Event()
    original = bridge.client_register

    def register(client: str, instance_id: str) -> dict[str, Any]:
        result = original(client, instance_id)
        registered.set()
        return result

    monkeypatch.setattr(bridge, "client_register", register)
    async with Client(create_server(bridge, client_name="kiro")):
        async with asyncio.timeout(1):
            await registered.wait()
        assert bridge.clients.snapshot()["clients"][0]["available"]
    assert not bridge.clients.snapshot()["clients"][0]["available"]
    await bridge.close()


async def test_new_configuration_does_not_probe_or_register_any_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unselected provider was probed")

    monkeypatch.setattr("codex_herdr_bridge.cli.executable_path", unexpected)
    target = tmp_path / "config.json"
    await dispatch(parser().parse_args(["--config", str(target), "configure"]))
    assert Config.load(target).registered_providers() == []


def test_legacy_configuration_preserves_existing_provider_selection(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        '{"herdr_session":"legacy","codex_enabled":false,"hc_enabled":true,'
        '"crew_enabled":true,"kiro_workspaces":{"demo":"/work"}}'
    )
    assert Config.load(target).registered_providers() == ["hc", "herdr", "kiro", "crew"]


def test_client_leases_expire_per_instance_and_recover_without_duplicate_registration() -> None:
    from codex_herdr_bridge.client_registry import ClientRegistry

    now = [0.0]
    config = Config(client_registrations=["codex"], heartbeat_interval=10, heartbeat_misses=3)
    registry = ClientRegistry(config, clock=lambda: now[0])
    first, second = str(uuid4()), str(uuid4())
    registry.register("codex", first)
    registry.register("codex", second)
    now[0] = 20
    registry.heartbeat("codex", second)
    now[0] = 30
    state = registry.snapshot()["clients"][0]
    assert state["available"] is True
    assert state["live_instances"] == 1
    now[0] = 50
    assert registry.snapshot()["clients"][0]["available"] is False
    registry.heartbeat("codex", first)
    assert registry.snapshot()["clients"][0]["available"] is True
    assert config.client_registrations == ["codex"]
    registry.disconnect("codex", first)
    assert registry.snapshot()["clients"][0]["available"] is False


def test_service_restart_does_not_restore_stale_live_leases() -> None:
    from codex_herdr_bridge.client_registry import ClientRegistry

    config = Config(client_registrations=["kiro"])
    registry = ClientRegistry(config)
    registry.register("kiro", str(uuid4()))
    assert registry.snapshot()["clients"][0]["available"] is True
    restarted = ClientRegistry(config)
    assert restarted.snapshot()["clients"][0]["available"] is False


def test_registration_capacity_is_per_client() -> None:
    """red_at_base: one client's full lease allowance leaves room for a new client."""
    from codex_herdr_bridge.client_registry import ClientRegistry
    from codex_herdr_bridge.process import BridgeError

    config = Config()
    registry = ClientRegistry(config, clock=lambda: 0.0)
    for _ in range(64):
        registry.register("claude", str(uuid4()))
    other = str(uuid4())
    assert registry.register("codex", other)["registered"] is True
    registry.require_live("codex", other)
    assert "codex" in config.client_registrations
    overflow = str(uuid4())
    with pytest.raises(BridgeError, match="MCP client connection limit reached for this client"):
        registry.register("claude", overflow)
    assert ("claude", overflow) not in registry.leases


def test_registration_at_capacity_can_renew_normalized_instance() -> None:
    """pins_existing: registering the same UUID renews its existing lease."""
    from codex_herdr_bridge.client_registry import ClientRegistry

    now = [0.0]
    registry = ClientRegistry(Config(), clock=lambda: now[0])
    first = uuid4()
    registry.register("claude", str(first))
    for _ in range(63):
        registry.register("claude", str(uuid4()))
    now[0] = 20.0
    assert registry.register("claude", first.hex.upper())["registered"] is True
    assert len(registry.leases) == 64
    now[0] = 30.0
    registry.require_live("claude", str(first))
    registry.disconnect("claude", str(first))
    assert registry.register("claude", str(uuid4()))["registered"] is True


def test_registration_reclaims_expired_leases_at_client_capacity() -> None:
    """pins_existing: reaching a client ceiling must not disable expiry recovery."""
    from codex_herdr_bridge.client_registry import ClientRegistry

    now = [0.0]
    registry = ClientRegistry(
        Config(heartbeat_interval=10, heartbeat_misses=3), clock=lambda: now[0]
    )
    first = str(uuid4())
    registry.register("claude", first)
    for _ in range(63):
        registry.register("claude", str(uuid4()))
    now[0] = 20.0
    registry.heartbeat("claude", first)
    now[0] = 30.0
    replacement = str(uuid4())
    assert registry.register("claude", replacement)["registered"] is True
    registry.require_live("claude", first)
    registry.require_live("claude", replacement)
    assert registry.snapshot()["clients"][0]["live_instances"] == 2


def test_registration_global_capacity_remains_a_backstop() -> None:
    """pins_existing: legacy full state still limits new keys and prunes stale leases."""
    from codex_herdr_bridge.client_registry import ClientRegistry
    from codex_herdr_bridge.config import CLIENT_NAMES
    from codex_herdr_bridge.process import BridgeError

    now = [0.0]
    config = Config(heartbeat_interval=10, heartbeat_misses=3)
    registry = ClientRegistry(config, clock=lambda: now[0])
    others = CLIENT_NAMES[1:]
    registry.leases = {(others[index % len(others)], str(uuid4())): 0.0 for index in range(512)}
    first_client, first_instance = next(iter(registry.leases))
    assert registry.register(first_client, first_instance)["registered"] is True
    other = str(uuid4())
    with pytest.raises(BridgeError, match="^MCP client connection limit reached$"):
        registry.register("codex", other)
    assert "codex" not in config.client_registrations
    now[0] = 30.0
    assert registry.register("codex", other)["registered"] is True
    assert registry.leases == {("codex", other): 30.0}


async def test_empty_registry_never_queries_unselected_providers(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    result = await bridge.peer_search()
    assert result == {"peers": [], "complete": True, "errors": {}}
    with pytest.raises(ValueError, match="not registered"):
        await bridge.call("kiro_sessions", {})
    await bridge.close()


async def test_heartbeats_are_served_when_provider_slots_are_full() -> None:
    from codex_herdr_bridge.runtime import Service
    from codex_herdr_bridge.transport import RemoteBridge

    with tempfile.TemporaryDirectory(prefix="hb-lease-", dir="/tmp") as directory:
        path = Path(directory) / "config.json"
        config = Config(provider_selection=[])
        config.save(path)
        service = Service(path)
        service.path.parent.mkdir()
        service.bridge = Bridge(config, path)
        service.slots = asyncio.Semaphore(0)
        server = await asyncio.start_unix_server(service.handle, path=str(service.path))
        try:
            bridge = RemoteBridge(path)
            params = {"client": "codex", "instance_id": str(uuid4())}
            async with asyncio.timeout(1):
                await bridge.call("client_register", params)
                assert (await bridge.call("client_heartbeat", params))["available"]
        finally:
            server.close()
            await server.wait_closed()
            await service.bridge.close()


async def test_registration_cannot_retarget_an_active_provider(tmp_path: Path) -> None:
    config = Config(
        provider_selection=["kiro"],
        kiro_binary=sys.executable,
        kiro_workspaces={"demo": str(tmp_path)},
    )
    path = tmp_path / "config.json"
    config.save(path)
    bridge = Bridge(config, path)
    bridge.store.reserve("kiro:demo", "work", "one", {"kind": "kiro"})
    with pytest.raises(ValueError, match="unfinished"):
        await bridge.call("provider_register", {"provider": "kiro", "binary": "/bin/sh"})
    assert config.kiro_binary == sys.executable
    await bridge.close()


async def test_enrolled_offline_client_can_queue_notice_without_claiming_liveness(
    tmp_path: Path,
) -> None:
    config = Config(provider_selection=[])
    bridge = Bridge(config, tmp_path / "config.json")
    bridge.client_enroll("kiro")
    assert not bridge.clients.snapshot()["clients"][0]["available"]
    assert bridge.client_notify("kiro", "Ready", "notice")["status"] == "queued"
    assert config.registered_providers() == []
    await bridge.close()


async def test_unregister_retains_settings_and_refuses_unfinished_work(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = Config(
        provider_selection=["kiro"], kiro_workspaces={"demo": str(tmp_path)}, codex_enabled=False
    )
    config.save(path)
    bridge = Bridge(config, path)
    task, _ = bridge.store.reserve(
        "kiro:demo", "fixture", "fixture-one", {"kind": "kiro", "alias": "demo"}
    )
    with pytest.raises(ValueError, match="unfinished"):
        await bridge.call("provider_unregister", {"provider": "kiro"})
    bridge.store.update(task["id"], "completed", result="done")
    await bridge.call("provider_unregister", {"provider": "kiro"})
    reloaded = Config.load(path)
    assert reloaded.registered_providers() == []
    assert reloaded.kiro_workspaces == {"demo": str(tmp_path)}
    assert bridge.store.get(task["id"])["result"] == "done"
    await bridge.close()


async def test_client_registration_is_durable_but_heartbeats_do_not_write_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bridge = Bridge(Config.load(path), path)
    instance = str(uuid4())
    await bridge.call("client_register", {"client": "codex", "instance_id": instance})
    before = path.stat().st_mtime_ns
    await bridge.call("client_heartbeat", {"client": "codex", "instance_id": instance})
    assert path.stat().st_mtime_ns == before
    assert Config.load(path).client_registrations == ["codex"]
    assert Config.load(path).registered_providers() == []
    await bridge.close()
