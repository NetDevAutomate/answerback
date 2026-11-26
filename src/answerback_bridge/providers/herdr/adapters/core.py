"""Core provider discovery and execution implementation."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ....adapters.base import StoredAdapter
from ....adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ....config import Config, public_task
from ....plugin import KernelContext
from ....process import BridgeError
from ....tasks import TRUSTED_OS_ACCOUNT
from ..core import RESULT_MAX_CHARS, ResultText, result_path
from ..core_tasks import CoreTasks

# A remote receipt's result file is read at most once in this many seconds, except
# when a person asks with `task_reconcile`.
READ_INTERVAL = 5.0
ENDED = "terminal_ended_no_result"
# Metadata flag set once a receipt is classified ENDED. Status alone cannot say this:
# a verified send whose turn start was not seen is `uncertain` from the start.
ENDED_FLAG = "terminal_ended"


def _clock() -> float:
    return time.time()


def parse_result(text: str, task_id: str, token: str) -> tuple[str, str | None]:
    """Classify a result file's text: ("not_yet", None), ("refused", why) or ("body", text).

    `\\r\\n` line ends count as `\\n`, and one final newline is optional. The first
    line must be exactly `answerback-result <task_id> <token>` and the last line
    exactly `answerback-end <token>`; anything else is not this receipt's finished
    file yet. The body between them must hold 1 to RESULT_MAX_CHARS characters.
    """
    text = text.replace("\r\n", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    head, newline, rest = text.partition("\n")
    if not newline or head != f"answerback-result {task_id} {token}":
        return "not_yet", None
    end = f"answerback-end {token}"
    if rest == end:
        body = ""
    else:
        body, newline, last = rest.rpartition("\n")
        if not newline or last != end:
            return "not_yet", None
    if not body:
        return "refused", "Result file has an empty result; write the result between the lines"
    if len(body) > RESULT_MAX_CHARS:
        return "refused", f"Result file exceeds {RESULT_MAX_CHARS} characters; receipt not complete"
    return "body", body


class CoreDiscovery:
    name = "hc"
    registration = "hc"
    aliases = ()

    def __init__(self, config: Config, native: Callable[[], Any]) -> None:
        self.config, self._native = config, native

    @property
    def core(self) -> Any:
        return self._native()

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        if self.enabled():
            return Peer.from_record(await self.core.peer(selector.partition(":")[2]))
        return None

    async def discover(self) -> DiscoveryPage:
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        cursors: dict[str, str] = {}
        excluded: dict[str, str] = {}
        if self.config.provider_enabled("hc"):
            try:
                inventory = await self.core.inventory()
                peers.extend(inventory["peers"])
                if not inventory["complete"]:
                    errors["hc"] = str(inventory["errors"])
            except (BridgeError, OSError, ValueError, TimeoutError) as error:
                errors["hc"] = f"Core discovery unavailable ({type(error).__name__})"
        if not self.config.provider_enabled("hc"):
            errors["hc"] = (
                "Core is not configured; run `answerback hc-configure` "
                "or `answerback configure --runtime hc`"
            )
        return DiscoveryPage.from_record(
            {"peers": peers, "errors": errors, "cursors": cursors, "excluded": excluded}
        )


class CoreAdapter(CoreDiscovery, StoredAdapter):
    def __init__(
        self,
        ctx: KernelContext,
        native: Callable[[], Any],
        tasks: Callable[[], CoreTasks],
        lock: asyncio.Lock,
    ) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        CoreDiscovery.__init__(self, ctx.config, native)
        self._tasks, self.lock = tasks, lock
        # Striped like the service's task locks: one result read per receipt at a time,
        # whether `observe` or `reconcile` started it.
        self._read_locks = [asyncio.Lock() for _ in range(16)]

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        return PreparedPeer(self._tasks().target_key(self.core, peer.target), peer)

    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None:
        row = self._tasks().previous(
            self.core, selector.partition(":")[2], submission.message, submission.request_id
        )
        return TaskReceipt.from_record(row) if row else None

    async def submit(
        self,
        prepared: PreparedPeer,
        submission: Submission,
        *,
        guard: Guard,
    ) -> TaskReceipt:
        self.require_enabled()
        async with self.lock:
            guard()
            row = await self._tasks().submit(
                self.core,
                prepared.peer.target,
                submission.message,
                submission.request_id,
                expected=prepared.peer.as_record(),
                admission_guard=guard,
                subject=submission.subject,
            )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        meta = receipt.as_record().get("metadata", {})
        return receipt.provider == self.name and (
            meta.get("target") == peer.target
            and meta.get("peer", {}).get("binding") == peer.as_record().get("binding")
        )

    # Result files of terminals on another machine (plan row A2).

    def _machines(self) -> frozenset[str]:
        # Stand-in Core objects in tests may have no allow-list.
        return frozenset(getattr(self.core, "send_machines", ()) or ())

    def _unreadable(self, task: dict[str, Any], *, person: bool) -> str | None:
        """Why this receipt's result file is not read now, or None to read it."""
        meta = task["metadata"]
        if meta.get("kind") != self.name or not isinstance(meta.get("result_token"), str):
            return "it has no result file"
        if meta.get("identity_invalidated"):
            return "its delivery identity was invalidated"
        if not (
            task["status"] == "submitted"
            or (task["status"] == "uncertain" and meta.get("delivery_verified") is True)
        ):
            return f"it is {task['status']}"
        if meta.get("result_machine") not in self._machines():
            return "its machine is not in hc_send_machines"
        if not person:
            # A refused file is read again only for a person; until the terminal is
            # classified ended, polls still look at the terminal (see `_collect`).
            if meta.get("result_refused") and meta.get(ENDED_FLAG):
                return "its result file was refused and its terminal ended"
            last = meta.get("last_result_read")
            if isinstance(last, (int, float)) and _clock() - last < READ_INTERVAL:
                return "it was read less than 5 seconds ago"
        return None

    async def observe(self, receipt: TaskReceipt, *, refresh: bool) -> TaskReceipt:
        task = self.store.get(receipt.task_id)
        if refresh and self.enabled() and self._unreadable(task, person=False) is None:
            async with self._read_lock(receipt.task_id):
                task = self.store.get(receipt.task_id)
                if self._unreadable(task, person=False) is None:
                    await self._collect(task, person=False)
        return TaskReceipt.from_record(self.store.get(receipt.task_id))

    def poll_ids(self, limit: int) -> tuple[str, ...]:
        if not self.enabled():
            return ()
        return tuple(
            self.store.observation_ids(
                self.name, "last_result_read", limit, result_machines=self._machines()
            )
        )

    async def reconcile(self, task_id: str) -> dict[str, Any]:
        """A person's read of a remote result file now; it never sends anything."""
        task = self.store.get(task_id)
        if not isinstance(task["metadata"].get("result_token"), str):
            raise ValueError(
                f"Provider {self.name} does not support explicit reconciliation "
                "of a receipt without a result file"
            )
        if task["status"] == "completed":
            return public_task(task)
        self.require_enabled()
        async with self._read_lock(task_id):
            task = self.store.get(task_id)
            reason = self._unreadable(task, person=True)
            if reason is not None:
                raise ValueError(f"Result file not read: {reason}")
            await self._collect(task, person=True)
        return public_task(self.store.get(task_id))

    async def collect_result(self, task_id: str) -> None:
        """Read this receipt's result file once and apply it, whatever its status.

        No production path calls this: `observe` and `reconcile` check readability
        and call `_collect` themselves. Tests use it to show that a file found before
        delivery was verified is held by `complete_worker`. It needs a stored token.
        """
        async with self._read_lock(task_id):
            task = self.store.get(task_id)
            if task["metadata"].get("identity_invalidated"):
                raise ValueError("Delivery identity was invalidated; worker completion is refused")
            await self._collect(task, person=True)

    def _read_lock(self, task_id: str) -> asyncio.Lock:
        return self._read_locks[hash(task_id) % len(self._read_locks)]

    async def _collect(self, task: dict[str, Any], *, person: bool) -> None:
        """Read the file, and when there is no valid one, ask whether the terminal ended.

        A file refused earlier is read again only for a person; a poll goes straight
        to the terminal check. Every Core failure propagates as BridgeError with the
        receipt unchanged (the stamp aside), so the reconcile loop logs it. Nothing
        here sends.
        """
        task_id, meta = task["id"], task["metadata"]
        token, machine = meta["result_token"], meta["result_machine"]
        path = result_path(task_id, token)
        self.store.metadata(task_id, {"last_result_read": _clock()})
        refusal: str | None = None
        if person or not meta.get("result_refused"):
            outcome, refusal = self._apply(task, await self.core.read_result(machine, path))
            if outcome == "completed":
                return
        # Classification, not status, ends the terminal checks: a verified send whose
        # turn start was not seen is `uncertain` but its terminal was never looked at.
        delivered = task["status"] == "submitted" or (
            task["status"] == "uncertain" and meta.get("delivery_verified") is True
        )
        if meta.get(ENDED_FLAG) or not delivered:
            return
        # An ended terminal is not a missing file (plan section 4, rule 5), and a
        # refused file is no result either. A child of that terminal may still write
        # the file, so the receipt stays readable.
        if not await self.core.terminal_ended(meta["target"]):
            return
        outcome, again = self._apply(task, await self.core.read_result(machine, path))
        if outcome == "completed":
            return
        refusal = again or refusal
        detail = (
            f"{ENDED}: Herdr core reports the terminal ended and no valid result file "
            "was found; a file that appears later still completes this receipt. "
            "Inspect before sending the work again"
        )
        if refusal is not None:
            detail += f". The result file was refused: {refusal}"
        # Status first, flag second: a crash between them leaves a receipt that is
        # checked once more, never one that is flagged but still `submitted`.
        self.store.update(task_id, "uncertain", detail=detail)
        self.store.metadata(task_id, {ENDED_FLAG: True})

    def _apply(self, task: dict[str, Any], read: ResultText | None) -> tuple[str, str | None]:
        """Apply a read file: ("completed", None), ("refused", why) or ("none", None).

        "none" means there is no finished file for this receipt yet.
        """
        if read is None:
            return "none", None
        verdict, value = parse_result(read.text, task["id"], task["metadata"]["result_token"])
        if verdict == "not_yet" or value is None:
            return "none", None
        if verdict == "body" and not read.utf8:
            # Nothing of a file that is not UTF-8 text is stored, not even its body.
            verdict, value = "refused", "Result file is not valid UTF-8 text; receipt not complete"
        if verdict == "refused":
            self.store.metadata(task["id"], {"result_refused": True})
            self.store.update(task["id"], task["status"], detail=value)
            return "refused", value
        # The file proves that the brief reached the listed machine's OS account, not
        # which program of that account wrote it; the receipt says so. Herdr Core's
        # machine record names no account, so `result_account` is not recorded.
        self.store.complete_worker(
            task["id"],
            value,
            provenance={
                "result_authentication": TRUSTED_OS_ACCOUNT,
                "result_machine": task["metadata"]["result_machine"],
            },
        )
        return "completed", None

    async def close(self) -> None:
        await self._tasks().close()
