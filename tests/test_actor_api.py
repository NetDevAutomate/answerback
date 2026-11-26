"""Actor entry points preserve identity and never expose administrative authority."""

import asyncio
import json
import secrets
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from bus_helpers import unused_server_binary
from mcp import Client

from answerback_bridge.actors.api import ActorAPI, load_binding
from answerback_bridge.actors.frontend import create_actor_server
from answerback_bridge.actors.model import ActorAuth, ActorError
from answerback_bridge.actors.store import ActorStore
from answerback_bridge.bus.config import BusConfig
from answerback_bridge.config import Config
from answerback_bridge.service import Bridge


def setup_run(tmp_path: Path) -> tuple[Path, ActorStore, ActorAuth, ActorAuth]:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    BusConfig.create(path, server_binary=unused_server_binary(tmp_path))
    store = ActorStore(tmp_path / "bus/actors.sqlite")
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["coordinator", "worker"])
    actors = []
    for role in ("coordinator", "worker"):
        auth = ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"session_id": str(uuid4())})
        actors.append(auth)
    store.seal_run(run, auth=actors[0])
    return path, store, actors[0], actors[1]


def test_actor_read_does_not_enable_or_create_an_unconfigured_ledger(tmp_path: Path) -> None:
    api = ActorAPI(tmp_path / "config.json")
    assert api.available_store() is None
    with pytest.raises(ActorError):
        api.send({}, "invalid", "one", "review_request", {})
    assert not (tmp_path / "bus").exists()


def test_binding_is_private_strict_and_secret_safe(tmp_path: Path) -> None:
    _, _, auth, _ = setup_run(tmp_path)
    binding = asdict(auth)
    binding.pop("frontend_id")
    binding["schema_version"] = 1
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    path.chmod(0o600)
    loaded = load_binding(path)
    assert loaded.actor_id == auth.actor_id
    assert loaded.credential == auth.credential
    assert loaded.frontend_id != auth.frontend_id
    assert auth.credential not in repr(loaded)
    path.chmod(0o644)
    with pytest.raises(ActorError, match="binding"):
        load_binding(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ActorError):
        load_binding(link)
    path.write_text(json.dumps(binding)[:-1] + ',"epoch":1}')
    with pytest.raises(ActorError):
        load_binding(path)


async def test_actor_mcp_exposes_only_bound_operations_and_ack_requires_read(
    tmp_path: Path,
) -> None:
    path, store, sender, recipient = setup_run(tmp_path)
    bridge = Bridge(Config.load(path), path)
    message = store.send(sender, recipient.actor_id, "assign", "assignment", {"work": "Review"})
    item = store.pending_outbox()[0]
    store.deliver(item["payload"], "test-stream", 1)
    try:
        async with Client(create_actor_server(bridge, recipient)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {"actor_send", "actor_receive", "actor_ack", "actor_reply"}
            for tool in tools.values():
                assert "auth" not in tool.input_schema["properties"]
                assert "sender_actor_id" not in tool.input_schema["properties"]
                assert "credential" not in tool.input_schema["properties"]
            assert tools["actor_receive"].annotations.read_only_hint is False
            refused = await client.call_tool(
                "actor_ack", {"message_id": message["message_id"], "receipt_nonce": "unseen"}
            )
            assert refused.is_error
            read = (await client.call_tool("actor_receive", {})).structured_content
            offered = read["messages"][0]
            assert offered["body"] == {"work": "Review"}
            ack = await client.call_tool(
                "actor_ack",
                {"message_id": offered["message_id"], "receipt_nonce": offered["receipt_nonce"]},
            )
            assert not ack.is_error
            reply = await client.call_tool(
                "actor_reply",
                {"reply_to": offered["message_id"], "request_id": "answer", "body": {"ok": True}},
            )
            assert not reply.is_error
            assert auth_secrets(sender, recipient).isdisjoint(
                set(json.dumps(store.export(sender.run_id, auth=sender)).split('"'))
            )
    finally:
        await bridge.close()


def auth_secrets(*values: ActorAuth) -> set[str]:
    return {value.credential for value in values}


async def test_actor_send_is_the_only_destructive_actor_tool(tmp_path: Path) -> None:
    # actor_send puts text in front of another actor; receiving, acknowledging and
    # replying to an already acknowledged message do not.
    path, _, _, recipient = setup_run(tmp_path)
    bridge = Bridge(Config.load(path), path)
    try:
        async with Client(create_actor_server(bridge, recipient)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    finally:
        await bridge.close()
    assert tools["actor_send"].annotations.destructive_hint is True
    assert tools["actor_send"].annotations.read_only_hint is False
    for name in ("actor_receive", "actor_ack", "actor_reply"):
        assert tools[name].annotations.destructive_hint is False, name


async def test_receive_long_poll_does_not_block_delivery_and_observes_closure(
    tmp_path: Path,
) -> None:
    path, store, sender, recipient = setup_run(tmp_path)
    api = ActorAPI(path)
    pending = asyncio.create_task(api.receive(asdict(recipient), wait_seconds=2))
    await asyncio.sleep(0)
    assert not pending.done()
    store.finish_run(sender.run_id, "Test closure", auth=sender)
    with pytest.raises(ActorError) as failure:
        await asyncio.wait_for(pending, 1)
    assert failure.value.code == "run_finished"


def test_api_never_exposes_untyped_exception_text(tmp_path: Path, monkeypatch) -> None:
    path, _, sender, recipient = setup_run(tmp_path)
    private_detail = "sensitive-provider-detail"

    def fail(*args, **kwargs):
        raise ValueError(private_detail)

    monkeypatch.setattr(ActorStore, "send", fail)
    with pytest.raises(ActorError) as failure:
        ActorAPI(path).send(asdict(sender), recipient.actor_id, "one", "review_request", {})
    assert private_detail not in str(failure.value)


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
def test_actor_cli_dispatch_refuses_bare_run_id(tmp_path: Path, operation: str) -> None:
    """red_at_base: the live CLI dispatch boundary also requires proof."""
    from argparse import Namespace

    from answerback_bridge.actors.cli import dispatch

    path, store, sender, recipient = setup_run(tmp_path)
    store.send(sender, recipient.actor_id, "private", "assignment", {"work": "fixture-only"})
    with pytest.raises(ActorError, match="credential"):
        dispatch(Namespace(config=path, actor_operation=operation, run_id=sender.run_id))


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
@pytest.mark.parametrize("proof_kind", ["owner", "participant"])
def test_actor_cli_inspection_requires_and_passes_proof(
    tmp_path: Path, operation: str, proof_kind: str
) -> None:
    """red_at_base: parse and dispatch preserve owner and private binding proof."""
    from answerback_bridge.actors.cli import dispatch
    from answerback_bridge.cli import parser

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    BusConfig.create(path, server_binary=unused_server_binary(tmp_path))
    cli = parser()
    prefix = ["--config", str(path), "actors"]
    created = dispatch(
        cli.parse_args([*prefix, "create", "--project", str(tmp_path), "--role", "reader"])
    )
    run, token = created["run_id"], created["owner_token"]
    binding = tmp_path / "reader.json"
    actor = dispatch(
        cli.parse_args(
            [
                *prefix,
                "stage",
                run,
                "--role",
                "reader",
                "--harness",
                "test",
                "--binding-file",
                str(binding),
            ]
        )
    )
    proof = ["--owner-token", token] if proof_kind == "owner" else ["--binding-file", str(binding)]
    with pytest.raises(SystemExit):
        cli.parse_args([*prefix, operation, run])
    result = dispatch(cli.parse_args([*prefix, operation, run, *proof]))
    assert (result["run"] if operation == "export" else result)["run_id"] == run
    assert token not in json.dumps(result)
    store = ActorStore(tmp_path / "bus/actors.sqlite")
    with store.connection() as db:
        assert store._actor(db, run, actor["actor_id"], 1)["frontend_id"] is None
    if proof_kind == "participant":
        binding.chmod(0o644)
        with pytest.raises(ActorError, match="binding"):
            dispatch(cli.parse_args([*prefix, operation, run, *proof]))


async def test_run6_actor_store_calls_leave_the_event_loop_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base (run-6 hardening h4): a store call blocked the daemon's event loop."""
    import time
    from types import SimpleNamespace

    from answerback_bridge.bus.plugin import BusModule

    path, _store, sender, recipient = setup_run(tmp_path)
    module = BusModule(SimpleNamespace(config_path=path))  # type: ignore[arg-type]
    for name in ("send", "receive", "acknowledge"):
        real = getattr(ActorStore, name)

        def slow(
            self: ActorStore,
            *args: object,
            _real: Callable[..., object] = real,
            **kwargs: object,
        ) -> object:
            time.sleep(0.25)  # a write lock held by another process, within busy_timeout
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(ActorStore, name, slow)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    async def ticks_during(call: object) -> int:
        before = ticks
        try:
            await call  # type: ignore[misc]
        except ActorError:
            pass  # acknowledging an unknown message is refused; the wait is what counts
        return ticks - before

    running = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.02)
        sent = await ticks_during(
            module.actor_send(asdict(sender), recipient.actor_id, "one", "review_request", {})
        )
        read = await ticks_during(module.actor_receive(asdict(recipient)))
        acked = await ticks_during(module.actor_ack(asdict(recipient), str(uuid4()), "0" * 64))
    finally:
        running.cancel()
    assert min(sent, read, acked) >= 5, (sent, read, acked)


async def test_run6_one_actor_cannot_hold_every_receive_wait(tmp_path: Path) -> None:
    """red_at_base (run-6 hardening h4): only the module-wide 16 bounded waiting receives."""
    from types import SimpleNamespace

    from answerback_bridge.bus.plugin import BusModule

    path, store, sender, recipient = setup_run(tmp_path)
    module = BusModule(SimpleNamespace(config_path=path))  # type: ignore[arg-type]
    per_actor = getattr(module, "actor_receive_per_actor", 4)
    assert per_actor == 4  # min(4, 16 // 4)
    waits = [
        asyncio.create_task(module.actor_receive(asdict(recipient), wait_seconds=5))
        for _ in range(per_actor)
    ]
    try:
        await asyncio.sleep(0.3)
        assert not any(task.done() for task in waits)
        with pytest.raises(ActorError) as refused:
            await asyncio.wait_for(module.actor_receive(asdict(recipient), wait_seconds=5), 2)
        assert refused.value.code == "capacity"
        # Another actor still waits and reads; a wait that ends frees its slot.
        other = await module.actor_receive(asdict(sender), wait_seconds=1)
        assert other["messages"] == []
        immediate = await module.actor_receive(asdict(recipient), wait_seconds=0)
        assert immediate["messages"] == []
    finally:
        store.finish_run(sender.run_id, "Test closure", auth=sender)
        for task in waits:
            with pytest.raises(ActorError):
                await asyncio.wait_for(task, 2)
    assert getattr(module, "receive_waits", {}) == {}


def test_run6_one_sender_cannot_use_the_whole_run_message_budget(tmp_path: Path) -> None:
    """red_at_base (run-6 hardening h4): the 256-message run cap was shared by all senders."""
    from answerback_bridge.actors import store as actor_store

    _path, store, coordinator, worker = setup_run(tmp_path)
    quota = getattr(actor_store, "MAX_MESSAGES_PER_SENDER", actor_store.MAX_MESSAGES // 2)
    for index in range(quota):
        store.send(coordinator, worker.actor_id, f"a-{index}", "assignment", {})
    with pytest.raises(ActorError) as refused:
        store.send(coordinator, worker.actor_id, "a-over", "assignment", {})
    assert refused.value.code == "capacity"
    # An unchanged retry of an accepted message still returns its original receipt.
    store.send(coordinator, worker.actor_id, "a-0", "assignment", {})
    store.send(worker, coordinator.actor_id, "r-0", "review_request", {})
