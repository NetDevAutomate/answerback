"""Optional actor delivery on the existing JetStream connection, without task execution."""

from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING, Any, Self

import nats.errors
from nats.aio.msg import Msg
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    ReplayPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.client import JetStreamContext
from nats.js.errors import NotFoundError

from ..bus.config import BusConfig
from .model import MAX_ENVELOPE_BYTES, MAX_RECEIVE, ActorEnvelope, positive_integer

if TYPE_CHECKING:
    from .store import ActorStore

PUBLISH_BATCH = 8
REQUEST_TIMEOUT = 2.0
STEP_TIMEOUT = 8.0


class ActorLane:
    """A bounded mailbox lane; the caller owns connection and reconnect lifecycle."""

    def __init__(
        self,
        js: JetStreamContext,
        config: BusConfig,
        store: ActorStore,
        *,
        ack_wait: float = 30.0,
    ) -> None:
        if (
            type(ack_wait) not in (int, float)
            or not math.isfinite(ack_wait)
            or not 0 < ack_wait <= 600
        ):
            raise ValueError("Actor acknowledgement wait must be between 0 and 600 seconds")
        self.js, self.config, self.store = js, config, store
        self.stream = f"AGENT_ACTOR_MESSAGES_{config.namespace.upper()}"
        self.subject = f"agents.v1.{config.namespace}.actor_messages"
        self.consumer = "actor_mailbox_v1"
        self.ack_wait = ack_wait
        self._subscription: JetStreamContext.PullSubscription | None = None
        self._ready = False
        self._closed = False
        self._step_lock = asyncio.Lock()
        self._errors: dict[str, str] = {}
        self._counts = {
            "published": 0,
            "delivered": 0,
            "expired": 0,
            "rejected": 0,
            "duplicate_deliveries": 0,
            "invalid_deliveries": 0,
            "broker_acks": 0,
        }

    def status(self) -> dict[str, Any]:
        return {
            "ready": self._ready and not self._closed and not self._errors,
            "closed": self._closed,
            "stream": self.stream,
            "subject": self.subject,
            "consumer": self.consumer,
            "counts": dict(self._counts),
            "errors": dict(self._errors),
        }

    def _require_create_only_server(self) -> None:
        # NATS 2.10.0 consumer.go explicitly parses action="create" and refuses
        # a differing existing consumer. Older servers may ignore unknown JSON.
        version = self.js._nc.connected_server_version
        parts: list[int] = []
        for field in ("major", "minor", "patch"):
            value = getattr(version, field, None)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    "Actor delivery requires NATS 2.10.0 or newer create-only semantics"
                )
            parts.append(value)
        if tuple(parts) < (2, 10, 0):
            raise ValueError("Actor delivery requires NATS 2.10.0 or newer create-only semantics")

    def _stream_config(self) -> StreamConfig:
        return StreamConfig(
            name=self.stream,
            subjects=[self.subject],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.NEW,
            max_bytes=64 * 1024 * 1024,
            max_msgs=10_000,
            max_msg_size=MAX_ENVELOPE_BYTES,
            max_age=7 * 86400,
            max_consumers=1,
            num_replicas=1,
            duplicate_window=120,
        )

    def _consumer_config(self) -> ConsumerConfig:
        return ConsumerConfig(
            name=self.consumer,
            durable_name=self.consumer,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.ack_wait,
            max_ack_pending=MAX_RECEIVE,
            max_deliver=-1,
            max_waiting=MAX_RECEIVE,
            filter_subject=self.subject,
            replay_policy=ReplayPolicy.INSTANT,
            num_replicas=1,
            mem_storage=False,
        )

    def _check_stream(self, current: StreamConfig, desired: StreamConfig) -> None:
        fields = (
            "name",
            "subjects",
            "storage",
            "retention",
            "discard",
            "max_bytes",
            "max_msgs",
            "max_msg_size",
            "max_age",
            "max_consumers",
            "num_replicas",
            "duplicate_window",
            "max_msgs_per_subject",
            "no_ack",
            "sealed",
            "allow_rollup_hdrs",
            "discard_new_per_subject",
            "deny_delete",
            "deny_purge",
        )
        if any(getattr(current, key) != getattr(desired, key) for key in fields) or any(
            getattr(current, key, None)
            for key in ("mirror", "sources", "republish", "subject_transform")
        ):
            raise ValueError("Existing actor stream differs from its immutable contract")

    def _check_consumer(self, current: ConsumerConfig, desired: ConsumerConfig) -> None:
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
        if any(getattr(current, key) != getattr(desired, key) for key in fields) or any(
            getattr(current, key, None)
            for key in (
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
        ):
            raise ValueError("Existing actor consumer differs from its immutable contract")

    async def start(self) -> Self:
        if self._closed or self._ready:
            raise ValueError("Actor lane is already started or closed")
        try:
            self._require_create_only_server()
            async with asyncio.timeout(10):
                desired_stream = self._stream_config()
                try:
                    current_stream = (await self.js.stream_info(self.stream)).config
                except NotFoundError:
                    await self.js.add_stream(desired_stream)
                    current_stream = (await self.js.stream_info(self.stream)).config
                self._check_stream(current_stream, desired_stream)

                desired_consumer = self._consumer_config()
                try:
                    current_consumer = (
                        await self.js.consumer_info(self.stream, self.consumer)
                    ).config
                except NotFoundError:
                    # Pinned nats-py 2.16 has no public create-only action argument.
                    # Unlike add_consumer(), this request cannot alter a raced consumer.
                    await self.js._api_request(
                        f"{self.js._prefix}.CONSUMER.CREATE.{self.stream}.{self.consumer}",
                        json.dumps(
                            {
                                "stream_name": self.stream,
                                "config": desired_consumer.as_dict(),
                                "action": "create",
                            }
                        ).encode(),
                        timeout=REQUEST_TIMEOUT,
                    )
                    current_consumer = (
                        await self.js.consumer_info(self.stream, self.consumer)
                    ).config
                self._check_consumer(current_consumer, desired_consumer)
                self._subscription = await self.js.pull_subscribe_bind(
                    stream=self.stream,
                    consumer=self.consumer,
                    pending_msgs_limit=MAX_RECEIVE,
                    pending_bytes_limit=MAX_RECEIVE * MAX_ENVELOPE_BYTES,
                )
            self._ready = True
            self._errors.clear()
            return self
        except Exception as error:
            self._errors["setup"] = type(error).__name__
            raise

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _require_ready(self) -> None:
        if not self._ready or self._closed or self._subscription is None:
            raise ValueError("Actor lane is not started or is closed")

    async def flush_outbox(self) -> None:
        self._require_ready()
        self._errors.pop("publish", None)
        try:
            rows = await asyncio.to_thread(self.store.pending_outbox, PUBLISH_BATCH)
            if not isinstance(rows, list) or len(rows) > PUBLISH_BATCH:
                raise ValueError("Actor outbox exceeds its batch contract")
            for row in rows:
                payload = row["payload"]
                envelope = ActorEnvelope.from_bytes(payload)
                if (
                    envelope.message_id != row["message_id"]
                    or envelope.run_id != row["run_id"]
                    or envelope.to_bytes() != payload
                ):
                    raise ValueError("Actor outbox identity or canonical payload is invalid")
                ack = await self.js.publish(
                    self.subject,
                    payload,
                    headers={"Nats-Msg-Id": envelope.message_id},
                    timeout=REQUEST_TIMEOUT,
                )
                if ack.stream != self.stream:
                    raise ValueError("Actor publish acknowledgement names another stream")
                positive_integer(ack.seq, "Actor publish sequence")
                await asyncio.to_thread(
                    self.store.mark_published, envelope.message_id, self.stream, ack.seq
                )
                self._counts["published"] += 1
        except Exception as error:
            # No acknowledgement means the durable outbox remains pending. A later
            # connection retries the same message ID; there is no local inbox path.
            self._errors["publish"] = type(error).__name__

    def _delivery_identity(self, message: Msg) -> tuple[ActorEnvelope, int]:
        envelope = ActorEnvelope.from_bytes(message.data)
        try:
            metadata = message.metadata
            if (
                message.subject != self.subject
                or metadata.stream != self.stream
                or metadata.consumer != self.consumer
                or (message.headers or {}).get("Nats-Msg-Id") != envelope.message_id
            ):
                raise ValueError("Actor delivery has an unexpected broker identity")
            seq = positive_integer(metadata.sequence.stream, "Actor delivery sequence")
        except (AttributeError, nats.errors.Error) as error:
            raise ValueError("Actor broker metadata is unavailable") from error
        return envelope, seq

    async def _settle_failure(self, message: Msg, *, invalid: bool) -> None:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                if invalid:
                    await message.term()
                else:
                    await message.nak(delay=1)
        except Exception as error:
            self._errors["term" if invalid else "nak"] = type(error).__name__

    async def _deliver(self, message: Msg) -> None:
        try:
            envelope, seq = self._delivery_identity(message)
            delivered = await asyncio.to_thread(self.store.deliver, message.data, self.stream, seq)
            if (
                not isinstance(delivered, dict)
                or delivered.get("message_id") != envelope.message_id
                or delivered.get("delivery") not in {"delivered", "expired", "rejected"}
                or type(delivered.get("duplicate")) is not bool
            ):
                raise RuntimeError("Actor inbox commit returned an invalid receipt")
        except ValueError as error:
            self._counts["invalid_deliveries"] += 1
            self._errors["validation"] = type(error).__name__
            await self._settle_failure(message, invalid=True)
            return
        except Exception as error:
            self._errors["delivery"] = type(error).__name__
            await self._settle_failure(message, invalid=False)
            return
        if delivered["duplicate"]:
            self._counts["duplicate_deliveries"] += 1
        else:
            self._counts[delivered["delivery"]] += 1
        try:
            # The committed inbox is authoritative even if this broker ACK is lost.
            # Redelivery will record another observation, not another model message.
            await message.ack_sync(timeout=REQUEST_TIMEOUT)
            self._counts["broker_acks"] += 1
        except Exception as error:
            self._errors["ack"] = type(error).__name__

    async def step(self, *, wait_seconds: float = 0.1) -> dict[str, Any]:
        self._require_ready()
        if (
            type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds)
            or not 0 < wait_seconds <= 1
        ):
            raise ValueError("Actor fetch wait must be between 0 and 1 second")
        if self._step_lock.locked():
            raise ValueError("An actor lane step is already active")
        async with self._step_lock:
            self._errors.clear()
            try:
                async with asyncio.timeout(STEP_TIMEOUT):
                    await self.flush_outbox()
                    assert self._subscription is not None
                    try:
                        messages = await self._subscription.fetch(MAX_RECEIVE, timeout=wait_seconds)
                    except nats.errors.TimeoutError:
                        return self.status()
                    if len(messages) > MAX_RECEIVE:
                        raise ValueError("Actor fetch exceeded its batch contract")
                    for message in messages:
                        if self._closed:
                            break
                        await self._deliver(message)
            except Exception as error:
                self._errors["receive"] = type(error).__name__
        return self.status()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        if self._subscription is not None:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    await self._subscription.unsubscribe()
            except Exception as error:
                self._errors["close"] = type(error).__name__
