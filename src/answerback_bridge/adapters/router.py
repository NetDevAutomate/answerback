"""Generic admission and observation, independent of native provider names."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from ..retention import content_matches
from ..subjects import admission_subject, keep_after_cancel
from ..tasks import TaskStore
from .contracts import Peer, PreparedPeer, Submission, TaskReceipt
from .registry import AdapterRegistry


def same_subject(receipt: TaskReceipt, submission: Submission) -> bool:
    """The receipt holds exactly the submission's subject (both None counts as the same)."""
    return receipt.as_record().get("subject") == submission.subject


class AdapterRouter:
    def __init__(self, registry: AdapterRegistry, store: TaskStore) -> None:
        self.registry, self.store = registry, store

    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None:
        adapter = self.registry.get(selector.partition(":")[0])
        returned = adapter.previous(selector, submission)
        if returned is None:
            return None
        persisted = TaskReceipt.from_record(self.store.get(returned.task_id))
        if persisted.request_id == submission.request_id and not same_subject(
            persisted, submission
        ):
            raise ValueError("request_id already describes different work")
        if (
            persisted.request_id != submission.request_id
            or persisted.provider != adapter.name
            or not content_matches(persisted.as_record(), "message", submission.message)
            or returned.target != persisted.target
            or returned.request_id != persisted.request_id
        ):
            raise ValueError("Adapter returned an uncorrelated previous receipt")
        return persisted

    async def prepare(self, peer: Peer, *, expected_project: str | None = None) -> PreparedPeer:
        adapter = self.registry.for_peer(peer)
        if not adapter.enabled():
            raise ValueError("Recipient provider is not registered")
        prepared = await adapter.prepare(peer)
        if prepared.peer.peer_id != peer.peer_id or prepared.peer.provider != peer.provider:
            raise ValueError("Adapter changed the selected recipient")
        if not prepared.target_key:
            raise ValueError("Adapter did not bind a target identity")
        if expected_project is not None:

            def same_project() -> bool:
                return bool(prepared.peer.project) and (
                    Path(prepared.peer.project).resolve() == Path(expected_project).resolve()
                )

            if not await asyncio.to_thread(same_project):
                raise ValueError("Recipient project changed or is unknown")
        return prepared

    async def submit(
        self,
        prepared: PreparedPeer,
        submission: Submission,
        *,
        before_admission: Callable[[], None] | None = None,
    ) -> TaskReceipt:
        adapter = self.registry.for_peer(prepared.peer)

        def guard() -> None:
            if not adapter.enabled():
                raise ValueError("Recipient provider is not registered")
            if before_admission is not None:
                before_admission()

        guard()
        # A receipt that exists already is a repeat: one the adapter returns with another
        # subject is different work, not a reservation that lost its subject.
        prior = self.store.by_request(submission.request_id)
        # Adapters reserve without naming a subject; the store takes it from this
        # registration for this request ID only (`subjects.admission_subject`).
        with admission_subject(self.store.path, submission.request_id, submission.subject):
            try:
                returned = await adapter.submit(prepared, submission, guard=guard)
            except asyncio.CancelledError:
                # Cancelling the await does not stop an adapter's thread or executor job;
                # unless it has reserved already, it still reserves with this subject.
                try:
                    reserved = self.store.by_request(submission.request_id) is not None
                except Exception:  # noqa: BLE001 - keep the subject when unsure
                    reserved = False
                if not reserved:
                    keep_after_cancel(self.store.path, submission.request_id)
                raise
        try:
            record = self.store.get(returned.task_id)
        except ValueError:
            # The adapter reserved outside this store, where no subject was registered.
            raise ValueError(
                "Adapter reserved the receipt in another store, without its subject; "
                "inspect it before sending again"
                if submission.subject is not None
                else "Adapter returned an uncorrelated task receipt; inspect admission"
            ) from None
        persisted = TaskReceipt.from_record(record)
        if persisted.request_id == submission.request_id and not same_subject(
            persisted, submission
        ):
            raise ValueError(
                "Adapter reserved the receipt without its subject; the receipt exists "
                "without one: inspect it before sending again"
                if persisted.as_record().get("subject") is None
                and (prior is None or prior["id"] != persisted.task_id)
                else "request_id already describes different work"
            )
        if (
            persisted.target != prepared.target_key
            or persisted.request_id != submission.request_id
            or persisted.provider != prepared.peer.provider
            or not content_matches(persisted.as_record(), "message", submission.message)
            or returned.target != persisted.target
            or returned.request_id != persisted.request_id
        ):
            raise ValueError("Adapter returned an uncorrelated task receipt; inspect admission")
        return persisted

    async def observe(self, task_id: str, *, refresh: bool) -> TaskReceipt:
        before = TaskReceipt.from_record(self.store.get(task_id))
        # Old/unowned history remains readable, but cannot invoke an unknown adapter.
        if not self.registry.contains(before.provider):
            return before
        adapter = self.registry.get(before.provider)
        if not adapter.enabled():
            return TaskReceipt.from_record({**before.as_record(), "provider_disabled": True})
        returned = await adapter.observe(before, refresh=refresh)
        current = TaskReceipt.from_record(self.store.get(task_id))
        if (current.task_id, current.target, current.request_id, current.provider) != (
            before.task_id,
            before.target,
            before.request_id,
            before.provider,
        ):
            raise ValueError("Adapter observation changed persisted task identity")
        if (returned.task_id, returned.target, returned.request_id) != (
            current.task_id,
            current.target,
            current.request_id,
        ):
            raise ValueError("Adapter observation belongs to another task")
        record = current.as_record()
        if returned.as_record().get("provider_disabled") is True:
            record["provider_disabled"] = True
        return TaskReceipt.from_record(record)
