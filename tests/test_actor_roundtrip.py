"""Real STDIO MCP, local RPC and NATS transport, with no model-success claim."""

import asyncio
import json
import secrets
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

from bus_helpers import BrokerFixture
from mcp import Client, StdioServerParameters

from answerback_bridge.actors.store import ActorStore
from answerback_bridge.config import Config
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.transport import RemoteBridge


async def test_two_stdio_frontends_exchange_correlated_messages_through_real_bus() -> None:
    with tempfile.TemporaryDirectory(prefix="ab-actor-", dir="/tmp") as folder:
        root = await asyncio.to_thread(Path(folder).resolve)
        broker = BrokerFixture(root)
        cfg = broker.config
        config = Config(provider_selection=[])
        config.save(cfg.bridge_path)
        service = Service(cfg.bridge_path)
        service.bridge = Bridge(config, cfg.bridge_path)
        remote = RemoteBridge(cfg.bridge_path)
        remote.path.parent.mkdir(mode=0o700, exist_ok=True)
        store = ActorStore(cfg.root / "actors.sqlite")
        run = str(uuid4())
        created = store.create_run(run, str(root), ["author", "reviewer"])
        actor_ids = []
        bindings = []
        for role in ("author", "reviewer"):
            actor_id = str(uuid4())
            credential = secrets.token_hex(32)
            store.stage_actor(run, actor_id, role, "test-stdio", credential)
            store.activate_actor(run, actor_id, 1, {"session_id": str(uuid4())})
            path = root / f"{role}.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": run,
                        "actor_id": actor_id,
                        "epoch": 1,
                        "credential": credential,
                    }
                )
            )
            path.chmod(0o600)
            bindings.append(path)
            actor_ids.append(actor_id)
        store.seal_run(run, owner_token=created["owner_token"])
        worker = None
        await broker.start()
        try:
            async with await asyncio.start_unix_server(service.handle, path=remote.path):
                worker = asyncio.create_task(service.run_bus())
                async with AsyncExitStack() as stack:
                    clients = []
                    for binding in bindings:
                        clients.append(
                            await stack.enter_async_context(
                                Client(
                                    StdioServerParameters(
                                        command=sys.executable,
                                        args=[
                                            "-m",
                                            "answerback_bridge",
                                            "--config",
                                            str(cfg.bridge_path),
                                            "mcp",
                                            "--actor-binding-file",
                                            str(binding),
                                        ],
                                    )
                                )
                            )
                        )
                    author, reviewer = clients
                    sent = await author.call_tool(
                        "actor_send",
                        {
                            "to_actor": actor_ids[1],
                            "request_id": "review-one",
                            "kind": "review_request",
                            "body": {"artifact_sha256": "f" * 64, "question": "Inspect this hash"},
                        },
                    )
                    assert not sent.is_error
                    first = sent.structured_content["message_id"]
                    received = await reviewer.call_tool("actor_receive", {"wait_seconds": 5})
                    assert not received.is_error
                    offered = received.structured_content["messages"][0]
                    assert offered["message_id"] == first
                    ack = await reviewer.call_tool(
                        "actor_ack",
                        {"message_id": first, "receipt_nonce": offered["receipt_nonce"]},
                    )
                    assert not ack.is_error
                    response = await reviewer.call_tool(
                        "actor_reply",
                        {
                            "reply_to": first,
                            "request_id": "review-answer",
                            "body": {"review": "The hash is a fixture"},
                        },
                    )
                    assert not response.is_error
                    returned = await author.call_tool("actor_receive", {"wait_seconds": 5})
                    assert not returned.is_error
                    reply = returned.structured_content["messages"][0]
                    assert reply["reply_to"] == first
                    assert reply["sender_actor_id"] == actor_ids[1]
                    assert reply["kind"] == "review_reply"
                    ack = await author.call_tool(
                        "actor_ack",
                        {
                            "message_id": reply["message_id"],
                            "receipt_nonce": reply["receipt_nonce"],
                        },
                    )
                    assert not ack.is_error
                    # Repeating the same request across the same frontend is one message.
                    repeated = await author.call_tool(
                        "actor_send",
                        {
                            "to_actor": actor_ids[1],
                            "request_id": "review-one",
                            "kind": "review_request",
                            "body": {"artifact_sha256": "f" * 64, "question": "Inspect this hash"},
                        },
                    )
                    assert repeated.structured_content["message_id"] == first
                    assert (
                        len(store.export(run, owner_token=created["owner_token"])["messages"]) == 2
                    )
                    assert service.bridge.bus_state["actors"]["counts"]["delivered"] == 2
                    assert service.bridge.store.counts() == {}
        finally:
            service.stopping.set()
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await service.bridge.close()
            await broker.stop()
