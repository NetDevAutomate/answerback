"""One coordinator owns admission; broker redelivery never authorises another attempt."""

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from ..adapters.contracts import PreparedPeer as PreparedTarget
from ..adapters.contracts import TaskReceipt
from ..errors import AdmissionRefused
from .config import BusConfig
from .extra import jetstream_bus_class
from .model import Command
from .store import BusStore

if TYPE_CHECKING:
    from ..actors.store import ActorStore


class Gateway(Protocol):
    async def prepare(self, command: Command) -> PreparedTarget: ...
    async def send(self, target: PreparedTarget, command: Command) -> TaskReceipt: ...
    def lookup(self, command: Command) -> TaskReceipt | None: ...
    def get(self, task_id: str) -> TaskReceipt: ...


class Coordinator:
    def __init__(
        self,
        store: BusStore,
        gateway: Gateway,
        *,
        actor_store: Callable[[], "ActorStore | None"] | None = None,
    ) -> None:
        self.store, self.gateway = store, gateway
        self.actor_store = actor_store
        self.state: dict[str, Any] = {
            "connected": False,
            "last_error": None,
            "rejected_messages": 0,
            "outbox_errors": {},
            "outbox_quarantined": self.store.outbox_quarantine_counts(),
            "actors": {"enabled": False, "ready": False},
        }
        self._projection_offset = 0
        self._failure_reason_at: float | None = None
        self._broker_reason: str | None = None

    def recover_claims(self, *, detail: str | None = None) -> None:
        for row in self.store.entries("dispatching"):
            command = self.store.command(row["command_id"])
            try:
                task = self.gateway.lookup(command)
                if task is not None:
                    self.store.bind(command.command_id, task.as_record())
                    continue
            except (ValueError, KeyError):
                pass
            self.store.finish(
                command.command_id,
                "uncertain",
                detail or "Coordinator stopped during admission; inspect before any new attempt",
            )

    async def process(self, raw: bytes) -> None:
        command = Command.from_bytes(raw)
        self.store.verify_payload(command)
        if self.store.get(command.command_id)["delivery"] != "queued":
            return
        if command.expires_at <= self.store.clock():
            self.store.claim(command.command_id)  # records expiry without contacting a harness
            return
        try:
            async with asyncio.timeout(30):
                target = await self.gateway.prepare(command)
        except Exception as error:
            # The detail reaches bus_result and bus_events: keep the exception's kind as a
            # stable code and drop its text, which may carry provider or peer detail.
            self.store.finish(
                command.command_id,
                "rejected",
                f"Admission preflight refused ({type(error).__name__})",
            )
            return
        if not self.store.claim(command.command_id, target.key):
            return
        try:
            existing = self.gateway.lookup(command)
            if existing is None:
                async with asyncio.timeout(60):
                    existing = await self.gateway.send(target, command)
            self.store.bind(command.command_id, existing.as_record())
        except AdmissionRefused as error:
            self.store.finish(
                command.command_id,
                "rejected",
                str(error),
                blocking_task_id=error.blocking_task_id,
            )
        except Exception as error:
            # The adapter may have reserved/submitted before its response was lost.
            # Preserve the claim, never retry the external send.
            self.state["last_error"] = type(error).__name__
            self.recover_claims(
                detail=f"Adapter admission raised {type(error).__name__}; "
                "no matching receipt could be bound. Inspect before any new attempt"
            )

    def project_receipts(self) -> None:
        rows = self.store.entries("bound")
        batch = rows[self._projection_offset : self._projection_offset + 100]
        for row in batch:
            self.store.project_task(row["command_id"], self.gateway.get(row["task_id"]).as_record())
        self._projection_offset += len(batch)
        if self._projection_offset >= len(rows):
            self._projection_offset = 0

    async def _keep_ack_alive(self, message: Any) -> None:
        while True:
            await asyncio.sleep(5)
            await message.in_progress()

    async def _deliver(self, message: Any) -> None:
        heartbeat = asyncio.create_task(self._keep_ack_alive(message))
        try:
            try:
                await self.process(message.data)
            except (ValueError, KeyError):
                self.state["rejected_messages"] += 1
                await message.term()
            else:
                # Ack only after the durable claim/binding or rejection is recorded.
                await message.ack_sync(timeout=2)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self, config: BusConfig, stopping: asyncio.Event) -> None:
        # nats-py is the optional `bus` extra: a missing install is refused here, once, before
        # the ledger is touched, instead of being retried as a transport error below.
        try:
            bus_class = jetstream_bus_class()
            self.recover_claims()
        except Exception as error:
            self.state.update(connected=False, last_error=type(error).__name__)
            raise
        while not stopping.is_set():
            try:
                async with bus_class(config) as bus:
                    self.state.update(connected=True, last_error=None)
                    self._failure_reason_at = None
                    self._broker_reason = None
                    actor_worker = None
                    if self.actor_store is not None:
                        from ..actors.runtime import run_actor_lane

                        actor_worker = asyncio.create_task(
                            run_actor_lane(
                                bus.js, config, self.actor_store, self.state["actors"], stopping
                            )
                        )
                    try:
                        while not stopping.is_set():
                            self.project_receipts()
                            message = await bus.fetch(wait_seconds=0.25)
                            if message is not None:
                                await self._deliver(message)
                            self.state["outbox_errors"] = await bus.flush_outbox(self.store)
                            self.state["outbox_quarantined"] = self.store.outbox_quarantine_counts()
                    finally:
                        if actor_worker is not None:
                            actor_worker.cancel()
                            await asyncio.gather(actor_worker, return_exceptions=True)
            except Exception as error:
                detail = type(error).__name__
                was_connected = bool(self.state["connected"])
                is_transport_error = type(error).__module__.startswith("nats.") or isinstance(
                    error, (OSError, TimeoutError)
                )
                is_connected_nats_timeout = (
                    was_connected
                    and type(error).__module__.startswith("nats.")
                    and type(error).__name__ == "TimeoutError"
                )
                now = time.monotonic()
                refresh_reason = (
                    was_connected
                    or self._failure_reason_at is None
                    or now - self._failure_reason_at >= 30
                )
                if is_transport_error and not is_connected_nats_timeout and refresh_reason:
                    # Count the attempt even if diagnosis fails, so a damaged marker or
                    # unavailable manager cannot be polled once per reconnect attempt.
                    self._failure_reason_at = now
                    try:
                        # Keep optional bus imports lazy: core startup still works when the
                        # extra is absent or broken, and that earlier path reports ImportError.
                        from .broker import Broker

                        detail = await Broker(config).failure_reason()
                        self._broker_reason = detail
                    except Exception:
                        self._broker_reason = None
                elif is_transport_error and self._broker_reason is not None:
                    # Inside the window reuse only the reason this branch computed, never a
                    # non-transport error text left by delivery (re-check finding X3).
                    detail = self._broker_reason
                self.state.update(connected=False, last_error=detail)
                try:
                    await asyncio.wait_for(stopping.wait(), 1)
                except TimeoutError:
                    pass
        self.state["connected"] = False
