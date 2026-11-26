"""Bounded JetStream delivery. Only the coordinator interprets task outcomes."""

import json
from typing import TYPE_CHECKING, Any

import nats
import nats.errors
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.client import JetStreamContext
from nats.js.errors import APIError, NotFoundError

from .broker import connect_broker
from .config import BusConfig

if TYPE_CHECKING:
    from .store import BusStore


class JetStreamBus:
    def __init__(self, config: BusConfig, *, ack_wait: float = 30, consume: bool = True) -> None:
        self.config, self.ack_wait = config, ack_wait
        self.consume = consume
        self.nc: Client | None = None
        self.js: JetStreamContext
        self.subscription: JetStreamContext.PullSubscription

    async def __aenter__(self) -> "JetStreamBus":
        self.nc = await connect_broker(self.config)
        try:
            self.js = self.nc.jetstream(timeout=2)
            for name, subject, size, age, retention in (
                (
                    self.config.commands_stream,
                    self.config.commands_subject,
                    64 * 1024 * 1024,
                    7 * 86400,
                    RetentionPolicy.WORK_QUEUE,
                ),
                (
                    self.config.events_stream,
                    self.config.events_subject,
                    32 * 1024 * 1024,
                    30 * 86400,
                    RetentionPolicy.LIMITS,
                ),
            ):
                desired = StreamConfig(
                    name=name,
                    subjects=[subject],
                    storage=StorageType.FILE,
                    retention=retention,
                    discard=DiscardPolicy.NEW,
                    max_bytes=size,
                    max_msgs=100_000,
                    max_msg_size=128_000,
                    max_age=age,
                    num_replicas=1,
                    duplicate_window=120,
                )
                try:
                    current = (await self.js.stream_info(name)).config
                except NotFoundError:
                    await self.js.add_stream(desired)
                else:
                    if (
                        current.subjects != desired.subjects
                        or current.storage != desired.storage
                        or current.retention != desired.retention
                        or current.discard != desired.discard
                        or current.max_bytes != desired.max_bytes
                        or current.max_age != desired.max_age
                        or current.max_msgs != desired.max_msgs
                        or current.max_msg_size != desired.max_msg_size
                        or current.num_replicas != desired.num_replicas
                        or current.duplicate_window != desired.duplicate_window
                    ):
                        raise ValueError("Existing bus stream differs from the configured contract")
            if not self.consume:
                return self
            await self._ensure_consumer()
            self.subscription = await self.js.pull_subscribe_bind(
                stream=self.config.commands_stream,
                consumer="coordinator_v1",
                pending_msgs_limit=4,
                pending_bytes_limit=512_000,
            )
            return self
        except BaseException:
            await self.nc.close()
            raise

    async def _ensure_consumer(self) -> None:
        desired = ConsumerConfig(
            name="coordinator_v1",
            durable_name="coordinator_v1",
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.ack_wait,
            max_ack_pending=1,
            max_deliver=-1,
            max_waiting=512,
            num_replicas=0,
            filter_subject=self.config.commands_subject,
        )
        try:
            current = (
                await self.js.consumer_info(self.config.commands_stream, "coordinator_v1")
            ).config
        except NotFoundError:
            assert self.nc is not None
            version = self.nc.connected_server_version
            required = "Bus consumer creation requires NATS 2.10.0 or newer create-only semantics"
            parts: list[int] = []
            for field in ("major", "minor", "patch"):
                value = getattr(version, field, None)
                if type(value) is not int or value < 0:
                    raise ValueError(required) from None
                parts.append(value)
            if tuple(parts) < (2, 10, 0):
                raise ValueError(required) from None
            # nats-py 2.16 has no public create-only action argument. A raced
            # durable must be refused by the server, never silently updated.
            await self.js._api_request(
                f"{self.js._prefix}.CONSUMER.CREATE.{self.config.commands_stream}.coordinator_v1",
                json.dumps(
                    {
                        "stream_name": self.config.commands_stream,
                        "config": desired.as_dict(),
                        "action": "create",
                    }
                ).encode(),
                timeout=2,
            )
            current = (
                await self.js.consumer_info(self.config.commands_stream, "coordinator_v1")
            ).config
        # Compare the broker's representation: NATS truncates seconds to integer
        # nanoseconds on the wire. Keep the original values for the create request.
        desired = ConsumerConfig.from_response(desired.as_dict())
        fields = (
            "name",
            "durable_name",
            "deliver_policy",
            "ack_policy",
            "ack_wait",
            "max_ack_pending",
            "max_deliver",
            "max_waiting",
            "filter_subject",
            "replay_policy",
            "num_replicas",
        )
        overrides = (
            "opt_start_seq",
            "opt_start_time",
            "backoff",
            "filter_subjects",
            "deliver_subject",
            "deliver_group",
            "flow_control",
            "idle_heartbeat",
            "headers_only",
            "inactive_threshold",
            "mem_storage",
            "pause_until",
            "rate_limit_bps",
        )
        if any(getattr(current, field) != getattr(desired, field) for field in fields) or any(
            getattr(current, field, None) for field in overrides
        ):
            raise ValueError("Existing bus consumer differs from the configured contract")

    async def __aexit__(self, *_: object) -> None:
        if self.nc is not None:
            await self.nc.close()

    async def flush_outbox(self, store: "BusStore") -> dict[str, str]:
        errors = {}
        for kind in ("command", "event"):
            for row in store.pending_outbox(100, kind=kind):
                try:
                    await self.publish(row)
                except APIError as error:
                    errors[kind] = type(error).__name__
                    # Keep ordering during bounded retries; quarantine a persistent
                    # failure so healthy later rows of this kind can advance.
                    if store.record_outbox_failure(row["id"], type(error).__name__):
                        continue
                    break
                store.mark_published(row["id"])
        return errors

    async def publish(self, row: dict[str, Any]) -> None:
        subject = (
            self.config.commands_subject if row["kind"] == "command" else self.config.events_subject
        )
        await self.js.publish(
            subject, row["payload"].encode(), headers={"Nats-Msg-Id": row["id"]}, timeout=2
        )

    async def fetch(self, *, wait_seconds: float = 0.25) -> Msg | None:
        try:
            messages = await self.subscription.fetch(1, timeout=wait_seconds)
        except nats.errors.TimeoutError:
            return None
        return messages[0] if messages else None
