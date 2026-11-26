"""Public diagnostics and passive notification receipts must retain their meaning."""

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from answerback_bridge.cli import dispatch, parser
from answerback_bridge.config import Config
from answerback_bridge.mcp_server import create_server
from answerback_bridge.process import BridgeError
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.transport import RemoteBridge


@pytest.mark.parametrize("over_socket", [False, True])
@pytest.mark.parametrize(
    "error_type", [RuntimeError, KeyError, OSError, ValueError, TypeError, BridgeError]
)
async def test_unexpected_submission_error_does_not_expose_internal_details(
    over_socket: bool, error_type: type[Exception]
) -> None:
    class BrokenBridge:
        METHODS = {"peer_submit"}

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise error_type("PRIVATE_INTERNAL_DETAIL")

    with tempfile.TemporaryDirectory(prefix="ab-mask-", dir="/tmp") as folder:
        config_path = await asyncio.to_thread(Path(folder).resolve)
        config_path /= "config.json"
        broken = BrokenBridge()
        service = Service(config_path)
        service.bridge = broken  # type: ignore[assignment]
        server = None
        try:
            if over_socket:
                service.path.parent.mkdir(mode=0o700)
                server = await asyncio.start_unix_server(service.handle, path=service.path)
            endpoint = RemoteBridge(config_path) if over_socket else broken
            async with Client(create_server(endpoint)) as client:
                result = await client.call_tool(
                    "peer_submit",
                    {"selector": "kiro:test", "message": "hello", "request_id": "one"},
                )
            assert result.is_error
            assert "PRIVATE_INTERNAL_DETAIL" not in str(result.content)
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()


async def test_sender_can_observe_notification_ack_without_poll_or_execution_claim(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config(provider_selection=[], client_registrations=["kiro"])
    config.save(config_path)
    bridge = Bridge(config, config_path)
    try:
        sent = bridge.client_notify("kiro", "Private notice", "notice-42")
        async with Client(create_server(bridge)) as sender:
            tools = {tool.name: tool for tool in (await sender.list_tools()).tools}
            assert tools["notification_status"].annotations.read_only_hint is True
            params = {"client": "kiro", "request_id": "notice-42"}
            initial = (await sender.call_tool("notification_status", params)).structured_content
            assert initial["id"] == sent["id"]
            assert initial["status"] == "queued"
            assert initial["client_acknowledged_at"] is None
            assert initial["delivery_mode"] == "passive_inbox"
            assert initial["wakes_agent"] is False
            assert initial["task_id"] is None
            assert "Private notice" not in str(initial)

            async with Client(create_server(bridge, client_name="kiro")) as receiver:
                await receiver.call_tool("read_inbox", {})
                polled = (await sender.call_tool("notification_status", params)).structured_content
                assert polled == initial
                await receiver.call_tool("acknowledge_inbox", {"ids": [sent["id"]]})
                acknowledged = (
                    await sender.call_tool("notification_status", params)
                ).structured_content
                assert acknowledged["status"] == "acknowledged"
                assert acknowledged["client_acknowledged_at"] is not None
                assert acknowledged["acknowledgement_scope"] == "client_inbox"
                assert acknowledged["task_id"] is None
                # Duplicate acknowledgement must not rewrite the evidence timestamp.
                await receiver.call_tool("acknowledge_inbox", {"ids": [sent["id"]]})
                assert (
                    await sender.call_tool("notification_status", params)
                ).structured_content == acknowledged
        assert bridge.store.recent() == []
        unknown = bridge.mailbox.status("codex", "notice-42")
        assert unknown["found"] is False
        assert unknown["status"] == "not_found"
    finally:
        await bridge.close()


async def test_notification_status_cli_is_read_only_and_routes_exact_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def capture(self: RemoteBridge, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        return {"found": False}

    monkeypatch.setattr(RemoteBridge, "call", capture)
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "notification-status",
            "kiro",
            "--request-id",
            "notice-42",
        ]
    )
    assert await dispatch(args) == {"found": False}
    assert calls == [("client_notification_status", {"client": "kiro", "request_id": "notice-42"})]
