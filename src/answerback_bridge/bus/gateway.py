"""Typed adapter registry bridge for durable commands; no native provider decisions."""

from typing import TYPE_CHECKING

from ..adapters.catalog import definition
from ..adapters.contracts import Peer, PreparedPeer, Submission, TaskReceipt
from ..retention import content_matches
from .config import routing_digest
from .model import Command

if TYPE_CHECKING:
    from ..service import Bridge


class BridgeGateway:
    def __init__(self, bridge: "Bridge") -> None:
        self.bridge = bridge

    def guard(self, command: Command) -> None:
        if routing_digest(self.bridge.config) != command.routing_digest:
            raise ValueError("Routing configuration changed; discover and authorise a new command")

    async def prepare(self, command: Command) -> PreparedPeer:
        self.guard(command)
        peer = Peer.from_record(await self.bridge.directory().resolve(command.peer_id))
        if peer.peer_id != command.peer_id:
            raise ValueError("Bus delivery requires an exact discovered peer ID")
        target = await self.bridge.router.prepare(peer, expected_project=command.project)
        self.guard(command)
        return target

    async def send(self, target: PreparedPeer, command: Command) -> TaskReceipt:
        return await self.bridge.router.submit(
            target,
            Submission(command.message, command.adapter_request_id),
            before_admission=lambda: self.guard(command),
        )

    def lookup(self, command: Command) -> TaskReceipt | None:
        task = self.bridge.store.by_request(command.adapter_request_id)
        provider = definition(command.peer_id.partition(":")[0])
        if task is not None and (
            provider is None
            or task["metadata"].get("kind") != provider.name
            or not task["target"].startswith(provider.name + ":")
            or task["request_key"] != command.adapter_request_id
            or not content_matches(task, "message", command.message)
        ):
            raise ValueError("Adapter request key belongs to different work")
        return TaskReceipt.from_record(task) if task else None

    def get(self, task_id: str) -> TaskReceipt:
        return TaskReceipt.from_record(self.bridge.store.get(task_id))
