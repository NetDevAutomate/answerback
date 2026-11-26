"""Shared storage/lifecycle defaults; native interpretation belongs to each adapter."""

import asyncio
from pathlib import Path

from ..config import Config
from ..tasks import TaskStore
from .catalog import definition
from .contracts import DiscoveryPage, Peer, Submission, TaskControls, TaskReceipt


async def validate_project(observed: str, expected: str | None) -> None:
    if expected is None:
        return
    if not isinstance(observed, str) or not observed:
        raise ValueError("Recipient project is unknown")

    def matches() -> bool:
        return Path(observed).resolve() == Path(expected).resolve()

    if not await asyncio.to_thread(matches):
        raise ValueError("Recipient project changed after preparation")


class StoredAdapter:
    name: str
    registration: str
    aliases: tuple[str, ...] = ()
    controls: TaskControls | None = None

    def __init__(self, config: Config, config_path: Path, store: TaskStore) -> None:
        self.config, self.config_path, self.store = config, config_path, store

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    def require_enabled(self) -> None:
        if not self.enabled():
            raise ValueError(f"{self.registration} provider is not registered")

    @property
    def recovery_states(self) -> tuple[str, ...]:
        policy = definition(self.name)
        return policy.recovery_states if policy else ()

    async def discover(self) -> DiscoveryPage:
        return DiscoveryPage()

    async def resolve_exact(self, selector: str) -> Peer | None:
        return None

    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None:
        return None

    async def observe(self, receipt: TaskReceipt, *, refresh: bool) -> TaskReceipt:
        return TaskReceipt.from_record(self.store.get(receipt.task_id))

    def poll_ids(self, limit: int) -> tuple[str, ...]:
        return ()

    async def close(self) -> None:
        """This adapter owns no persistent execution resource by default."""
