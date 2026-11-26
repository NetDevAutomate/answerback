"""Local durable acceptance is available even while the broker/coordinator is offline."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapters.catalog import definition
from ..config import Config, public_task
from ..tasks import TaskStore
from .config import BusConfig, routing_digest
from .store import BusStore

if TYPE_CHECKING:
    from ..pressure import Pressure, PressureStore

# Providers whose peers are terminals running any harness; the ID does not say which.
TERMINAL_HOSTS = ("hc", "herdr")


class BusAPI:
    def __init__(
        self,
        config_path: Path,
        *,
        pressure: "Callable[[], PressureStore | None] | None" = None,
    ) -> None:
        self.config_path = config_path.expanduser().resolve()
        # A getter, like the service's adapter owners: the store is built after this API.
        self.pressure = pressure

    def hold(self, peer_id: str) -> "Pressure | None":
        """The provider pressure a command to `peer_id` waits behind, or None.

        A native or Kiro peer ID names its harness (`kiro:`, `claude_code:`, ...). A
        terminal host's ID (`hc:`, `herdr:`) does not, and acceptance must not depend on a
        live discovery, so such a command waits behind the held provider whose pause ends
        last. Any other provider (`rex:`, a plugin's, ...) is not held.
        """
        store = self.pressure() if self.pressure is not None else None
        if store is None:
            return None
        from ..pressure import KNOWN_PROVIDERS, harness_of_peer, provider_of
        from ..pressure.gate import held

        prefix = peer_id.partition(":")[0]
        if prefix not in TERMINAL_HOSTS:
            return held(store, provider_of(harness_of_peer({"provider": prefix})))
        latest: Pressure | None = None
        for name in KNOWN_PROVIDERS:
            pressure = held(store, name)
            if pressure is not None and (
                latest is None or (pressure.until_wall or 0.0) > (latest.until_wall or 0.0)
            ):
                latest = pressure
        return latest

    def store(self) -> BusStore:
        config = BusConfig.load(self.config_path)
        return BusStore(config.root / "ledger.sqlite")

    def submit(
        self,
        peer_id: str,
        project: str,
        message: str,
        request_id: str,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        config = Config.load(self.config_path)
        if not isinstance(peer_id, str):
            raise ValueError("Use a canonical peer ID")
        provider = definition(peer_id.partition(":")[0])
        if provider is None or not config.provider_enabled(provider.registration):
            raise ValueError("Recipient provider is not registered")
        # Under provider pressure the command is still accepted and stays `queued`; its
        # outbox row is published only after the pause ends (`outbox_holds`).
        held = self.hold(peer_id)
        return self.store().submit(
            peer_id=peer_id,
            project=project,
            message=message,
            request_id=request_id,
            ttl_seconds=ttl_seconds,
            routing_digest=routing_digest(config),
            not_before=held.until_wall if held is not None else None,
            hold_reason=(
                f"provider_backoff:{held.provider}:{held.account_hint}:level {held.level}"
                if held is not None
                else "provider_backoff"
            ),
        )

    def result(self, command_id: str) -> dict[str, Any]:
        row = self.store().get(command_id)
        task = None
        if row["task_id"] is not None:
            task = public_task(
                TaskStore(self.config_path.parent / "tasks.sqlite").get(row["task_id"])
            )
        return {**row, "task": task}

    def events(self, after: int = 0, limit: int = 50) -> dict[str, Any]:
        return self.store().events(after=after, limit=limit)

    def cancel(self, command_id: str) -> dict[str, Any]:
        return self.store().cancel(command_id)

    def status(self) -> dict[str, Any]:
        if not (self.config_path.parent / "bus/config.json").exists():
            return {"configured": False}
        config = BusConfig.load(self.config_path)
        store = self.store()
        with store.connection() as db:
            pending = db.execute(
                "SELECT count(*) FROM outbox WHERE published_at IS NULL"
            ).fetchone()[0]
        return {
            "configured": True,
            **config.public(),
            "commands": store.counts(),
            "outbox_pending": pending,
        }
