"""MCP connection leases. Liveness never establishes agent readiness or delivery."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from .config import CLIENT_NAMES, Config
from .process import BridgeError
from .transport import Operations

MAX_LEASES_PER_CLIENT = 64
MAX_LEASES = 512


class ClientRegistry:
    def __init__(self, config: Config, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.leases: dict[tuple[str, str], float] = {}

    def _key(self, client: str, instance_id: str) -> tuple[str, str]:
        if client not in CLIENT_NAMES:
            raise ValueError("Unknown MCP client")
        if not isinstance(instance_id, str):
            raise ValueError("Client instance ID must be a UUID")
        return client, str(UUID(instance_id))

    def policy(self) -> dict[str, Any]:
        return {
            "heartbeat_interval": self.config.heartbeat_interval,
            "heartbeat_misses": self.config.heartbeat_misses,
        }

    def enroll(self, client: str) -> dict[str, Any]:
        if client not in CLIENT_NAMES:
            raise ValueError("Unknown MCP client")
        if client not in self.config.client_registrations:
            self.config.client_registrations = [*self.config.client_registrations, client]
        return {"registered": True}

    def register(self, client: str, instance_id: str) -> dict[str, Any]:
        key = self._key(client, instance_id)
        # The global ceiling bounds this scan; expired leases must not exhaust a client.
        held_by_client = sum(name == client for name, _ in self.leases)
        if held_by_client >= MAX_LEASES_PER_CLIENT or len(self.leases) >= MAX_LEASES:
            cutoff = self.clock() - self.config.heartbeat_interval * self.config.heartbeat_misses
            self.leases = {k: seen for k, seen in self.leases.items() if seen > cutoff}
            if key not in self.leases:
                if sum(name == client for name, _ in self.leases) >= MAX_LEASES_PER_CLIENT:
                    raise BridgeError("MCP client connection limit reached for this client")
                if len(self.leases) >= MAX_LEASES:
                    raise BridgeError("MCP client connection limit reached")
        self.enroll(client)
        self.leases[key] = self.clock()
        return {"registered": True, **self.policy()}

    def heartbeat(self, client: str, instance_id: str) -> dict[str, Any]:
        key = self._key(client, instance_id)
        if key not in self.leases:
            raise BridgeError("Client lease unknown; register again")
        self.leases[key] = self.clock()
        return {"available": True, **self.policy()}

    def require_live(self, client: str, instance_id: str) -> None:
        seen = self.leases.get(self._key(client, instance_id))
        if seen is None or self.clock() - seen >= (
            self.config.heartbeat_interval * self.config.heartbeat_misses
        ):
            raise BridgeError("Client lease unavailable; register or heartbeat first")

    def disconnect(self, client: str, instance_id: str) -> dict[str, Any]:
        self.leases.pop(self._key(client, instance_id), None)
        return {"disconnected": True}

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        timeout = self.config.heartbeat_interval * self.config.heartbeat_misses
        clients = []
        for client in self.config.client_registrations:
            ages = [
                max(0.0, now - seen) for (name, _), seen in self.leases.items() if name == client
            ]
            live = sum(age < timeout for age in ages)
            clients.append(
                {
                    "client": client,
                    "registered": True,
                    "available": live > 0,
                    "live_instances": live,
                    "last_heartbeat_age": round(min(ages), 3) if ages else None,
                }
            )
        return {"clients": clients, **self.policy()}


@asynccontextmanager
async def client_presence(
    bridge: Operations,
    client: str,
    *,
    instance_id: str | None = None,
    inbox: dict[str, Any] | None = None,
) -> AsyncIterator[None]:
    """Renew independently of tool calls; reconnect after a bridge restart."""
    params = {"client": client, "instance_id": instance_id or str(uuid4())}
    interval = 10
    ready = asyncio.Event()

    async def update(method: str) -> dict[str, Any]:
        async with asyncio.timeout(2):
            return await bridge.call(method, params)

    async def renew() -> None:
        nonlocal interval
        registered = False
        while True:
            try:
                response = await update("client_heartbeat" if registered else "client_register")
                interval = int(response["heartbeat_interval"])
                registered = True
                ready.set()
            except (BridgeError, OSError, ValueError, KeyError):
                registered = False
                ready.clear()
            await asyncio.sleep(interval if registered else min(interval, 2))

    async def poll() -> None:
        assert inbox is not None
        while True:
            await ready.wait()
            try:
                inbox.update(await update("client_poll"))
            except (BridgeError, OSError, ValueError):
                pass
            await asyncio.sleep(interval)

    tasks = [asyncio.create_task(renew())]
    if inbox is not None:
        tasks.append(asyncio.create_task(poll()))
    try:
        try:
            async with asyncio.timeout(2.1):
                await ready.wait()
        except TimeoutError:
            pass  # Keep MCP usable while the bounded background reconnect loop retries.
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await update("client_disconnect")
        except (BridgeError, OSError, ValueError):
            pass
