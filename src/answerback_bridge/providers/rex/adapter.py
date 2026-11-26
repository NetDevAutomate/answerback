"""Explicit enrolled Rex peers; cooperative results never use worker-report controls."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from ...adapters.base import StoredAdapter
from ...adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ...config import Config
from ...plugin import KernelContext
from ...retention import content_matches
from .client import _owned_inventory
from .service import RexService


class RexDiscovery:
    name = registration = "rex"
    aliases = ()

    def __init__(self, config: Config, native: Callable[[], Any]) -> None:
        self.config, self._native = config, native

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        if not selector.startswith("rex:") or selector.startswith("rex:terminal:"):
            return None
        try:
            UUID(selector.removeprefix("rex:"))
        except ValueError:
            return None
        if not self.enabled():
            raise ValueError("Rex provider is not registered")
        native = self._native()
        if not isinstance(native, RexService):
            return None
        return Peer.from_record(await native.peer(selector))

    async def discover(self) -> DiscoveryPage:
        if not self.enabled():
            return DiscoveryPage(errors=(("rex", "Rex is not registered; use rex-configure"),))
        provider = self._native()
        native = await provider.inventory()
        if not isinstance(provider, RexService):
            native = _owned_inventory(native)
        errors = []
        for index, diagnostic in enumerate(native.get("errors", [])):
            text = (
                diagnostic.get("error", str(diagnostic))
                if isinstance(diagnostic, dict)
                else str(diagnostic)
            )
            errors.append((f"rex:{index}", text))
        if native.get("complete") is not True and not errors:
            errors.append(("rex", "Rex inventory coverage is incomplete"))
        rows = []
        for row in native["peers"]:
            capabilities = row.get("capabilities", [])
            if isinstance(capabilities, dict):
                capabilities = [
                    name for name in ("read", "enroll") if capabilities.get(name) is True
                ]
            rows.append(
                Peer.from_record(
                    {
                        **row,
                        "provider": self.name,
                        "target": row.get("target", row["peer_id"]),
                        "project": row.get("project") or "",
                        "kind": row.get("kind", "unenrolled_terminal"),
                        "capabilities": capabilities,
                    }
                )
            )
        return DiscoveryPage(
            peers=tuple(rows),
            errors=tuple(errors),
        )


class RexAdapter(RexDiscovery, StoredAdapter):
    controls = None

    def __init__(self, ctx: KernelContext, cooperative: Callable[[], RexService]) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        RexDiscovery.__init__(self, ctx.config, cooperative)

    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None:
        row = self.store.by_request(submission.request_id)
        if row is None:
            return None
        if (
            row["target"] != selector
            or not content_matches(row, "message", submission.message)
            or row["metadata"].get("kind") != self.name
            or row["metadata"].get("peer_id") != selector
        ):
            raise ValueError("request_id already describes different work")
        return TaskReceipt.from_record(row)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        if peer.kind != "cooperative" or not peer.peer_id.startswith("rex:"):
            raise ValueError("Rex terminal must be explicitly enrolled before queue admission")
        current = Peer.from_record(await self._native().peer(peer.peer_id))
        if current.as_record()["binding"] != peer.as_record().get("binding"):
            raise ValueError("Rex enrollment changed after selection")
        return PreparedPeer(current.peer_id, current)

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        self.require_enabled()
        row = await self._native().submit(
            prepared.peer.peer_id,
            submission.message,
            submission.request_id,
            expected=prepared.peer.as_record(),
            admission_guard=guard,
            subject=submission.subject,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return (
            receipt.provider == self.name
            and receipt.target == peer.peer_id
            and receipt.as_record()["metadata"].get("peer_id") == peer.peer_id
        )

    async def close(self) -> None:
        native = self._native()
        await native.close()
