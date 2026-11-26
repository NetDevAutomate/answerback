import asyncio
from pathlib import Path

import pytest
from bus_helpers import BrokerFixture

pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")
import nats.errors

from codex_herdr_bridge.bus.jetstream import JetStreamBus
from codex_herdr_bridge.bus.store import BusStore


@pytest.fixture
async def broker(tmp_path: Path):
    server = BrokerFixture(tmp_path)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_authenticated_publish_dedup_and_restart_redelivery(broker: BrokerFixture) -> None:
    cfg = broker.config
    store = BusStore(cfg.root / "ledger.sqlite")
    receipt = store.submit(
        request_id="one",
        peer_id="kiro:worker",
        project="/work",
        routing_digest="a" * 64,
        message="Ping",
    )
    row = next(row for row in store.pending_outbox() if row["kind"] == "command")
    async with JetStreamBus(cfg, ack_wait=0.2) as bus:
        await bus.publish(row)
        await bus.publish(row)
        message = await bus.fetch(wait_seconds=1)
        assert message is not None
        assert message.data == row["payload"].encode()
        assert (await bus.js.stream_info(cfg.commands_stream)).state.messages == 1
        # Deliberately omit acknowledgement, as if the worker died.
    await broker.stop()
    await broker.start()
    async with JetStreamBus(cfg, ack_wait=0.2) as bus:
        replay = await bus.fetch(wait_seconds=2)
        assert replay is not None and replay.data == row["payload"].encode()
        await replay.ack_sync(timeout=1)
        assert await bus.fetch(wait_seconds=0.15) is None
    assert store.get(receipt["command_id"])["delivery"] == "queued"


async def test_unauthenticated_connection_is_rejected(broker: BrokerFixture) -> None:
    errors = []

    async def on_error(error):
        errors.append(type(error).__name__)

    with pytest.raises((nats.errors.Error, asyncio.TimeoutError)):
        async with asyncio.timeout(2):
            await nats.connect(
                broker.config.url,
                allow_reconnect=False,
                max_reconnect_attempts=0,
                connect_timeout=0.5,
                error_cb=on_error,
            )
    assert errors


async def test_outbox_remains_pending_after_lost_publish_ack(broker: BrokerFixture) -> None:
    cfg = broker.config
    store = BusStore(cfg.root / "ledger.sqlite")
    store.submit(
        request_id="lost-ack",
        peer_id="kiro:worker",
        project="/work",
        routing_digest="a" * 64,
        message="Ping",
    )
    row = store.pending_outbox()[0]
    async with JetStreamBus(cfg) as bus:
        await bus.publish(row)
        # A publisher crash before this local commit is safe to replay.
        assert len(store.pending_outbox()) == 2
        await bus.publish(row)
        store.mark_published(row["id"])
        assert (await bus.js.stream_info(cfg.commands_stream)).state.messages == 1
    assert len(store.pending_outbox()) == 1


async def test_full_event_stream_does_not_block_command_delivery(broker: BrokerFixture) -> None:
    cfg = broker.config
    store = BusStore(cfg.root / "ledger.sqlite")
    async with JetStreamBus(cfg) as bus:
        events = (await bus.js.stream_info(cfg.events_stream)).config
        events.max_msgs = 1
        await bus.js.update_stream(events)
        for index in range(3):
            store.submit(
                request_id=f"event-backpressure-{index}",
                peer_id="kiro:worker",
                project="/work",
                routing_digest="a" * 64,
                message="Ping",
            )
            errors = await bus.flush_outbox(store)
        assert errors == {"event": "ServiceUnavailableError"}
        assert (await bus.js.stream_info(cfg.commands_stream)).state.messages == 3
        assert all(row["kind"] == "event" for row in store.pending_outbox())


class ConsumerTransport:
    """Small fake implementing the pinned client's existing-durable bind behavior."""

    def __init__(self, config, *, existing=True):
        from types import SimpleNamespace

        from nats.js.api import AckPolicy, ConsumerConfig

        self._prefix = "$JS.API"
        self.version = SimpleNamespace(major=2, minor=15, patch=3)
        self.consumer = (
            ConsumerConfig(
                name="coordinator_v1",
                durable_name="coordinator_v1",
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30,
                max_ack_pending=1,
                max_deliver=-1,
                max_waiting=512,
                num_replicas=0,
                filter_subject=config.commands_subject,
            )
            if existing
            else None
        )
        self.streams = {}
        self.bound = False
        self.closed = False
        self.creates = []
        self.raced = None
        self.changed_after_create = None

    async def stream_info(self, name):
        from types import SimpleNamespace

        from nats.js.errors import NotFoundError

        if name not in self.streams:
            raise NotFoundError()
        return SimpleNamespace(config=self.streams[name])

    async def add_stream(self, config):
        self.streams[config.name] = config

    async def consumer_info(self, *args):
        from types import SimpleNamespace

        from nats.js.errors import NotFoundError

        if self.consumer is None:
            raise NotFoundError()
        return SimpleNamespace(config=self.consumer)

    async def pull_subscribe(self, subject, *, config, **kwargs):
        # nats-py 2.16.0 uses its supplied config only for a missing durable.
        if self.consumer is None:
            self.consumer = self.raced or config
        self.bound = True
        return object()

    async def pull_subscribe_bind(self, **kwargs):
        self.bound = True
        return object()

    async def _api_request(self, subject, payload, **kwargs):
        import json

        from nats.js.api import ConsumerConfig
        from nats.js.errors import APIError

        request = json.loads(payload)
        self.creates.append((subject, request))
        if self.raced is not None:
            self.consumer = self.raced
            if request.get("action") == "create":
                raise APIError(description="Fixture create race")
        self.consumer = self.changed_after_create or ConsumerConfig.from_response(
            dict(request["config"])
        )
        return {}

    @property
    def connected_server_version(self):
        return self.version

    def jetstream(self, **kwargs):
        return self

    async def close(self):
        self.closed = True


@pytest.fixture
def consumer_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bus_helpers import unused_server_binary

    from answerback_bridge.bus import jetstream
    from answerback_bridge.bus.config import BusConfig

    config = BusConfig.create(
        tmp_path / "config.json", server_binary=unused_server_binary(tmp_path)
    )
    transport = ConsumerTransport(config)

    async def connect(*args, **kwargs):
        return transport

    # Both names allow the same behavioral tests to exercise the BASE connection
    # caller and the verified connector, without opening any TCP connection.
    monkeypatch.setattr(nats, "connect", connect)
    monkeypatch.setattr(jetstream, "connect_broker", connect, raising=False)
    return config, transport


@pytest.mark.parametrize(
    "field,value",
    [
        ("ack_policy", "none"),
        ("ack_wait", 1),
        ("max_ack_pending", 50000),
        ("max_deliver", 1),
        ("filter_subject", "other.commands"),
        ("deliver_policy", "new"),
        ("replay_policy", "original"),
        ("backoff", [1, 2]),
        ("filter_subjects", ["other.commands"]),
        ("headers_only", True),
        ("deliver_subject", "push.commands"),
        ("inactive_threshold", 1),
        ("pause_until", "2099-01-01T00:00:00Z"),
    ],
)
async def test_divergent_coordinator_consumer_is_refused(consumer_transport, field, value) -> None:
    """red_at_base: verify effective delivery settings before binding a durable."""
    config, transport = consumer_transport
    setattr(transport.consumer, field, value)
    with pytest.raises(ValueError, match="consumer.*contract"):
        async with JetStreamBus(config):
            pass
    assert not transport.bound
    assert transport.closed
    assert transport.creates == []
    assert getattr(transport.consumer, field) == value


async def test_matching_coordinator_consumer_is_bound_without_update(consumer_transport) -> None:
    """pins_existing: a matching durable continues to bind without mutation."""
    config, transport = consumer_transport
    async with JetStreamBus(config):
        assert transport.bound
        assert not transport.creates
    assert transport.closed


async def test_publish_only_bus_leaves_consumer_untouched(consumer_transport) -> None:
    """pins_existing: actor and CLI publishers do not establish a consumer."""
    config, transport = consumer_transport
    transport.consumer.ack_policy = "none"
    async with JetStreamBus(config, consume=False):
        assert not transport.bound
        assert not transport.creates
    assert transport.consumer.ack_policy == "none"


async def test_coordinator_consumer_creation_is_create_only(consumer_transport) -> None:
    """red_at_base: a first bind must not silently update a raced durable."""
    config, transport = consumer_transport
    transport.consumer = None
    async with JetStreamBus(config):
        assert transport.bound
        assert len(transport.creates) == 1
        subject, request = transport.creates[0]
        assert subject == f"$JS.API.CONSUMER.CREATE.{config.commands_stream}.coordinator_v1"
        assert request["action"] == "create"
        assert request["stream_name"] == config.commands_stream
        assert request["config"]["ack_wait"] == 30_000_000_000


@pytest.mark.parametrize("race", ["create", "readback"])
async def test_consumer_race_is_not_bound_or_overwritten(consumer_transport, race) -> None:
    """red_at_base: creation and readback must both preserve the consumer contract."""
    from copy import deepcopy

    from nats.js.errors import APIError

    config, transport = consumer_transport
    divergent = deepcopy(transport.consumer)
    divergent.max_deliver = 1
    transport.consumer = None
    if race == "create":
        transport.raced = divergent
    else:
        transport.changed_after_create = divergent
    with pytest.raises((ValueError, APIError)):
        async with JetStreamBus(config):
            pass
    assert not transport.bound
    assert transport.closed
    assert divergent.max_deliver == 1


@pytest.mark.parametrize("version", [(2, 9, 9), (None, None, None)])
async def test_consumer_creation_requires_create_only_server(consumer_transport, version) -> None:
    """red_at_base: old or unknown servers cannot silently ignore create-only."""
    from types import SimpleNamespace

    config, transport = consumer_transport
    transport.consumer = None
    transport.version = SimpleNamespace(
        **dict(zip(("major", "minor", "patch"), version, strict=True))
    )
    with pytest.raises(ValueError, match="2.10.0"):
        async with JetStreamBus(config):
            pass
    assert not transport.bound
    assert not transport.creates
    assert transport.closed


@pytest.mark.parametrize("kind", ["command", "event"])
async def test_poison_outbox_row_is_quarantined_across_reconnects(
    tmp_path: Path, kind: str
) -> None:
    """red_at_base: a persistent failure has a durable bound and cannot block its kind."""
    from answerback_bridge.bus.config import BusConfig

    config = BusConfig(tmp_path / "config.json", 4222, "/unused-nats-server")
    store = BusStore(tmp_path / "ledger.sqlite", clock=lambda: 100)
    for index in range(3):
        store.submit(
            request_id=f"poison-{index}",
            peer_id="kiro:worker",
            project="/work",
            routing_digest="a" * 64,
            message="Dummy assignment",
        )
    rows = store.pending_outbox(kind=kind)
    poison = rows[0]
    healthy = {row["id"] for row in rows[1:]}
    published: set[str] = set()
    failures = 0

    class Publisher:
        async def publish(
            self, subject: str, payload: bytes, *, headers: dict[str, str], **kwargs: object
        ) -> None:
            nonlocal failures
            if headers["Nats-Msg-Id"] == poison["id"]:
                failures += 1
                raise nats.js.errors.APIError(description="dummy private diagnostic")
            published.add(headers["Nats-Msg-Id"])

    for attempt in range(20):
        # Reconstruct both objects, as after a coordinator restart/reconnect.
        store = BusStore(store.path, clock=lambda: 120)
        bus = JetStreamBus(config)
        bus.js = Publisher()
        assert await bus.flush_outbox(store) == {kind: "APIError"}
        if attempt < 19:
            assert not healthy & published
    assert healthy <= published
    assert poison["id"] not in published
    assert store.pending_outbox() == []
    quarantined = store.quarantined_outbox()
    assert quarantined == [
        {
            "id": poison["id"],
            "kind": kind,
            "command_id": poison["command_id"],
            "attempts": 20,
            "last_attempt_at": 120,
            "quarantined_at": 120,
            "error_type": "APIError",
        }
    ]
    assert store.outbox_quarantine_counts()[kind] == 1
    assert await bus.flush_outbox(store) == {}
    assert failures == 20
    # Cancellation cannot erase quarantined evidence (including a command row).
    store.cancel(poison["command_id"])
    assert store.quarantined_outbox() == quarantined
    with store.connection() as db:
        retained = dict(db.execute("SELECT * FROM outbox WHERE id=?", (poison["id"],)).fetchone())
        assert retained == poison
        assert "dummy private diagnostic" not in str(
            [dict(row) for row in db.execute("SELECT * FROM outbox_failures")]
        )


async def test_transient_outbox_error_keeps_order_then_recovers(tmp_path: Path) -> None:
    """pins_existing: a transient error leaves durable pending work for the next flush."""
    from answerback_bridge.bus.config import BusConfig

    config = BusConfig(tmp_path / "config.json", 4222, "/unused-nats-server")
    store = BusStore(tmp_path / "ledger.sqlite")
    store.submit(
        request_id="transient",
        peer_id="kiro:worker",
        project="/work",
        routing_digest="a" * 64,
        message="Dummy assignment",
    )

    class Publisher:
        fail = True

        async def publish(self, subject: str, payload: bytes, **kwargs: object) -> None:
            if self.fail:
                raise nats.js.errors.APIError(description="Temporary fixture failure")

    publisher = Publisher()
    bus = JetStreamBus(config)
    bus.js = publisher
    assert await bus.flush_outbox(store) == {"command": "APIError", "event": "APIError"}
    assert len(store.pending_outbox()) == 2
    publisher.fail = False
    assert await bus.flush_outbox(store) == {}
    assert store.pending_outbox() == []


async def test_legacy_outbox_opens_and_flushes_without_dropping_rows(tmp_path: Path) -> None:
    """pins_existing: a database with the exact BASE outbox schema remains usable."""
    import sqlite3

    from answerback_bridge.bus.config import BusConfig

    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE outbox (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "command_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, "
            "published_at REAL)"
        )
        db.execute("INSERT INTO outbox VALUES ('pending','event','one','{}',100,NULL)")
        db.execute("INSERT INTO outbox VALUES ('published','command','one','{}',100,110)")
    store = BusStore(path, clock=lambda: 120)
    reopened = BusStore(path, clock=lambda: 120)
    bus = JetStreamBus(BusConfig(tmp_path / "config.json", 4222, "/unused-nats-server"))
    published = []

    class Publisher:
        async def publish(
            self, subject: str, payload: bytes, *, headers: dict[str, str], **kwargs: object
        ) -> None:
            published.append(headers["Nats-Msg-Id"])

    bus.js = Publisher()
    assert await bus.flush_outbox(reopened) == {}
    assert published == ["pending"]
    assert store.pending_outbox() == []
    with store.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM outbox ORDER BY id")] == [
            ("pending", "event", "one", "{}", 100, 120),
            ("published", "command", "one", "{}", 100, 110),
        ]


async def test_consumer_ack_wait_compares_at_wire_precision(consumer_transport) -> None:
    """pins_existing: NATS truncates fractional seconds to integer nanoseconds."""
    config, transport = consumer_transport
    transport.consumer.ack_wait = 0.123456789
    async with JetStreamBus(config, ack_wait=0.1234567899):
        assert transport.bound
        assert transport.creates == []
