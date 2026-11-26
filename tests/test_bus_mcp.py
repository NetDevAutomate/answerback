from pathlib import Path

from bus_helpers import unused_server_binary
from mcp import Client

from codex_herdr_bridge.bus.config import BusConfig
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.mcp_server import create_server
from codex_herdr_bridge.service import Bridge


async def test_mcp_bus_receipts_are_delivery_receipts_not_completed_tasks(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = Config(provider_selection=["kiro"], kiro_workspaces={"worker": str(tmp_path)})
    config.save(path)
    BusConfig.create(path, server_binary=unused_server_binary(tmp_path))
    bridge = Bridge(config, path)
    try:
        async with Client(create_server(bridge)) as client:
            sent = await client.call_tool(
                "bus_submit",
                {
                    "peer_id": "kiro:worker",
                    "project": str(tmp_path),
                    "message": "Review",
                    "request_id": "mcp-one",
                },
            )
            command = sent.structured_content
            assert command["delivery"] == "queued"
            result = await client.call_tool("bus_result", {"command_id": command["command_id"]})
            assert result.structured_content["task"] is None
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["bus_submit"].annotations.read_only_hint is False
            assert tools["bus_result"].annotations.read_only_hint is True
    finally:
        await bridge.close()
