"""Provider-neutral discovery and routing for owned CLI workers."""

from typing import Any

from ...adapters.base import StoredAdapter
from ...adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ...config import NATIVE_PROVIDERS, Config
from ...plugin import KernelContext
from .workers import NativeWorkers


class NativeDiscovery:
    aliases: tuple[str, ...] = ()

    def __init__(self, provider: str, config: Config) -> None:
        if provider not in NATIVE_PROVIDERS:
            raise ValueError("Unknown native CLI provider")
        self.name = self.registration = provider
        self.config = config

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        return None

    async def discover(self) -> DiscoveryPage:
        peers: list[dict[str, Any]] = []
        if self.enabled():
            peers = [
                {
                    "peer_id": f"{self.name}:{alias}",
                    "provider": self.name,
                    "target": alias,
                    "name": alias,
                    "project": cwd,
                    "kind": "bridge_managed",
                    "capabilities": ["send", "result", "permissions", "cancel"],
                }
                for alias, cwd in getattr(self.config, f"{self.name}_workspaces").items()
            ]
        return DiscoveryPage.from_record({"peers": peers})


class NativeAdapter(NativeDiscovery, StoredAdapter):
    controls: NativeWorkers

    def __init__(self, provider: str, ctx: KernelContext) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        NativeDiscovery.__init__(self, provider, ctx.config)
        self.controls = ctx.shared.native_workers(provider, ctx.config, ctx.config_path, ctx.store)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        if getattr(self.config, f"{self.name}_workspaces").get(peer.target) != peer.project:
            raise ValueError("Native workspace changed after discovery")
        return PreparedPeer(f"{self.name}:{peer.target}:{peer.project}", peer)

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        self.require_enabled()
        guard()
        row = await self.controls.submit(
            prepared.peer.target,
            submission.message,
            submission.request_id,
            expected_cwd=prepared.peer.project,
            admission_guard=guard,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return receipt.provider == self.name and receipt.target == (
            f"{self.name}:{peer.target}:{peer.project}"
        )

    async def close(self) -> None:
        await self.controls.close()
