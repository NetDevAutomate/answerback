"""Actor broker delivery tests; fixture peers do not count as live participants."""

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bus_helpers import BrokerFixture

pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")
import nats.errors
from nats.js.api import ConsumerConfig
from nats.js.errors import APIError, NotFoundError

from answerback_bridge.actors.model import MAX_ENVELOPE_BYTES, ActorAuth, ActorEnvelope
from answerback_bridge.actors.transport import ActorLane
from answerback_bridge.bus.config import BusConfig
from answerback_bridge.bus.jetstream import JetStreamBus


def envelope(project: str) -> ActorEnvelope:
    now = time.time()
    return ActorEnvelope(
        schema_version=1,
        message_id=str(uuid4()),
        run_id=str(uuid4()),
        project=project,
        sender_actor_id=str(uuid4()),
        sender_epoch=1,
        recipient_actor_id=str(uuid4()),
        recipient_epoch=1,
        roster_digest="a" * 64,
        request_id="review-1",
        kind="review_request",
        body={"question": "Inspect the source contract"},
        reply_to=None,
        created_at=now,
        expires_at=now + 3600,
    )


class MemoryStore:
    def __init__(self, message):
        self.message = message
        self.raw = message.to_bytes()
        self.published = []
        self.deliveries = []
        self.trace = []
        self.delivery = "delivered"
        self.failure = None

    def pending_outbox(self, limit=100):
        return (
            [
                {
                    "message_id": self.message.message_id,
                    "run_id": self.message.run_id,
                    "payload": self.raw,
                }
            ]
            if not self.published
            else []
        )[:limit]

    def mark_published(self, message_id, stream, seq):
        self.published.append((message_id, stream, seq))
        self.trace.append("published")

    def deliver(self, raw, stream, seq):
        if self.failure:
            raise self.failure
        if raw != self.raw:
            raise ValueError("Unknown or altered actor envelope")
        duplicate = bool(self.deliveries)
        self.deliveries.append((stream, seq))
        self.trace.append("commit")
        return {
            "message_id": self.message.message_id,
            "delivery": self.delivery,
            "duplicate": duplicate,
        }


class FakeMessage:
    def __init__(self, lane, store, *, raw=None, seq=1):
        self.data = store.raw if raw is None else raw
        self.subject = lane.subject
        self.headers = {"Nats-Msg-Id": store.message.message_id}
        self.metadata = SimpleNamespace(
            stream=lane.stream,
            consumer=lane.consumer,
            sequence=SimpleNamespace(stream=seq),
        )
        self.trace = store.trace
        self.acked = False
        self.terminated = False
        self.nacked = False
        self.ack_failure = False

    async def ack_sync(self, **kwargs):
        self.trace.append("ack")
        if self.ack_failure:
            raise nats.errors.TimeoutError
        self.acked = True

    async def term(self):
        self.trace.append("term")
        self.terminated = True

    async def nak(self, delay=None):
        self.trace.append("nak")
        self.nacked = True


class FakeSubscription:
    def __init__(self):
        self.messages = []
        self.batch_sizes = []
        self.unsubscribed = False

    async def fetch(self, batch, **kwargs):
        self.batch_sizes.append(batch)
        if not self.messages:
            raise nats.errors.TimeoutError
        selected, self.messages = self.messages[:batch], self.messages[batch:]
        return selected

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeJetStream:
    def __init__(self):
        self._prefix = "$JS.API"
        self._nc = SimpleNamespace(
            connected_server_version=SimpleNamespace(major=2, minor=15, patch=3)
        )
        self.stream_config = None
        self.consumer_config = None
        self.subscription = FakeSubscription()
        self.creates = []
        self.publications = []
        self.messages = {}
        self.publish_failure = None
        self.lose_next_puback = False

    async def stream_info(self, name):
        if self.stream_config is None:
            raise NotFoundError
        return SimpleNamespace(config=self.stream_config)

    async def add_stream(self, config):
        self.creates.append(("stream", config))
        self.stream_config = config
        return SimpleNamespace(config=config)

    async def consumer_info(self, stream, consumer):
        if self.consumer_config is None:
            raise NotFoundError
        return SimpleNamespace(config=self.consumer_config)

    async def _api_request(self, subject, payload, **kwargs):
        request = json.loads(payload)
        assert request["action"] == "create"
        self.creates.append(("consumer", request))
        incoming = ConsumerConfig.from_response(request["config"])
        if self.consumer_config is not None and self.consumer_config != incoming:
            raise APIError(code=400, err_code=10148, description="already exists")
        self.consumer_config = incoming
        return {"success": True}

    async def pull_subscribe_bind(self, **kwargs):
        assert kwargs["pending_msgs_limit"] == 8
        assert kwargs["pending_bytes_limit"] <= 8 * MAX_ENVELOPE_BYTES
        return self.subscription

    async def publish(self, subject, payload, *, headers, **kwargs):
        if self.publish_failure:
            raise self.publish_failure
        ident = headers["Nats-Msg-Id"]
        duplicate = ident in self.messages
        seq = self.messages[ident][0] if duplicate else len(self.messages) + 1
        self.messages[ident] = (seq, payload)
        self.publications.append((subject, payload, headers))
        if self.lose_next_puback:
            self.lose_next_puback = False
            raise nats.errors.TimeoutError
        return SimpleNamespace(stream=self.stream_config.name, seq=seq, duplicate=duplicate)


@pytest.fixture
def lane_setup(tmp_path):
    config = BusConfig(tmp_path / "config.json", 4222, "/bin/true")
    store = MemoryStore(envelope(str(tmp_path)))
    js = FakeJetStream()
    lane = ActorLane(js, config, store)
    return js, store, lane


async def test_start_uses_separate_immutable_resources_and_bind_only(lane_setup):
    js, _, lane = lane_setup
    await lane.start()
    assert lane.stream == "AGENT_ACTOR_MESSAGES_" + lane.config.namespace.upper()
    assert lane.subject == f"agents.v1.{lane.config.namespace}.actor_messages"
    assert lane.consumer == "actor_mailbox_v1"
    assert [kind for kind, _ in js.creates] == ["stream", "consumer"]
    assert js.stream_config.max_msg_size == MAX_ENVELOPE_BYTES
    assert js.consumer_config.max_ack_pending == 8
    await lane.close()
    assert js.subscription.unsubscribed
    # Closing a lane neither deletes the durable nor closes the shared connection.
    assert js.consumer_config is not None


@pytest.mark.parametrize("version", [(2, 9, 99), (1, 99, 0), (None, 15, 0)])
async def test_old_or_unknown_server_rejected_before_resource_creation(lane_setup, version):
    js, _, lane = lane_setup
    js._nc.connected_server_version = SimpleNamespace(
        major=version[0], minor=version[1], patch=version[2]
    )
    with pytest.raises(ValueError, match="2.10"):
        await lane.start()
    assert js.creates == []


async def test_qualified_minimum_server_accepts_create_only_action(lane_setup):
    js, _, lane = lane_setup
    js._nc.connected_server_version = SimpleNamespace(major=2, minor=10, patch=0)
    await lane.start()
    assert js.creates[-1][1]["action"] == "create"
    await lane.close()


async def test_matching_existing_resources_are_bound_without_create_or_update(lane_setup):
    js, store, first = lane_setup
    await first.start()
    await first.close()
    js.creates.clear()
    second = ActorLane(js, first.config, store)
    await second.start()
    assert js.creates == []
    await second.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("subjects", ["foreign.>"]),
        ("max_msg_size", 128_000),
        ("no_ack", True),
        ("allow_rollup_hdrs", True),
    ],
)
async def test_existing_stream_mismatch_is_not_updated(lane_setup, field, value):
    js, store, first = lane_setup
    await first.start()
    await first.close()
    js.stream_config = replace(js.stream_config, **{field: value})
    before = js.stream_config.as_dict()
    js.creates.clear()
    second = ActorLane(js, first.config, store)
    with pytest.raises(ValueError, match="stream"):
        await second.start()
    assert js.creates == []
    assert js.stream_config.as_dict() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("filter_subject", "foreign.>"),
        ("ack_policy", "none"),
        ("max_ack_pending", 64),
        ("deliver_subject", "unexpected.push"),
        ("backoff", [1.0]),
    ],
)
async def test_existing_consumer_mismatch_is_not_updated(lane_setup, field, value):
    js, store, first = lane_setup
    await first.start()
    await first.close()
    js.consumer_config = replace(js.consumer_config, **{field: value})
    before = js.consumer_config.as_dict()
    js.creates.clear()
    second = ActorLane(js, first.config, store)
    with pytest.raises(ValueError, match="consumer"):
        await second.start()
    assert js.creates == []
    assert js.consumer_config.as_dict() == before


async def test_lost_publish_ack_leaves_exact_outbox_pending_and_retry_deduplicates(lane_setup):
    js, store, lane = lane_setup
    await lane.start()
    js.lose_next_puback = True
    await lane.flush_outbox()
    assert store.pending_outbox()
    assert store.deliveries == []
    await lane.flush_outbox()
    assert not store.pending_outbox()
    assert len(js.messages) == 1
    assert js.publications[0] == js.publications[1]
    assert store.published == [(store.message.message_id, lane.stream, 1)]
    await lane.close()


async def test_offline_publish_does_not_deliver_locally_or_expose_body(lane_setup):
    js, store, lane = lane_setup
    await lane.start()
    js.publish_failure = OSError("SENSITIVE BODY")
    status = await lane.step()
    assert store.pending_outbox() and not store.deliveries
    assert "SENSITIVE" not in json.dumps(status)
    assert "Inspect the source" not in json.dumps(status)
    assert status["errors"]["publish"] == "OSError"
    await lane.close()


@pytest.mark.parametrize(
    "override", [{"stream": "foreign"}, {"seq": True}, {"seq": 0}, {"seq": -1}]
)
async def test_invalid_puback_does_not_advance_outbox(lane_setup, monkeypatch, override):
    js, store, lane = lane_setup
    await lane.start()
    publish = js.publish

    async def invalid_ack(*args, **kwargs):
        ack = await publish(*args, **kwargs)
        for key, value in override.items():
            setattr(ack, key, value)
        return ack

    monkeypatch.setattr(js, "publish", invalid_ack)
    await lane.flush_outbox()
    assert store.pending_outbox()
    assert lane.status()["errors"]["publish"] == "ValueError"
    await lane.close()


async def test_corrupt_outbox_identity_is_refused_before_publish(lane_setup, monkeypatch):
    js, store, lane = lane_setup
    await lane.start()
    monkeypatch.setattr(
        store,
        "pending_outbox",
        lambda limit: [
            {"message_id": str(uuid4()), "run_id": store.message.run_id, "payload": store.raw}
        ],
    )
    await lane.flush_outbox()
    assert js.publications == [] and store.published == []
    await lane.close()


async def test_commit_precedes_ack_and_broker_redelivery_does_not_duplicate(lane_setup):
    js, store, lane = lane_setup
    await lane.start()
    first = FakeMessage(lane, store)
    first.ack_failure = True
    js.subscription.messages.append(first)
    await lane.step()
    assert store.trace.index("commit") < store.trace.index("ack")
    assert not first.acked
    replay = FakeMessage(lane, store)
    js.subscription.messages.append(replay)
    status = await lane.step()
    assert replay.acked
    assert len(store.deliveries) == 2
    assert status["counts"]["duplicate_deliveries"] == 1
    await lane.close()


@pytest.mark.parametrize("delivery", ["expired", "rejected"])
async def test_durable_expiry_or_rejection_is_acknowledged(lane_setup, delivery):
    js, store, lane = lane_setup
    store.delivery = delivery
    await lane.start()
    message = FakeMessage(lane, store)
    js.subscription.messages.append(message)
    status = await lane.step()
    assert message.acked
    assert status["counts"][delivery] == 1
    await lane.close()


@pytest.mark.parametrize(
    "corruption",
    ["subject", "stream", "consumer", "sequence", "header", "payload", "oversized"],
)
async def test_foreign_or_malformed_broker_delivery_is_terminated(lane_setup, corruption):
    js, store, lane = lane_setup
    await lane.start()
    message = FakeMessage(lane, store)
    if corruption == "subject":
        message.subject = "foreign"
    elif corruption == "stream":
        message.metadata.stream = "foreign"
    elif corruption == "consumer":
        message.metadata.consumer = "foreign"
    elif corruption == "sequence":
        message.metadata.sequence.stream = True
    elif corruption == "header":
        message.headers["Nats-Msg-Id"] = str(uuid4())
    elif corruption == "payload":
        message.data = replace(store.message, recipient_epoch=2).to_bytes()
    else:
        message.data = b"x" * (MAX_ENVELOPE_BYTES + 1)
    js.subscription.messages.append(message)
    await lane.step()
    assert message.terminated and not message.acked
    assert store.deliveries == []
    await lane.close()


async def test_storage_failure_is_redeliverable_not_terminal(lane_setup):
    js, store, lane = lane_setup
    await lane.start()
    store.failure = sqlite3.OperationalError("private storage error")
    message = FakeMessage(lane, store)
    js.subscription.messages.append(message)
    status = await lane.step()
    assert message.nacked and not message.terminated and not message.acked
    assert status["errors"]["delivery"] == "OperationalError"
    await lane.close()


async def test_receive_batch_is_bounded_and_no_more_than_eight_messages_commit(lane_setup):
    js, store, lane = lane_setup
    await lane.start()
    js.subscription.messages.extend(FakeMessage(lane, store) for _ in range(12))
    await lane.step()
    assert js.subscription.batch_sizes == [8]
    assert len(store.deliveries) == 8
    assert len(js.subscription.messages) == 4
    await lane.close()


def actual_store(root: Path):
    from answerback_bridge.actors.store import ActorStore

    store = ActorStore(root / "actor-ledger.sqlite")
    run_id, sender, recipient = (str(uuid4()) for _ in range(3))
    store.create_run(run_id, str(root), ["sender", "recipient"])
    for actor, role, credential in [
        (sender, "sender", "a" * 64),
        (recipient, "recipient", "b" * 64),
    ]:
        store.stage_actor(run_id, actor, role, "codex_cli", credential)
        store.activate_actor(run_id, actor, 1, {"fixture_session": str(uuid4())})
    auth_sender = ActorAuth(run_id, sender, 1, "a" * 64, str(uuid4()))
    auth_recipient = ActorAuth(run_id, recipient, 1, "b" * 64, str(uuid4()))
    store.seal_run(run_id, auth=auth_sender)
    receipt = store.send(
        auth_sender, recipient, "request-1", "review_request", {"question": "Review"}
    )
    return store, auth_recipient, receipt


@pytest.fixture
async def actor_broker(tmp_path):
    broker = BrokerFixture(tmp_path)
    await broker.start()
    try:
        yield broker
    finally:
        await broker.stop()


async def test_real_nats_lost_puback_reconnect_and_exact_actor_delivery(actor_broker, monkeypatch):
    store, recipient, receipt = actual_store(actor_broker.config.root)
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.2)
        await lane.start()
        publish = bus.js.publish

        async def lose_ack(*args, **kwargs):
            await publish(*args, **kwargs)
            raise nats.errors.TimeoutError

        monkeypatch.setattr(bus.js, "publish", lose_ack)
        await lane.flush_outbox()
        assert store.pending_outbox()
        assert store.receive(recipient)["messages"] == []
        await lane.close()
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.2)
        await lane.start()
        await lane.step(wait_seconds=1)
        assert not store.pending_outbox()
        assert (await bus.js.stream_info(lane.stream)).state.messages == 1
        messages = store.receive(recipient)["messages"]
        assert len(messages) == 1
        assert messages[0]["message_id"] == receipt["message_id"]
        await lane.close()


async def test_real_nats_inbox_commit_survives_restart_before_ack(actor_broker, monkeypatch):
    from nats.aio.msg import Msg

    store, recipient, receipt = actual_store(actor_broker.config.root)
    original_ack = Msg.ack_sync

    async def lost_ack(message, *args, **kwargs):
        raise nats.errors.TimeoutError

    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.2)
        await lane.start()
        monkeypatch.setattr(Msg, "ack_sync", lost_ack)
        await lane.step(wait_seconds=1)
        message = store.receive(recipient)["messages"][0]
        store.acknowledge(recipient, message["message_id"], message["receipt_nonce"])
        assert message["message_id"] == receipt["message_id"]
        await lane.close()
    monkeypatch.setattr(Msg, "ack_sync", original_ack)
    await actor_broker.stop()
    await actor_broker.start()
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.2)
        await lane.start()
        status = await lane.step(wait_seconds=1)
        assert status["counts"]["duplicate_deliveries"] == 1
        assert store.receive(recipient)["messages"] == []
        assert (await bus.js.consumer_info(lane.stream, lane.consumer)).num_ack_pending == 0
        await lane.close()


async def test_real_nats_offline_outbox_resumes_without_local_fallback(actor_broker):
    store, recipient, _ = actual_store(actor_broker.config.root)
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store)
        await lane.start()
        await actor_broker.stop()
        await asyncio.sleep(0.05)
        status = await lane.step()
        assert status["errors"]
        assert store.pending_outbox()
        assert store.receive(recipient)["messages"] == []
        await lane.close()
    await actor_broker.start()
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        lane = ActorLane(bus.js, actor_broker.config, store)
        await lane.start()
        await lane.step(wait_seconds=1)
        assert len(store.receive(recipient)["messages"]) == 1
        await lane.close()


async def test_real_create_race_cannot_update_existing_consumer(actor_broker, monkeypatch):
    store, _, _ = actual_store(actor_broker.config.root)
    async with JetStreamBus(actor_broker.config, consume=False) as bus:
        existing = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.2)
        await existing.start()
        await existing.close()
        real_info = bus.js.consumer_info
        missing_once = True

        async def raced_info(*args, **kwargs):
            nonlocal missing_once
            if missing_once:
                missing_once = False
                raise NotFoundError
            return await real_info(*args, **kwargs)

        monkeypatch.setattr(bus.js, "consumer_info", raced_info)
        contender = ActorLane(bus.js, actor_broker.config, store, ack_wait=0.3)
        with pytest.raises(APIError):
            await contender.start()
        actual = (await real_info(existing.stream, existing.consumer)).config
        assert actual.ack_wait == 0.2
        assert bus.nc is not None and bus.nc.is_connected
        await contender.close()
