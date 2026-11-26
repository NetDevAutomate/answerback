from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from codex_herdr_bridge.bus.config import routing_digest
from codex_herdr_bridge.bus.gateway import BridgeGateway
from codex_herdr_bridge.bus.model import Command
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge

CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def command(config: Config, peer: str, project: Path) -> Command:
    return Command(1, CHAT, "test", peer, str(project), routing_digest(config), "Review", 1, 3601)


async def test_fuzzy_recipient_is_refused_before_dispatch(tmp_path: Path) -> None:
    config = Config(provider_selection=["herdr"], herdr_session="review")
    bridge = Bridge(config, tmp_path / "config.json")
    bridge.herdr.call = AsyncMock(
        return_value={
            "agents": [
                {
                    "name": "worker",
                    "foreground_cwd": str(tmp_path),
                    "terminal_id": "terminal-1",
                    "agent_session": "session-1",
                    "agent": "kiro",
                }
            ]
        }
    )
    try:
        # A bound fuzzy match reaches the gateway's exact-peer guard.
        assert (await bridge.directory().resolve("herdr:wor"))["peer_id"] == "herdr:worker"
        with pytest.raises(ValueError, match="^Bus delivery requires an exact discovered peer ID$"):
            await BridgeGateway(bridge).prepare(command(config, "herdr:wor", tmp_path))
        assert bridge.store.recent() == []
    finally:
        await bridge.close()


async def test_paired_alias_loads_project_from_its_bound_chat(tmp_path: Path) -> None:
    config = Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT})
    bridge = Bridge(config, tmp_path / "config.json")
    bridge.desktop_index.read = AsyncMock(
        return_value={
            "peer_id": f"desktop:{CHAT}",
            "provider": "desktop",
            "target": CHAT,
            "thread_id": CHAT,
            "project": str(tmp_path),
        }
    )
    try:
        target = await BridgeGateway(bridge).prepare(command(config, "desktop:reviewer", tmp_path))
        assert target.key == f"desktop:{CHAT}"
        assert target.peer.peer_id == "desktop:reviewer"
        assert target.peer.target == "reviewer"
        bridge.desktop_index.read.assert_awaited_once_with(CHAT)
    finally:
        await bridge.close()
