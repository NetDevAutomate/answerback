import asyncio
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import Client

from answerback_bridge.config import Config
from answerback_bridge.mcp_server import create_server
from answerback_bridge.runtime import Service
from answerback_bridge.transport import RemoteBridge


@pytest.fixture
async def runtime(monkeypatch):
    # Short runtime paths also work on macOS's bounded Unix socket address.
    with tempfile.TemporaryDirectory(prefix="rex-") as directory:
        path = Path(directory) / "config.json"
        Config(
            codex_enabled=False,
            rex_enabled=True,
            rex_server="unix:///tmp/rex.sock",
            rex_host_id="local",
        ).save(path)
        service = Service(path)
        serving = asyncio.create_task(service.serve())
        try:
            async with asyncio.timeout(3):
                for _ in range(300):
                    if service.path.exists():
                        break
                    await asyncio.sleep(0.01)
                assert service.path.exists()
            binding = dict(
                endpoint="unix:///tmp/rex.sock",
                server_instance_id="one",
                server_host_id="local",
                block_host_id="local",
                session_id="session:" + str(uuid4()),
                block_id="block:" + str(uuid4()),
                foreground_pid=42,
                foreground_name="agent",
                foreground_path=None,
                foreground_script=None,
                foreground_cwd="/tmp",
            )

            async def terminal(self, session_id, block_id):
                assert (session_id, block_id) == (binding["session_id"], binding["block_id"])
                return {"binding": binding.copy(), "foreground": {"user_id": os.getuid()}}

            async def read(self, session_id, block_id, max_bytes=16384):
                assert (session_id, block_id) == (binding["session_id"], binding["block_id"])
                return {"text": "observed snapshot", "max_bytes": max_bytes}

            monkeypatch.setattr("answerback_bridge.rex.Rex.terminal", terminal)
            monkeypatch.setattr("answerback_bridge.rex.Rex.read", read)
            yield service, RemoteBridge(path), binding
        finally:
            service.stopping.set()
            await asyncio.wait_for(serving, 5)


async def test_real_mcp_cooperative_roundtrip(runtime):
    service, remote, binding = runtime
    async with Client(create_server(remote)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("rex_read", "rex_receive"):
            assert tools[name].annotations.read_only_hint is True
        for name in ("rex_enroll", "rex_ack", "rex_complete", "rex_retire"):
            assert tools[name].annotations.read_only_hint is False
        assert "rex_configure" not in tools
        assert set(tools["rex_enroll"].input_schema["required"]) == {
            "name",
            "session_id",
            "block_id",
            "request_id",
        }

        async def call(tool_name, **params):
            response = await client.call_tool(tool_name, params)
            assert not response.is_error, response
            return response.structured_content

        capture = await call(
            "rex_read",
            session_id=binding["session_id"],
            block_id=binding["block_id"],
            max_bytes=1024,
        )
        assert capture == {"text": "observed snapshot", "max_bytes": 1024}
        peer = await call(
            "rex_enroll",
            name="agent",
            session_id=binding["session_id"],
            block_id=binding["block_id"],
            request_id="enrollment",
        )
        assert (
            await call(
                "rex_enroll",
                name="agent",
                session_id=binding["session_id"],
                block_id=binding["block_id"],
                request_id="enrollment",
            )
        )["peer_id"] == peer["peer_id"]
        body = "Full original message " + "λ" * 5000
        task = await call(
            "peer_submit", selector=peer["peer_id"], message=body, request_id="work", provider="rex"
        )
        assert (
            await call(
                "peer_submit",
                selector=peer["peer_id"],
                message=body,
                request_id="work",
                provider="rex",
            )
        )["id"] == task["id"]
        assert (
            await client.call_tool(
                "rex_complete",
                {"peer_id": peer["peer_id"], "task_id": task["id"], "result": "premature"},
            )
        ).is_error
        assert (
            await client.call_tool("task_complete", {"task_id": task["id"], "result": "bypass"})
        ).is_error
        inbox = await call("rex_receive", peer_id=peer["peer_id"])
        assert inbox["tasks"][0]["message"] == body
        receipt = await call("task_get", task_id=task["id"])
        assert "message" not in receipt
        listing = await call("task_list")
        assert body not in str(listing)
        assert (await call("rex_ack", peer_id=peer["peer_id"], task_id=task["id"]))[
            "status"
        ] == "submitted"
        completed = await call(
            "rex_complete", peer_id=peer["peer_id"], task_id=task["id"], result="exact result"
        )
        assert completed["status"] == "completed"
        assert (await call("task_get", task_id=task["id"]))["result"] == "exact result"
        assert (
            await call(
                "rex_complete", peer_id=peer["peer_id"], task_id=task["id"], result="exact result"
            )
        )["id"] == task["id"]
        await call("rex_retire", peer_id=peer["peer_id"])
        assert body not in (service.path.parent / "bridge.log").read_text()


async def test_enrolled_rex_client_submits_to_core_fixture(runtime, monkeypatch):
    service, remote, binding = runtime
    service.bridge.config.hc_enabled = True
    peer = dict(
        peer_id="hc:fixture",
        provider="hc",
        target="fixture",
        name="Core fixture",
        project="/tmp",
        project_known=True,
        agent_kind="claude",
        kind="core_agent",
        state="idle",
        send_ready=True,
        binding="one",
        session=None,
    )
    sent = []

    async def inventory():
        return {"peers": [peer], "complete": True, "errors": {}}

    async def observe(target):
        assert target == "fixture"
        return peer.copy()

    async def send(target, message, expected):
        assert expected["binding"] == "one"
        sent.append(message)
        return {"turn": 10, "started": True, "status": "idle"}

    monkeypatch.setattr(service.bridge.core, "inventory", inventory)
    monkeypatch.setattr(service.bridge.core, "peer", observe)
    monkeypatch.setattr(service.bridge.core, "send", send)
    async with Client(create_server(remote)) as client:
        enrolled = await client.call_tool(
            "rex_enroll",
            dict(
                name="rex sender",
                session_id=binding["session_id"],
                block_id=binding["block_id"],
                request_id="sender",
            ),
        )
        assert not enrolled.is_error
        found = await client.call_tool("peer_search", {"provider": "hc"})
        response = await client.call_tool(
            "peer_submit",
            dict(
                selector="hc:fixture",
                message="Outgoing work",
                request_id="outgoing",
                provider="hc",
                binding=found.structured_content["peers"][0]["binding"],
            ),
        )
        assert not response.is_error
        task = response.structured_content
        async with asyncio.timeout(3):
            await asyncio.gather(*service.bridge.core_tasks.jobs.values())
        assert service.bridge.store.get(task["id"])["status"] == "submitted"
        assert len(sent) == 1 and task["id"] in sent[0] and "Outgoing work" in sent[0]
        completed = await client.call_tool(
            "task_complete", dict(task_id=task["id"], result="CORE_RESULT")
        )
        assert completed.structured_content["id"] == task["id"]
        read = await client.call_tool("task_get", dict(task_id=task["id"]))
        assert read.structured_content["result"] == "CORE_RESULT"
