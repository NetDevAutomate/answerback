"""Crew provider discovery and execution implementation."""

import asyncio
import shlex
from collections.abc import Callable
from typing import Any

from ...adapters.base import StoredAdapter
from ...adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ...config import Config, public_task
from ...plugin import KernelContext
from ...process import BridgeError, ProviderOffline, module_launch_argv


class CrewDiscovery:
    name = "crew"
    registration = "crew"
    aliases = ()

    def __init__(self, config: Config, native: Callable[[], Any]) -> None:
        self.config, self._native = config, native

    @property
    def crew(self) -> Any:
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
        if self.config.provider_enabled("crew"):
            try:
                peers.extend(await self.crew.peers())
            except ProviderOffline as error:
                excluded["crew"] = str(error)
            except (BridgeError, OSError, ValueError) as error:
                errors["crew"] = str(error)
        return DiscoveryPage.from_record(
            {"peers": peers, "errors": errors, "cursors": cursors, "excluded": excluded}
        )


class CrewAdapter(CrewDiscovery, StoredAdapter):
    def __init__(self, ctx: KernelContext, native: Callable[[], Any]) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        CrewDiscovery.__init__(self, ctx.config, native)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        return PreparedPeer(f"crew:{self.crew.origin}:{peer.target}", peer)

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        self.require_enabled()
        guard()
        row = await self.submit_native(
            prepared.peer.target,
            submission.message,
            submission.request_id,
            prepared.peer.as_record(),
            admission_guard=guard,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        meta = receipt.as_record().get("metadata", {})
        binding = peer.as_record().get("binding")
        stored_peer = meta.get("peer")
        return (
            receipt.provider == self.name
            and peer.target in (meta.get("target"), meta.get("slot"), meta.get("alias"))
            and isinstance(binding, str)
            and bool(binding)
            and isinstance(stored_peer, dict)
            and stored_peer.get("binding") == binding
        )

    async def submit_native(
        self,
        target: str,
        message: str,
        request_id: str,
        expected: dict[str, Any],
        *,
        admission_guard: Guard | None = None,
    ) -> dict[str, Any]:
        from .client import CrewIdentityChanged, check_identity

        # The annotation forbids None, but a caller can still pass it at runtime;
        # refuse it here, before any lookup, reservation or send. The live peer is
        # never substituted for a missing identity: the row and the delivery carry
        # exactly what the caller discovered.
        if expected is None:
            raise ValueError("Selected Crew identity is missing; search again")
        current = await self.crew.peer(target)
        # Compare the caller's identity with the live chat before admission and
        # reservation, using the comparison Crew.submit runs, as the Herdr adapter
        # compares its terminal: {}, another slot's identity, a record missing
        # created/project/agent or another gateway's origin is refused here, so no
        # receipt row is left for a delivery that never happened. Crew.submit still
        # compares again before and after its POST, for a change in between.
        try:
            check_identity(target, expected, current)
        except CrewIdentityChanged:
            raise ValueError(
                "Selected Crew identity changed or is unavailable; search again"
            ) from None
        peer = expected
        if admission_guard is not None:
            admission_guard()
        task, created = self.store.reserve(
            f"crew:{self.crew.origin}:{target}",
            message,
            request_id,
            {"kind": "crew", "slot": target, "peer": peer, "completion": "worker_report"},
        )
        if not created:
            return public_task(task)
        command = shlex.join(
            module_launch_argv(self.config_path, "complete", task["id"], "--result-file")
        )
        envelope = (
            f"{message}\n\nBridge task: {task['id']}\n"
            "When this task is complete, write your final result to a UTF-8 file and run:\n"
            f"{command} /absolute/path/to/result.txt\n"
            "Alternatively call the bridge task_complete MCP tool with this task ID. "
            "Do not claim completion while awaiting human approval. "
            "If the bridge command is unavailable, say so; do not fabricate a receipt."
        )
        try:
            receipt = await self.crew.submit(target, envelope, task["id"], expected=peer)
        except asyncio.CancelledError:
            self.store.update(task["id"], "uncertain", detail="Crew submission was interrupted")
            raise
        except CrewIdentityChanged as error:
            return public_task(self.store.invalidate_submission(task["id"], str(error)))
        except (BridgeError, OSError, ValueError) as error:
            return public_task(self.store.update(task["id"], "uncertain", detail=str(error)))
        self.store.metadata(task["id"], {"delivery": receipt, "send_id": task["id"]})
        return public_task(
            self.store.update(task["id"], "queued" if receipt.get("queued") else "submitted")
        )
