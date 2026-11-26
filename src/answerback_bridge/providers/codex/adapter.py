"""Codex provider: exact chat identity and persisted native turn evidence."""

import asyncio
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from ... import receipt_version
from ...adapters.base import StoredAdapter, validate_project
from ...adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ...config import Config, public_task
from ...plugin import KernelContext, StoreHooks
from ...process import BridgeError
from ...retention import matches_work
from ...tasks import TERMINAL, Task, TaskStore
from . import history
from .desktop import DesktopIndex

# A receipt whose submission text appeared in two turns of one history window. The
# original and the copy cannot be told apart, so the receipt never binds a turn by
# itself; the operator ends it (`abandon`) and resubmits if the work is still wanted.
AMBIGUOUS_TURN_BINDING = "ambiguous"
AMBIGUOUS_TURN_DETAIL = (
    "result ambiguous: two turns match this submission; the receipt will not auto-bind. "
    "Read the thread, then abandon this task by id and resubmit if the work is still wanted."
)


def is_reconcilable_desktop_failure(task: Task) -> bool:
    """Only the old history-classification failures have this recovery path."""
    return (
        task["status"] == "failed"
        and task["result"] in ("Codex turn interrupted", "Codex turn failed")
        and isinstance(task["turn_id"], str)
        and bool(task["turn_id"])
        and task["metadata"].get("kind") == "desktop"
        and not task["metadata"].get("identity_invalidated")
    )


class DesktopStoreHooks(StoreHooks):
    """The Codex plugin's store policy: failed history receipts, corrected on an exact turn."""

    def is_reconcilable(self, task: Task) -> bool:
        return is_reconcilable_desktop_failure(task)

    def reconcile(
        self, store: TaskStore, task: Task, *, result: str, evidence: Mapping[str, Any]
    ) -> Task:
        """Atomically correct one verified late completion, retaining its old verdict.

        ``evidence["completed_at"]`` is the verified turn's completion time.
        """
        expected = task
        if not is_reconcilable_desktop_failure(expected):
            raise ValueError("Reconciliation requires a failed Desktop history receipt")
        if not result or len(result) > 200_000:
            raise ValueError("Completion requires a result of at most 200000 characters")
        if "completed_at" not in evidence:
            raise ValueError("Reconciliation requires the verified turn's completion time")
        completed_at = evidence["completed_at"]
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (expected["id"],)).fetchone()
            if row is None or store.decode(row, db) != expected:
                raise ValueError("Receipt changed during reconciliation; inspect it again")
            metadata = dict(expected["metadata"])
            now = time.time()
            metadata["desktop_reconciliation"] = {
                "reason": "exact_turn_completed_after_history_failure",
                "previous_status": expected["status"],
                "previous_result": expected["result"],
                "previous_detail": expected["detail"],
                "previous_updated": expected["updated"],
                "turn_id": expected["turn_id"],
                "completed_at": completed_at,
                "reconciled_at": now,
            }
            metadata = receipt_version.stamp_ended(metadata)
            db.execute(
                "UPDATE tasks SET status='completed', result=?, detail=NULL, metadata=?, "
                "updated=? WHERE id=?",
                (result, json.dumps(metadata), now, expected["id"]),
            )
        return store.get(expected["id"])


class CodexDiscovery:
    name = "desktop"
    registration = "codex"
    aliases = ("codex",)

    def __init__(
        self,
        config: Config,
        native: Callable[[], Any],
        owned_cli: Callable[[str], bool] | None = None,
    ) -> None:
        self.config, self._native = config, native
        self._owned_cli = owned_cli or (lambda _: False)

    @property
    def desktop(self) -> Any:
        return self._native()

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        if not self.enabled() or self.desktop is None:
            return None
        target = selector.partition(":")[2]
        if target in self.config.desktop_threads:
            if self._owned_cli(self.config.thread(target)):
                raise ValueError("Owned CLI sessions cannot be routed through Desktop")
            return Peer.from_record(
                {
                    "peer_id": selector,
                    "provider": self.name,
                    "target": target,
                    "name": target,
                    "thread_id": self.config.thread(target),
                    "kind": "paired_desktop",
                    "capabilities": ["queue", "persisted_result"],
                }
            )
        try:
            ident = str(uuid.UUID(target))
        except ValueError:
            return None
        if self._owned_cli(ident):
            raise ValueError("Owned CLI sessions cannot be routed through Desktop")
        return Peer.from_record(await self.desktop.read(ident))

    async def discover(self) -> DiscoveryPage:
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        cursors: dict[str, str] = {}
        excluded: dict[str, str] = {}
        if self.config.provider_enabled("codex"):
            discovered = []
            if self.desktop is not None:
                try:
                    inventory = await self.desktop.inventory()
                    discovered = inventory["peers"]
                    if not inventory["complete"]:
                        if inventory.get("next_cursor"):
                            cursors["desktop"] = inventory["next_cursor"]
                            detail = "Use desktop_sessions with cursors.desktop to continue. "
                        else:
                            detail = "Some indexed metadata is truncated. "
                        errors["desktop"] = (
                            "Desktop inventory is partial. "
                            + detail
                            + "An exact returned peer_id can still be selected."
                        )
                except (BridgeError, OSError, ValueError, TimeoutError) as error:
                    errors["desktop"] = f"Desktop discovery unavailable ({type(error).__name__})"
            discovered = [peer for peer in discovered if not self._owned_cli(peer["thread_id"])]
            by_id = {peer["thread_id"]: peer for peer in discovered}
            for alias, ident in self.config.desktop_threads.items():
                if self._owned_cli(ident):
                    excluded[f"desktop:{alias}"] = "Session belongs to an owned CLI worker"
                    continue
                peers.append(
                    {
                        **by_id.get(ident, {}),
                        "peer_id": f"desktop:{alias}",
                        "provider": "desktop",
                        "target": alias,
                        "thread_id": ident,
                        "name": alias,
                        "kind": "paired_desktop",
                        "capabilities": ["queue", "persisted_result"],
                    }
                )
            paired_ids = set(self.config.desktop_threads.values())
            peers.extend(peer for peer in discovered if peer["thread_id"] not in paired_ids)
        if not self.config.provider_enabled("codex"):
            errors["desktop"] = "Codex Desktop/IDE provider is disabled"
        return DiscoveryPage.from_record(
            {"peers": peers, "errors": errors, "cursors": cursors, "excluded": excluded}
        )


class CodexAdapter(CodexDiscovery, StoredAdapter):
    name = "desktop"
    registration = "codex"
    aliases = ("codex",)

    def __init__(self, ctx: KernelContext, index: Callable[[], DesktopIndex]) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        CodexDiscovery.__init__(
            self,
            ctx.config,
            index,
            lambda ident: ctx.store.native_session_owner("codex_cli", ident) is not None,
        )
        self._index = index
        self.task_locks = ctx.locks
        self.history_slots = asyncio.Semaphore(2)

    @property
    def desktop_index(self) -> DesktopIndex:
        return self._index()

    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None:
        selected = selector.partition(":")[2]
        ident = self.config.desktop_threads.get(selected)
        if ident is None:
            try:
                ident = str(uuid.UUID(selected))
            except ValueError:
                return None
        row = self.store.by_request(submission.request_id)
        if row is None:
            return None
        if not matches_work(
            row, f"desktop:{ident}", submission.message, submission.request_id, self.name
        ):
            raise ValueError("request_id already describes different work")
        return TaskReceipt.from_record(row)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        record = peer.as_record()
        ident = record.get("thread_id") or self.config.thread(peer.target)
        if peer.kind == "paired_desktop":
            observed = await self.desktop_index.read(ident)
            if observed.get("thread_id") != ident:
                raise ValueError("Paired Desktop identity changed")
            peer = Peer.from_record({**record, "project": observed.get("project", "")})
        return PreparedPeer(f"desktop:{ident}", peer)

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        peer = prepared.peer
        ident = peer.as_record().get("thread_id") or self.config.thread(peer.target)
        alias = peer.target if peer.kind == "paired_desktop" else None
        row = await self.submit_native(
            ident,
            submission.message,
            submission.request_id,
            alias,
            admission_guard=guard,
            expected_project=peer.project or None,
            subject=submission.subject,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    async def observe(self, receipt: TaskReceipt, *, refresh: bool) -> TaskReceipt:
        if not refresh:
            return TaskReceipt.from_record(self.store.get(receipt.task_id))
        async with self.history_slots:
            row = await self.observe_native(receipt.task_id, refresh=refresh)
        return TaskReceipt.from_record(row)

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        return receipt.provider == self.name and (
            receipt.as_record()["metadata"].get("thread_id") == peer.as_record().get("thread_id")
        )

    def poll_ids(self, limit: int) -> tuple[str, ...]:
        return (
            tuple(self.store.observation_ids(self.name, "last_history_attempt", limit))
            if self.enabled()
            else ()
        )

    async def submit_native(
        self,
        thread_id: str,
        message: str,
        request_id: str,
        alias: str | None = None,
        *,
        admission_guard: Guard | None = None,
        expected_project: str | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.provider_enabled("codex"):
            raise ValueError("Codex provider is disabled")
        if self._owned_cli(thread_id):
            raise ValueError("Owned CLI sessions cannot be routed through Desktop")
        if admission_guard is not None:
            admission_guard()
        metadata = {"kind": "desktop", "thread_id": thread_id}
        if alias is None:
            metadata["binding"] = "discovered"
        else:
            metadata["alias"] = alias
        task, created = self.store.reserve(
            f"desktop:{thread_id}", message, request_id, metadata, subject=subject
        )
        if not created:
            return public_task(task)
        try:
            observed = await self.desktop_index.read(thread_id)
            await validate_project(observed.get("project", ""), expected_project)
            if admission_guard is not None:
                admission_guard()
            if self._owned_cli(thread_id):
                raise ValueError("Owned CLI sessions cannot be routed through Desktop")
        except (BridgeError, OSError, ValueError, TimeoutError) as error:
            return public_task(self.store.update(task["id"], "failed", detail=str(error)))
        except asyncio.CancelledError:
            self.store.update(task["id"], "uncertain", detail="Submission was interrupted")
            raise
        # The turn this submission produces cannot start before now. Desktop records
        # startedAt in whole seconds, so the floor is the whole second too.
        self.store.metadata(task["id"], {"submitted_floor": math.floor(time.time())})
        try:
            receipt = await history.queue(
                self.config.codex_binary, thread_id, task["id"], message, self.config.codex_home
            )
        except (BridgeError, OSError, ValueError) as error:
            return public_task(self.store.update(task["id"], "uncertain", detail=str(error)))
        except asyncio.CancelledError:
            self.store.update(task["id"], "uncertain", detail="Submission was interrupted")
            raise
        queue_id = history.queued_submission_id(receipt) if isinstance(receipt, str) else None
        if queue_id is not None:
            self.store.metadata(task["id"], {"queued_submission_id": queue_id})
        return public_task(self.store.update(task["id"], "queued", detail=receipt))

    async def observe_native(self, task_id: str, *, refresh: bool = True) -> dict[str, Any]:
        task = self.store.get(task_id)
        meta = task["metadata"]
        if meta.get("kind") == "desktop" and not self.config.codex_enabled:
            return {**public_task(task), "provider_disabled": True}
        if refresh and task["status"] not in TERMINAL and meta.get("kind") == "desktop":
            task = self.store.metadata(task_id, {"last_history_attempt": time.time()})
            if meta.get("turn_binding") == AMBIGUOUS_TURN_BINDING:
                # Remembered from an earlier poll: no history read can bind this receipt
                # now, however many of the matching turns have since left the window.
                return public_task(task)
            if (
                meta.get("binding") != "discovered"
                and self.config.thread(meta["alias"]) != meta["thread_id"]
            ):
                raise ValueError("Desktop pairing changed; restore it before reading this task")
            turn_id = meta.get("desktop_turn_id")
            result = None
            ambiguous: list[Any] = []
            async with history.HistoryReader(
                self.config.codex_binary, self.config.codex_home
            ) as reader:
                newest, next_cursor = await reader.scan(meta["thread_id"], pages=1)
                window = list(newest)
                cursor = meta.get("history_cursor") or next_cursor
                if turn_id is not None:
                    result = history.result_from_turns(task_id, turn_id, window)
                if (turn_id is None or result is None) and cursor:
                    # Unbound: the deeper pages too, so that one turn with the queued text
                    # is one in this window, not one on the newest page.
                    older, cursor = await reader.scan(meta["thread_id"], pages=4, cursor=cursor)
                    window = [*newest, *older]
                if turn_id is None:
                    floor = meta.get("submitted_floor")
                    if type(floor) not in (int, float):
                        floor = math.floor(task["created"])
                    matching = history.submitted_turns(task_id, task["message"], floor, window)
                    if len(matching) > 1:
                        ambiguous = [turn.get("id") for turn in matching]
                    else:
                        turn_id = history.submitted_turn(task_id, task["message"], floor, window)
                if turn_id is not None and result is None:
                    result = history.result_from_turns(task_id, turn_id, window)
            if ambiguous:
                # Two turns carry the queued text. Whichever of them a later window still
                # holds, this receipt is never bound to it: the state is kept, not the turns.
                task = self.store.metadata(
                    task_id,
                    {
                        "turn_binding": AMBIGUOUS_TURN_BINDING,
                        "ambiguous_turn_ids": ambiguous,
                        "history_cursor": None,
                        "history_search_incomplete": False,
                    },
                )
                return public_task(
                    self.store.update(task_id, "uncertain", detail=AMBIGUOUS_TURN_DETAIL)
                )
            changes: dict[str, Any] = {
                "history_cursor": cursor if result is None else None,
                "history_search_incomplete": bool(cursor) and result is None,
            }
            if turn_id is not None:
                changes["desktop_turn_id"] = turn_id
            task = self.store.metadata(task_id, changes)
            if result:
                status, text, turn_id = result
                if len(text) > 200_000:
                    raise BridgeError(
                        "Desktop result exceeds 200000 characters; receipt is not complete"
                    )
                task = self.store.update(task_id, status, result=text, turn_id=turn_id)
        return public_task(task)

    async def reconcile(self, task_id: str) -> dict[str, Any]:
        """Explicit recovery; ordinary reads never rewrite a terminal receipt."""
        lock = self.task_locks[hash(task_id) % len(self.task_locks)]
        async with lock, self.history_slots:
            task = self.store.get(task_id)
            meta = task["metadata"]
            if task["status"] == "completed" and meta.get("desktop_reconciliation"):
                return public_task(task)
            if meta.get("turn_binding") == AMBIGUOUS_TURN_BINDING:
                raise ValueError(
                    "Reconciliation refused: " + AMBIGUOUS_TURN_DETAIL.partition(". ")[0]
                )
            if not is_reconcilable_desktop_failure(task):
                raise ValueError("Reconciliation requires a failed Desktop history receipt")
            if not self.config.provider_enabled("codex"):
                raise ValueError("Codex provider is disabled")
            if (
                meta.get("binding") != "discovered"
                and self.config.thread(meta["alias"]) != meta["thread_id"]
            ):
                raise ValueError("Desktop pairing changed; restore it before reconciliation")
            async with history.HistoryReader(
                self.config.codex_binary, self.config.codex_home
            ) as reader:
                turns, _ = await reader.scan(meta["thread_id"], pages=5)
            matching = [turn for turn in turns if turn.get("id") == task["turn_id"]]
            if len(matching) != 1 or not history.has_terminal_timestamp(matching[0]):
                raise ValueError("Exact completed turn not verified in the history window")
            result = history.result_from_turns(task_id, task["turn_id"], matching)
            if result is None or result[0] != "completed" or not result[1]:
                raise ValueError("Exact completed turn and matching final answer required")
            return public_task(
                self.store.reconcile(
                    task,
                    result=result[1],
                    evidence={"completed_at": matching[0]["completedAt"]},
                )
            )
