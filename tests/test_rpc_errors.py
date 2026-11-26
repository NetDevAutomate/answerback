"""Error authority is explicit; legacy or malformed metadata never publishes raw text."""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from answerback_bridge.errors import CrewVersionUnsupported
from answerback_bridge.mcp_server import create_server
from answerback_bridge.process import BridgeError, CommandError
from answerback_bridge.runtime import Service
from answerback_bridge.transport import (
    RPC_VERSION,
    RemoteBridge,
    TransportUnavailable,
    encode,
    timeout_message,
)


@asynccontextmanager
async def error_endpoint(
    *, error: Exception | None = None, reply: dict[str, Any] | None = None
) -> AsyncIterator[RemoteBridge]:
    with tempfile.TemporaryDirectory(prefix="ab-error-", dir="/tmp") as folder:
        config_path = await asyncio.to_thread(Path(folder).resolve)
        config_path /= "config.json"
        remote = RemoteBridge(config_path)
        remote.path.parent.mkdir(mode=0o700)
        if error is not None:

            class FailingBridge:
                METHODS = {"peer_submit", "peer_search", "task_reconcile"}

                async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                    raise error

            service = Service(config_path)
            service.bridge = FailingBridge()  # type: ignore[assignment]
            handler = service.handle
        else:
            assert reply is not None

            async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await reader.readline()
                writer.write(encode(reply))
                await writer.drain()
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_unix_server(handler, path=remote.path):
            yield remote


@pytest.mark.parametrize(
    "error",
    [
        BridgeError("PRIVATE_INTERNAL_DETAIL"),
        CommandError(2, "PRIVATE_INTERNAL_DETAIL"),
        KeyError("PRIVATE_INTERNAL_DETAIL"),
        OSError("PRIVATE_INTERNAL_DETAIL"),
        RuntimeError("PRIVATE_INTERNAL_DETAIL"),
    ],
)
async def test_service_error_frame_never_serializes_unapproved_exception_text(
    error: Exception,
) -> None:
    async with error_endpoint(error=error) as remote:
        reader, writer = await asyncio.open_unix_connection(remote.path)
        writer.write(
            encode(
                {
                    "version": RPC_VERSION,
                    "config_path": str(remote.config_path),
                    "method": "peer_submit",
                    "params": {},
                }
            )
        )
        await writer.drain()
        raw = await reader.readline()
        writer.close()
        await writer.wait_closed()
    reply = json.loads(raw)
    assert reply["version"] == RPC_VERSION
    assert reply["ok"] is False
    assert isinstance(reply["error"], str)
    assert b"PRIVATE_INTERNAL_DETAIL" not in raw
    assert reply["error_info"] == {"kind": "internal"}


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Crew request timed out; check its gateway",
        "Core read timed out; retry the read",
        "Bridge is stopping",
        "Core command timed out; delivery may be uncertain. Do not resubmit blindly.",
    ],
)
async def test_approved_bridge_diagnostic_survives_actual_socket(diagnostic: str) -> None:
    async with error_endpoint(error=BridgeError(diagnostic)) as remote:
        async with Client(create_server(remote)) as client:
            result = await client.call_tool("peer_search", {})
    assert result.is_error
    assert diagnostic in str(result.content)


async def test_a_refused_crew_version_crosses_the_socket_as_its_fixed_diagnostic() -> None:
    # The refusal's own text names what an executable printed. Across the socket only
    # the reviewed literal of its code may appear, never that text.
    error = CrewVersionUnsupported(
        "Unsupported Crew version PRIVATE_INTERNAL_DETAIL; "
        "this adapter requires kirocrew 0.12.2 or later"
    )
    async with error_endpoint(error=error) as remote:
        reader, writer = await asyncio.open_unix_connection(remote.path)
        writer.write(
            encode(
                {
                    "version": RPC_VERSION,
                    "config_path": str(remote.config_path),
                    "method": "peer_search",
                    "params": {},
                }
            )
        )
        await writer.drain()
        raw = await reader.readline()
        writer.close()
        await writer.wait_closed()
        async with Client(create_server(remote)) as client:
            result = await client.call_tool("peer_search", {})
    reply = json.loads(raw)
    assert reply["ok"] is False
    assert reply["error_info"] == {"kind": "public", "code": "crew_version_unsupported"}
    assert reply["error"] == (
        "Crew version is unsupported or unreadable; run answerback doctor to see what it read"
    )
    assert b"PRIVATE_INTERNAL_DETAIL" not in raw
    assert result.is_error
    assert "Crew version is unsupported or unreadable" in str(result.content)
    assert "PRIVATE_INTERNAL_DETAIL" not in str(result.content)


async def test_approved_recovery_diagnostic_survives_actual_socket() -> None:
    diagnostic = "Receipt changed during reconciliation; inspect it again"
    async with error_endpoint(error=ValueError(diagnostic)) as remote:
        async with Client(create_server(remote)) as client:
            result = await client.call_tool(
                "task_reconcile", {"task_id": "11111111-2222-3333-4444-555555555555"}
            )
    assert result.is_error
    assert diagnostic in str(result.content)


async def test_unexpected_direct_recovery_error_remains_masked() -> None:
    class BrokenBridge:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("PRIVATE_INTERNAL_DETAIL")

    async with Client(create_server(BrokenBridge())) as client:
        result = await client.call_tool(
            "task_reconcile", {"task_id": "11111111-2222-3333-4444-555555555555"}
        )
    assert result.is_error
    assert "PRIVATE_INTERNAL_DETAIL" not in str(result.content)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        "public",
        {"kind": "unknown"},
        {"kind": "public"},
        {"kind": "public", "code": "PRIVATE_INTERNAL_DETAIL"},
        {"kind": "public", "code": ["crew_timeout"]},
        {"kind": "admission_refused"},
        {"kind": "admission_refused", "blocking_task_id": 42, "target": "fixtureprov:test"},
        {
            "kind": "admission_refused",
            "blocking_task_id": "PRIVATE_INTERNAL_DETAIL",
            "target": "fixtureprov:test",
        },
        {
            "kind": "admission_refused",
            "blocking_task_id": "11111111-2222-3333-4444-555555555555",
            "target": "PRIVATE_INTERNAL_DETAIL\n" * 100,
        },
        {"kind": "timeout", "method": "PRIVATE_INTERNAL_DETAIL"},
    ],
)
async def test_unclassified_or_malformed_peer_reply_is_masked(metadata: Any) -> None:
    reply = {"version": RPC_VERSION, "ok": False, "error": "PRIVATE_INTERNAL_DETAIL"}
    if metadata is not None:
        reply["error_info"] = metadata
    async with error_endpoint(reply=reply) as remote:
        async with Client(create_server(remote)) as client:
            result = await client.call_tool(
                "peer_submit",
                {"selector": "fixtureprov:test", "message": "hello", "request_id": "one"},
            )
    assert result.is_error
    assert "PRIVATE_INTERNAL_DETAIL" not in str(result.content)


async def test_public_code_renders_local_diagnostic_and_ignores_remote_raw_text() -> None:
    reply = {
        "version": RPC_VERSION,
        "ok": False,
        "error": "PRIVATE_INTERNAL_DETAIL",
        "error_info": {"kind": "public", "code": "draining"},
    }
    async with error_endpoint(reply=reply) as remote:
        async with Client(create_server(remote)) as client:
            result = await client.call_tool("peer_search", {})
    assert result.is_error
    assert "Bridge is draining; inspect outstanding task receipts" in str(result.content)
    assert "PRIVATE_INTERNAL_DETAIL" not in str(result.content)


async def test_older_peer_success_remains_compatible() -> None:
    reply = {"version": RPC_VERSION, "ok": True, "result": {"peers": []}}
    async with error_endpoint(reply=reply) as remote:
        assert await remote.call("peer_search", {}) == reply["result"]


@pytest.mark.parametrize("method", ["peer_search", "peer_submit", "crew_configure"])
async def test_socket_timeout_preserves_operation_semantics_without_raw_text(method: str) -> None:
    reply = {
        "version": RPC_VERSION,
        "ok": False,
        "error": "PRIVATE_TIMEOUT_DETAIL",
        "error_info": {"kind": "timeout"},
    }
    async with error_endpoint(reply=reply) as remote:
        with pytest.raises(TransportUnavailable) as raised:
            await remote.call(method, {})
    assert "PRIVATE_TIMEOUT_DETAIL" not in str(raised.value)
    assert str(raised.value) == timeout_message(method)


@pytest.mark.parametrize("method", ["peer_search", "peer_submit"])
async def test_service_timeout_has_safe_mcp_diagnostic(method: str) -> None:
    async with error_endpoint(error=TimeoutError("PRIVATE_TIMEOUT_DETAIL")) as remote:
        async with Client(create_server(remote)) as client:
            params = (
                {}
                if method == "peer_search"
                else {"selector": "fixtureprov:test", "message": "hello", "request_id": "one"}
            )
            result = await client.call_tool(method, params)
    assert result.is_error
    assert timeout_message(method) in str(result.content)
    assert "PRIVATE_TIMEOUT_DETAIL" not in str(result.content)
