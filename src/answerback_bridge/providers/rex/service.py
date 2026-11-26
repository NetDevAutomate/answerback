"""Cooperative Rex routing: native observations authorize durable mailbox operations."""

import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ...config import Config, public_task
from ...retention import content_matches
from ...tasks import TaskStore
from .client import (
    Rex,
    RexIdentityChanged,
    _full_id,
    _owned_foreground,
    _owned_inventory,
    resolve_binary,
)
from .mailbox import RexMailbox


class RexHost(Protocol):
    """Only the durable storage/configuration dependencies owned by the host."""

    config: Config
    config_path: Path
    store: TaskStore
    provider_status: dict[str, Any]


class IdentityComparison(Enum):
    """Missing native evidence withholds authority without proving replacement."""

    VERIFIED = "verified"
    CHANGED = "changed"
    INCOMPLETE = "incomplete"


class RexService:
    def __init__(self, bridge: RexHost) -> None:
        self.bridge = bridge
        self.mailbox = RexMailbox(bridge.store)
        self.adapter = Rex(
            bridge.config.rex_binary, bridge.config.rex_server, bridge.config.rex_host_id
        )
        self.adapter_binding = (
            bridge.config.rex_binary,
            bridge.config.rex_server,
            bridge.config.rex_host_id,
        )
        self.lock = asyncio.Lock()
        self.waiters: dict[str, asyncio.Event] = {}
        self.closed = False

    def enabled(self) -> None:
        if not self.bridge.config.provider_enabled("rex"):
            raise ValueError("Rex is disabled; run rex-configure first")
        if not self.bridge.config.rex_server or not self.bridge.config.rex_host_id:
            raise ValueError("Rex requires a probed binding; run rex-configure first")
        if self.adapter_binding != (
            self.bridge.config.rex_binary,
            self.bridge.config.rex_server,
            self.bridge.config.rex_host_id,
        ):
            raise ValueError("Rex native binding differs from its configuration; run rex-configure")

    def config_binding(self) -> tuple[Any, ...]:
        config = self.bridge.config
        return (
            config.provider_enabled("rex"),
            config.rex_enabled,
            config.rex_binary,
            config.rex_server,
            config.rex_host_id,
        )

    def unchanged(self, adapter: Any, expected: tuple[Any, ...]) -> None:
        self.enabled()
        if self.closed:
            raise ValueError("Bridge is shutting down")
        if self.adapter is not adapter or self.config_binding() != expected:
            raise ValueError("Rex routing configuration changed during native observation")

    @staticmethod
    def eligible(observation: dict[str, Any]) -> dict[str, Any]:
        binding = observation["binding"]
        if (
            not binding.get("server_host_id")
            or binding.get("server_host_id") != binding.get("block_host_id")
            or type(binding.get("foreground_pid")) is not int
            or binding["foreground_pid"] <= 0
            or not binding.get("foreground_name")
            or not isinstance(binding.get("foreground_cwd"), str)
            or not os.path.isabs(binding["foreground_cwd"])
        ):
            raise ValueError("Rex enrollment requires a current local foreground process and cwd")
        if not _owned_foreground(observation.get("foreground")):
            raise ValueError("Rex enrollment requires a foreground process owned by this OS user")
        return binding

    @staticmethod
    def compare_identity(
        binding: dict[str, Any], observation: dict[str, Any]
    ) -> IdentityComparison:
        current = observation["binding"]
        incomplete = False
        for field, saved in binding.items():
            # Optional path/script were never known when saved as None.
            # Learning their values cannot prove a change to a known identity.
            if saved is None:
                continue
            observed = current.get(field)
            if observed is None or observed == "":
                incomplete = True
            elif observed != saved:
                return IdentityComparison.CHANGED
        uid = (observation.get("foreground") or {}).get("user_id")
        if type(uid) is not int:
            incomplete = True
        elif not _owned_foreground(observation.get("foreground")):
            return IdentityComparison.CHANGED
        return IdentityComparison.INCOMPLETE if incomplete else IdentityComparison.VERIFIED

    async def verify(self, peer_id: str) -> dict[str, Any]:
        self.enabled()
        peer = self.mailbox.get(peer_id)
        if peer["state"] != "active":
            raise ValueError("Rex peer is stale or retired; enroll its current terminal explicitly")
        binding = peer["binding"]
        adapter, config_binding = self.adapter, self.config_binding()
        try:
            observation = await adapter.terminal(binding["session_id"], binding["block_id"])
        except RexIdentityChanged as error:
            self.unchanged(adapter, config_binding)
            self.mailbox.invalidate(peer_id, str(error))
            self.notify(peer_id)
            raise
        self.unchanged(adapter, config_binding)
        comparison = self.compare_identity(binding, observation)
        if comparison is IdentityComparison.CHANGED:
            self.mailbox.invalidate(peer_id, "Observed Rex terminal binding changed")
            self.notify(peer_id)
            raise ValueError("Rex terminal binding changed; enrollment invalidated")
        if comparison is IdentityComparison.INCOMPLETE:
            raise ValueError("Rex terminal identity evidence is incomplete; peer is unverified")
        self.eligible(observation)
        return peer

    async def configure(self, binary: str = "rex", server: str | None = None) -> dict[str, Any]:
        config_binding = self.config_binding()
        selected = server if server is not None else self.bridge.config.rex_server
        replace(self.bridge.config, rex_binary=binary, rex_server=selected)
        binary = resolve_binary(binary)
        adapter = Rex(binary, selected)
        observation = await adapter.probe()
        async with self.lock:
            if self.closed:
                raise ValueError("Bridge is shutting down")
            if config_binding != self.config_binding():
                raise ValueError("Rex configuration changed during probe")
            config = self.bridge.config
            # First pin of a legacy path preserves the server-bound enrollments.
            # Replacing an already pinned executable still changes the binding.
            changed = (config.rex_server, config.rex_host_id) != (
                observation["endpoint"],
                observation["server_host_id"],
            ) or (os.path.isabs(config.rex_binary) and config.rex_binary != binary)
            if changed:
                with self.bridge.store.connection() as db:
                    unfinished = db.execute(
                        "SELECT 1 FROM tasks WHERE json_extract(metadata, '$.kind')='rex' "
                        "AND status NOT IN ('completed','failed','abandoned','cancelled') LIMIT 1"
                    ).fetchone()
                if unfinished:
                    raise ValueError("Reconcile unfinished Rex tasks before changing its binding")
            candidate = replace(
                config,
                rex_enabled=True,
                rex_binary=binary,
                rex_server=observation["endpoint"],
                rex_host_id=observation["server_host_id"],
            )
            candidate.select_provider("rex", True)
            candidate.save(self.bridge.config_path)
            if changed:
                for peer in self.mailbox.peers():
                    self.mailbox.invalidate(peer["peer_id"], "Rex configuration changed")
                    self.notify(peer["peer_id"])
            config.__dict__.update(candidate.__dict__)
            self.adapter = adapter
            self.adapter_binding = (config.rex_binary, config.rex_server, config.rex_host_id)
            self.bridge.provider_status["rex"].update(state="connected")
        return {"configured": True, "provider": "rex", **observation, "binary": binary}

    async def read(self, session_id: str, block_id: str, max_bytes: int = 16384) -> dict[str, Any]:
        self.enabled()
        adapter, config_binding = self.adapter, self.config_binding()
        result = await adapter.read(session_id, block_id, max_bytes)
        self.unchanged(adapter, config_binding)
        return result

    async def enroll(
        self, name: str, session_id: str, block_id: str, request_id: str
    ) -> dict[str, Any]:
        self.enabled()
        _full_id(session_id, "session")
        _full_id(block_id, "block")
        async with self.lock:
            adapter, config_binding = self.adapter, self.config_binding()
            observation = await adapter.terminal(session_id, block_id)
            self.unchanged(adapter, config_binding)
            binding = self.eligible(observation)
            if (
                binding["endpoint"] != self.bridge.config.rex_server
                or binding["server_host_id"] != self.bridge.config.rex_host_id
            ):
                raise ValueError("Rex terminal does not match the configured binding")
            return self.mailbox.enroll(name, binding, request_id)

    def previous(self, peer_id: str, message: str, request_id: str) -> dict[str, Any] | None:
        task = self.bridge.store.by_request(request_id)
        if task is None:
            return None
        if (
            task["target"] != peer_id
            or not content_matches(task, "message", message)
            or task["metadata"].get("kind") != "rex"
            or task["metadata"].get("peer_id") != peer_id
        ):
            raise ValueError("request_id already describes different work")
        return public_task(task)

    async def submit(
        self,
        peer_id: str,
        message: str,
        request_id: str,
        *,
        expected: dict[str, Any] | None = None,
        admission_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            previous = self.previous(peer_id, message, request_id)
            if previous is not None:
                return previous
            peer = await self.verify(peer_id)
            if expected is not None and (
                expected.get("peer_id") != peer_id
                or expected.get("binding") != peer["binding"]
                or expected.get("project") != peer["binding"]["foreground_cwd"]
            ):
                raise ValueError("Prepared Rex enrollment binding changed")
            if admission_guard is not None:
                admission_guard()
            task = self.mailbox.submit(peer_id, message, request_id)
            self.notify(peer_id)
            return task

    def notify(self, peer_id: str) -> None:
        if peer_id in self.waiters:
            self.waiters[peer_id].set()

    async def receive(self, peer_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 15:
            raise ValueError("wait_seconds must be an integer between 0 and 15")
        async with asyncio.timeout(25):
            event = asyncio.Event()
            if wait_seconds:
                if len(self.waiters) >= 8 or peer_id in self.waiters:
                    raise ValueError(
                        "Rex receive waiter capacity reached; one waiter per enrollment"
                    )
                self.waiters[peer_id] = event
            try:
                async with self.lock:
                    if self.closed:
                        raise ValueError("Bridge is shutting down")
                    await self.verify(peer_id)
                    inbox = self.mailbox.inbox(peer_id)
                if wait_seconds and not inbox["tasks"] and not inbox["active_task"]:
                    try:
                        await asyncio.wait_for(event.wait(), wait_seconds)
                    except TimeoutError:
                        pass
                    async with self.lock:
                        if self.closed:
                            return {**inbox, "shutdown": True}
                        await self.verify(peer_id)
                        inbox = self.mailbox.inbox(peer_id)
                return inbox
            finally:
                if wait_seconds:
                    self.waiters.pop(peer_id, None)

    async def ack(self, peer_id: str, task_id: str) -> dict[str, Any]:
        async with self.lock:
            await self.verify(peer_id)
            return self.mailbox.acknowledge(peer_id, task_id)

    async def complete(self, peer_id: str, task_id: str, result: str) -> dict[str, Any]:
        async with self.lock:
            task = self.bridge.store.get(task_id)
            if task["status"] != "completed":
                await self.verify(peer_id)
            return self.mailbox.complete(peer_id, task_id, result)

    async def retire(self, peer_id: str) -> dict[str, Any]:
        async with self.lock:
            result = self.mailbox.retire(peer_id)
            self.notify(peer_id)
            return result

    @staticmethod
    def row(peer: dict[str, Any], verified: bool) -> dict[str, Any]:
        return {
            **peer,
            "provider": "rex",
            "target": peer["peer_id"],
            "project": peer["binding"]["foreground_cwd"],
            "kind": "cooperative",
            "verified": verified,
            "capabilities": ["queue", "ack", "result"] if verified else [],
        }

    async def peer(self, peer_id: str) -> dict[str, Any]:
        async with self.lock:
            return self.row(await self.verify(peer_id), True)

    async def inventory(self) -> dict[str, Any]:
        self.enabled()
        # Bind this observation to the registrations that existed before it began.
        # Native collection stays outside the lock so a stalled provider cannot
        # block retire/enroll/submit or consume their caller deadlines.
        async with self.lock:
            adapter = self.adapter
            snapshot = {peer["peer_id"]: peer for peer in self.mailbox.peers()}
            config_binding = self.config_binding()
        native: dict[str, Any] = await adapter.inventory()
        observations = {
            (p["binding"]["session_id"], p["binding"]["block_id"]): p for p in native["peers"]
        }
        rows = []
        hidden = set()
        async with self.lock:
            same_config = adapter is self.adapter and config_binding == self.config_binding()
            if not same_config:
                native = {
                    **native,
                    "complete": False,
                    "errors": [
                        *native.get("errors", []),
                        {"error": "Rex native owner or routing changed during discovery"},
                    ],
                }
            for peer in self.mailbox.peers():
                binding = peer["binding"]
                key = (binding["session_id"], binding["block_id"])
                observation = observations.get(key)
                verified = False
                scope = native.get("absence_scope")
                if (
                    same_config
                    and snapshot.get(peer["peer_id"]) == peer
                    and peer["state"] == "active"
                    and observation is None
                    and native.get("complete") is True
                    and not native.get("errors")
                    and isinstance(scope, dict)
                    and all(
                        scope.get(field) == binding[field]
                        for field in ("endpoint", "server_host_id", "server_instance_id")
                    )
                ):
                    self.mailbox.invalidate(peer["peer_id"], "Rex terminal confirmed absent")
                    self.notify(peer["peer_id"])
                    peer = self.mailbox.get(peer["peer_id"])
                if (
                    same_config
                    and snapshot.get(peer["peer_id"]) == peer
                    and observation is not None
                    and peer["state"] == "active"
                ):
                    comparison = self.compare_identity(binding, observation)
                    if comparison is IdentityComparison.CHANGED:
                        self.mailbox.invalidate(
                            peer["peer_id"], "Observed Rex terminal binding changed"
                        )
                        self.notify(peer["peer_id"])
                        peer = self.mailbox.get(peer["peer_id"])
                    else:
                        verified = comparison is IdentityComparison.VERIFIED
                        hidden.add(key)
                        if not verified:
                            native = {
                                **native,
                                "complete": False,
                                "errors": [
                                    *native.get("errors", []),
                                    {
                                        "peer_id": peer["peer_id"],
                                        "error": "Rex terminal identity evidence is incomplete",
                                    },
                                ],
                            }
                rows.append(self.row(peer, verified))
        # Reconcile with all native observations first: a foreign uid proves a
        # changed binding, whereas missing ownership evidence is only incomplete.
        native = _owned_inventory(
            {
                **native,
                "peers": [
                    p
                    for p in native["peers"]
                    if (p["binding"]["session_id"], p["binding"]["block_id"]) not in hidden
                ],
            }
        )
        rows.extend(
            {
                **p,
                "target": p["peer_id"],
                "name": p.get("display_name") or p.get("label") or p["peer_id"],
                "kind": "unenrolled_terminal",
                "capabilities": [
                    capability
                    for capability in ("read", "enroll")
                    if p.get("capabilities", {}).get(capability) is True
                ],
            }
            for p in native["peers"]
        )
        return {**native, "peers": rows}

    async def close(self) -> None:
        self.closed = True
        for event in self.waiters.values():
            event.set()
