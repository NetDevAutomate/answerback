"""No native side effects: ownership and private coordination controls."""

import importlib.util
import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from answerback_bridge.mcp_server import create_server
from answerback_bridge.process import BridgeError

spec = importlib.util.spec_from_file_location(
    "smoke_rex", Path(__file__).parents[1] / "scripts" / "smoke_rex.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_control_environment_scrubs_selectors(monkeypatch):
    monkeypatch.setenv("REX_SESSION", "existing")
    monkeypatch.setenv("REX_SERVER", "remote")
    monkeypatch.setenv("FIXTURE_TEST", "preserved")
    assert not any(key.startswith("REX_") for key in smoke.control_env())
    assert smoke.control_env()["FIXTURE_TEST"] == "preserved"


async def test_cleanup_only_exact_owned_id(monkeypatch):
    native = smoke.Native("rex", "unix:///tmp/test.sock")
    owned = "session:12345678-1234-1234-1234-123456789abc"
    native.session = owned
    calls = []

    async def command(*args):
        calls.append(args)
        return json.dumps(
            {"sessions": [{"session_id": "session:00000000-0000-0000-0000-000000000001"}]}
        )

    monkeypatch.setattr(native, "command", command)
    result = await native.close()
    assert calls == [("kill", owned), ("api", "call", "session.list", "{}")]
    assert result["absence_verified"]
    assert native.session is None
    assert await native.close() == {"session_created": False}


async def test_cleanup_uncertainty_retains_target(monkeypatch):
    native = smoke.Native("rex", "unix:///tmp/test.sock")
    native.session = "session:12345678-1234-1234-1234-123456789abc"

    async def command(*args):
        return json.dumps({"sessions": [{"session_id": native.session}]})

    monkeypatch.setattr(native, "command", command)
    with pytest.raises(AssertionError, match="still listed"):
        await native.close()
    assert native.session is not None


async def test_no_cleanup_for_unowned_session(monkeypatch):
    native = smoke.Native("rex", "unix:///tmp/test.sock")
    assert await native.close() == {"session_created": False}


async def test_coordination_timeout_and_error(tmp_path):
    with pytest.raises(TimeoutError):
        await smoke.wait_file(tmp_path / "absent", 0.01)
    path = tmp_path / "result"
    path.write_text(json.dumps({"error": "fixture refused"}))
    with pytest.raises(RuntimeError, match="fixture refused"):
        await smoke.wait_file(path)


async def test_cleanup_never_retries_uncertain_kill(monkeypatch):
    native = smoke.Native("rex", "unix:///tmp/test.sock")
    native.session = "session:12345678-1234-1234-1234-123456789abc"
    calls = []

    async def command(*args):
        calls.append(args)
        if args[0] == "kill":
            raise TimeoutError("uncertain kill")
        return json.dumps({"sessions": []})

    monkeypatch.setattr(native, "command", command)
    with pytest.raises(TimeoutError):
        await native.close()
    assert (await native.close())["absence_verified"]
    assert [args[0] for args in calls] == ["kill", "api"]


@pytest.mark.parametrize(
    "listing",
    [{}, {"sessions": [], "next_cursor": "unknown"}, {"sessions": [{"session_id": "short"}]}],
)
async def test_cleanup_incomplete_listing_retains_owned_target(monkeypatch, listing):
    native = smoke.Native("rex", "unix:///tmp/test.sock")
    native.session = "session:12345678-1234-1234-1234-123456789abc"

    async def command(*args):
        return json.dumps(listing)

    monkeypatch.setattr(native, "command", command)
    with pytest.raises(BridgeError):
        await native.close()
    assert native.session is not None


def test_unicode_read_requires_encoded_bound():
    value = dict(
        text="雪🦊",
        capture_kind="bounded_prefix",
        truncated=True,
        bytes_returned=7,
        total_bytes=5000,
    )
    smoke.validate_read(value)
    with pytest.raises(AssertionError):
        smoke.validate_read({**value, "bytes_returned": 2})


def test_absence_proof_rejects_unrelated_mcp_error():
    unrelated = CallToolResult(
        is_error=True, content=[TextContent(type="text", text="Incompatible bridge protocol")]
    )
    with pytest.raises(AssertionError, match="confirmed-absence"):
        smoke.validate_absence(unrelated)


async def test_absence_proof_preserves_exact_rpc_reason():
    class RefusedBridge:
        async def call(self, method, params):
            raise BridgeError("Rex session confirmed absent")

    bridge = smoke.AbsenceProofBridge(RefusedBridge())
    async with Client(create_server(bridge)) as client:
        response = await client.call_tool("rex_receive", {"peer_id": "owned-fixture-peer"})
    assert smoke.validate_absence(response) == {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "Error executing tool rex_receive: Rex session confirmed absent",
            }
        ],
    }


async def test_absence_proof_does_not_promote_unrelated_rpc_error():
    class UnrelatedFailureBridge:
        async def call(self, method, params):
            raise BridgeError("Incompatible bridge protocol")

    bridge = smoke.AbsenceProofBridge(UnrelatedFailureBridge())
    async with Client(create_server(bridge)) as client:
        response = await client.call_tool("rex_receive", {"peer_id": "owned-fixture-peer"})
    with pytest.raises(AssertionError, match="confirmed-absence"):
        smoke.validate_absence(response)
