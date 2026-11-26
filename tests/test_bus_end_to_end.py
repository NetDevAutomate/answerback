"""Real NATS and coordinator IPC with scripted Codex and Kiro protocol endpoints."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from bus_helpers import BrokerFixture
from composition import contributed
from test_bus_cli import run as bus_cli

pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")
from codex_herdr_bridge.bus.api import BusAPI
from codex_herdr_bridge.bus.jetstream import JetStreamBus
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.lifecycle import start, stop
from codex_herdr_bridge.transport import RemoteBridge

CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
# The installed plugin's scripted peer runs the same case (tests/composition.py).
PLUGIN_PEERS = contributed("BUS_PEERS", {})


@pytest.fixture
async def environment():
    with tempfile.TemporaryDirectory(prefix="agent-bus-", dir="/tmp") as directory:
        root = Path(directory)
        codex = root / "codex-fixture"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import json,pathlib,sys,time\n"
            "root=pathlib.Path(__file__).parent\n"
            "args=sys.argv[1:]\n"
            "if 'queue' in args:\n"
            "    (root/'codex-message').write_text(args[args.index('--message')+1])\n"
            "    with (root/'codex-sends').open('a') as stream: stream.write('send\\n')\n"
            "    print('Queued message fixture for thread '+args[args.index('--thread')+1])\n"
            "elif 'app-server' in args:\n"
            "    for line in sys.stdin:\n"
            "        request=json.loads(line)\n"
            "        if 'id' not in request: continue\n"
            "        result={}\n"
            "        if request['method']=='thread/read':\n"
            f"            result={{'thread':{{'id':'{CHAT}','name':'Fixture','cwd':str(root),"
            "'source':'vscode','ephemeral':False}}\n"
            "        if request['method']=='thread/turns/list':\n"
            "            message=root/'codex-message'\n"
            "            result={'data':[],'nextCursor':None}\n"
            "            if message.exists():\n"
            "                result['data']=[{'id':'fixture-turn','status':'completed',"
            "'startedAt':4000000000,'completedAt':4000000001,'items':["
            "{'type':'userMessage','content':[{'type':'text','text':message.read_text()}]},"
            "{'type':'agentMessage','phase':'final_answer','text':'PONG scripted Codex'}]}]\n"
            "        print(json.dumps({'id':request['id'],'result':result}),flush=True)\n"
            "else: raise SystemExit(2)\n"
        )
        codex.chmod(0o700)
        kiro = root / "kiro-fixture"
        fixture = Path(__file__).parent / "fixtures/acp_agent.py"
        kiro.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        kiro.chmod(0o700)
        plugin_settings: dict[str, Any] = {}
        for peer in PLUGIN_PEERS.values():
            plugin_settings.update(peer.install(root))
        (root / "agent-settings.json").write_text(
            json.dumps(
                {
                    "mcp_open_plans": [
                        {
                            "after": [
                                {
                                    "servers": [
                                        {
                                            "name": "answerback_bridge",
                                            "status": "connected",
                                            "tools": [
                                                {"name": "peer_submit", "disabled": False},
                                                {"name": "task_wait", "disabled": False},
                                            ],
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                }
            )
        )
        config = root / "config.json"
        Config(
            codex_enabled=True,
            codex_binary=str(codex),
            kiro_binary=str(kiro),
            kiro_workspaces={"worker": str(root)},
            **plugin_settings,
            provider_selection=["codex", "kiro", *PLUGIN_PEERS],
        ).save(config)
        broker = BrokerFixture(root)
        try:
            yield root, config, broker
        finally:
            await stop(config)
            await broker.stop()


async def complete(remote: RemoteBridge, command_id: str) -> dict:
    async with asyncio.timeout(20):
        while True:
            result = await remote.call("bus_result", {"command_id": command_id})
            if result["task"] and result["task"]["status"] == "completed":
                return result
            assert result["delivery"] not in {"uncertain", "rejected", "expired"}, result
            await asyncio.sleep(0.1)


@pytest.mark.parametrize("provider", ["codex", "kiro", *PLUGIN_PEERS])
async def test_offline_coordinator_broker_restart_and_forced_duplicate(environment, provider: str):
    root, config, broker = environment
    await broker.start()
    peers = {"codex": f"desktop:{CHAT}", "kiro": "kiro:worker"}
    peers.update({name: peer.peer_id for name, peer in PLUGIN_PEERS.items()})
    peer = peers[provider]
    queued = await asyncio.to_thread(
        bus_cli,
        config,
        "submit",
        peer,
        "--project",
        str(root),
        "--message",
        "Reply to the fixture",
        "--request-id",
        "offline-one",
    )
    ident = queued["command_id"]
    assert queued["delivery"] == "queued"
    async with JetStreamBus(broker.config, consume=False) as transport:
        assert (await transport.js.stream_info(broker.config.commands_stream)).state.messages == 1
    await broker.stop()
    await broker.start()
    await start(config, standalone=True)
    remote = RemoteBridge(config)
    final = await complete(remote, ident)
    results = {"codex": "PONG scripted Codex", "kiro": "Hello world"}
    results.update({name: peer.result for name, peer in PLUGIN_PEERS.items()})
    assert final["task"]["result"] == results[provider]

    # Force redelivery independently of JetStream's own deduplication window.
    raw = BusAPI(config).store().command(ident).to_bytes()
    async with JetStreamBus(broker.config, consume=False) as transport:
        await transport.js.publish(
            broker.config.commands_subject, raw, headers={"Nats-Msg-Id": str(uuid4())}
        )
        async with asyncio.timeout(3):
            while True:
                if not (
                    await transport.js.stream_info(broker.config.commands_stream)
                ).state.messages:
                    break
                await asyncio.sleep(0.05)
    assert (await remote.call("bus_result", {"command_id": ident}))["task_id"] == final["task_id"]
    if provider == "codex":
        assert (root / "codex-sends").read_text().splitlines() == ["send"]
    else:
        log = "agent-requests.jsonl"
        if provider in PLUGIN_PEERS:
            log = PLUGIN_PEERS[provider].request_log
        calls = [json.loads(line) for line in (root / log).read_text().splitlines()]
        assert sum(call.get("method") == "session/prompt" for call in calls) == 1
    async with asyncio.timeout(3):
        while True:
            if any(
                event["type"] == "task.completed"
                for event in (await remote.call("bus_events", {}))["events"]
            ):
                break
            await asyncio.sleep(0.05)


async def test_broker_unavailable_does_not_break_rpc_and_reconnect_drains_outbox(environment):
    root, config, broker = environment
    await start(config, standalone=True)
    remote = RemoteBridge(config)
    queued = await remote.call(
        "bus_submit",
        {
            "peer_id": "kiro:worker",
            "project": str(root),
            "message": "Reply",
            "request_id": "broker-offline",
        },
    )
    assert (await remote.call("task_list", {}))["tasks"] == []
    assert (await remote.call("bus_result", {"command_id": queued["command_id"]}))["task"] is None
    await broker.start()
    assert (await complete(remote, queued["command_id"]))["task"]["result"] == "Hello world"


@pytest.mark.parametrize("payload", [b'{"not":"a command"}', b"[" * 2000 + b"]" * 2000])
async def test_poison_message_is_terminated_without_creating_a_task(environment, payload):
    root, config, broker = environment
    await broker.start()
    await start(config, standalone=True)
    remote = RemoteBridge(config)
    async with JetStreamBus(broker.config, consume=False) as transport:
        await transport.js.publish(broker.config.commands_subject, payload)
        async with asyncio.timeout(3):
            while True:
                if not (
                    await transport.js.stream_info(broker.config.commands_stream)
                ).state.messages:
                    break
                await asyncio.sleep(0.05)
    assert (await remote.call("task_list", {}))["tasks"] == []
    assert (await remote.call("bus_status", {}))["coordinator"]["rejected_messages"] == 1
    row = await remote.call(
        "bus_submit",
        {
            "peer_id": "kiro:worker",
            "project": str(root),
            "message": "Reply after poison",
            "request_id": "after-poison",
        },
    )
    assert (await complete(remote, row["command_id"]))["task"]["status"] == "completed"
