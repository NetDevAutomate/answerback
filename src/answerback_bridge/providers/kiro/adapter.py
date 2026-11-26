"""Managed Kiro provider. ACP identity/readiness remains in the worker owner."""

from collections.abc import Callable
from typing import Any

from ...adapters.base import StoredAdapter
from ...adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ...config import Config
from ...plugin import KernelContext
from .workers import Workers


class KiroDiscovery:
    name = "kiro"
    registration = "kiro"
    aliases = ()

    def __init__(self, config: Config, native: Callable[[], Any]) -> None:
        self.config, self._native = config, native

    @property
    def unused(self) -> Any:
        return self._native()

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        return None

    async def discover(self) -> DiscoveryPage:
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        cursors: dict[str, str] = {}
        excluded: dict[str, str] = {}
        if self.config.provider_enabled("kiro"):
            peers.extend(
                (
                    {
                        "peer_id": f"kiro:{alias}",
                        "provider": "kiro",
                        "target": alias,
                        "name": alias,
                        "project": cwd,
                        "kind": "bridge_managed",
                        "capabilities": ["send", "result", "permissions", "cancel"],
                    }
                    for alias, cwd in self.config.kiro_workspaces.items()
                )
            )
        return DiscoveryPage.from_record(
            {"peers": peers, "errors": errors, "cursors": cursors, "excluded": excluded}
        )


class KiroAdapter(KiroDiscovery, StoredAdapter):
    name = registration = "kiro"

    def __init__(self, ctx: KernelContext, workers: Callable[[], Workers]) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        KiroDiscovery.__init__(self, ctx.config, lambda: None)
        self._workers = workers

    @property
    def controls(self) -> Workers:
        return self._workers()

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        return PreparedPeer(f"kiro:{peer.target}:{peer.project}", peer)

    async def submit_alias(
        self,
        alias: str,
        message: str,
        request_id: str,
        *,
        guard: Guard,
    ) -> TaskReceipt:
        cwd = self.config.kiro_workspaces.get(alias)
        if cwd is None:
            raise ValueError("Unknown Kiro alias; configure a named workspace first")
        peer = Peer.from_record(
            {
                "peer_id": f"kiro:{alias}",
                "provider": self.name,
                "target": alias,
                "project": cwd,
            }
        )
        prepared = await self.prepare(peer)
        return await self.submit(prepared, Submission(message, request_id), guard=guard)

    async def submit(
        self,
        prepared: PreparedPeer,
        submission: Submission,
        *,
        guard: Guard,
    ) -> TaskReceipt:
        self.require_enabled()
        guard()
        row = await self.controls.submit(
            prepared.peer.target,
            submission.message,
            submission.request_id,
            expected_cwd=prepared.peer.project,
            admission_guard=guard,
            subject=submission.subject,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return receipt.provider == self.name and receipt.target == (
            f"kiro:{peer.target}:{peer.project}"
        )

    async def close(self) -> None:
        await self.controls.close()
