"""Managed workers: which terminals are ours, creating one, waiting for it, removing it.

A worker is managed only when this module can prove it made it: a registry row written at
creation whose `created_ms` still matches the live terminal, or a bare `hc run` terminal in
the project whose core name fits the policy's pattern and that was never given an App tab.
Every other terminal is the owner's and is never listed for teardown.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..placement.probe import LOCAL
from ..pressure import provider_of
from ..process import BridgeError
from ..tasks import STATES, TERMINAL
from .policy import Harness, Policy, classify
from .ports import (
    BridgeCalls,
    Handoffs,
    Layout,
    LayoutError,
    Ledger,
    RoutingError,
    Terminals,
    local_address,
)

ACTIVE_RECEIPTS = frozenset(STATES) - frozenset(TERMINAL)
REGISTRY_NAME = "routing-workers.json"
# A gate reserved just before a submit whose receipt never appeared (the submit raised)
# holds for this long, in case the bridge did store the brief; then it lapses.
GATE_RESERVE_SECONDS = 300
# Provider backoff (T-227): how often `create` asks the bridge again while it waits, and
# how long past the first `until` it waits before it gives up.
PRESSURE_POLL_SECONDS = 15.0
PRESSURE_GRACE_SECONDS = 60.0
# `task_list` reads the newest RECEIPT_WINDOW receipts of every peer. A list shorter than
# that is the whole store, so a worker missing from it was never dispatched. A full list is
# only the newest receipts, and a miss in it proves nothing: the worker's receipt may be
# older than the window.
RECEIPT_WINDOW = 100


def pressure_rows(status: Any) -> list[dict[str, Any]]:
    """The per-account rows of a `pressure_status` result.

    The RPC answers `{"pressure": <snapshot>, "settings": ..., ...}`, where the snapshot
    is keyed `provider:account_hint`; a bare snapshot is read the same way.
    """
    if isinstance(status, dict) and isinstance(status.get("pressure"), dict | list):
        status = status["pressure"]
    if isinstance(status, dict):
        rows = list(status.values())
    elif isinstance(status, list):
        rows = status
    else:
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("provider"), str)]


class AtCapacity(RoutingError):
    """Creating another worker would exceed max_workers or max_total; queue instead."""


def row_machine(row: Mapping[str, Any] | None) -> str:
    """The machine a worker row names; a row without the field is of this machine."""
    machine = row.get("machine") if row else None
    return machine if isinstance(machine, str) and machine else LOCAL


@dataclass(frozen=True)
class Worker:
    address: str
    name: str
    task_class: str
    harness: str
    state: str
    created_ms: int
    labels: Mapping[str, str]
    entry: Mapping[str, Any] | None

    @property
    def machine(self) -> str:
        """The machine the worker runs on, as the placement package names it."""
        return row_machine(self.entry)

    def public(self) -> dict[str, Any]:
        entry = self.entry or {}
        return {
            "address": self.address,
            "peer_id": f"hc:{self.address}",
            "name": self.name,
            "class": self.task_class,
            "harness": self.harness,
            "state": self.state,
            "registered": self.entry is not None,
            "task": entry.get("task"),
            "request_id": entry.get("request_id"),
            "receipt_id": entry.get("receipt_id"),
            "space": self.labels.get("herdr.space"),
            "tab": self.labels.get("herdr.tab"),
        }


@dataclass(frozen=True)
class Facts:
    """What gc knows about one managed worker when it decides."""

    worker: Worker
    receipt: str  # "none", "unknown", or a receipt status
    since: float  # epoch seconds of the last receipt change, else of creation
    handoff: str  # merged, rejected or open
    reusable: bool  # idle and send_ready: it could take another assignment


@dataclass
class Verdict:
    worker: Worker
    action: str
    reason: str
    since: float = 0.0
    held: bool = field(default=False, repr=False)  # kept by a hold, not by a never_kill rule

    def public(self) -> dict[str, Any]:
        return {**self.worker.public(), "action": self.action, "reason": self.reason}


class Registry:
    """Rows for the workers this tool created, beside the bridge config (0600)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Worker rows by address, and gate holders by request id."""
        try:
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {}, {}
        except (OSError, ValueError):
            raise RoutingError(f"Worker registry unreadable: {self.path}") from None
        rows = data.get("workers") if isinstance(data, dict) else None
        gates = data.get("gates", {}) if isinstance(data, dict) else None
        if not isinstance(rows, dict) or not isinstance(gates, dict):
            raise RoutingError(f"Worker registry has an unsupported shape: {self.path}")
        for section in (rows, gates):
            if not all(isinstance(row, dict) for row in section.values()):
                raise RoutingError(f"Worker registry has an unsupported shape: {self.path}")
        return rows, gates

    def load(self) -> dict[str, dict[str, Any]]:
        return self._read()[0]

    def gates(self) -> dict[str, dict[str, Any]]:
        """Briefs dispatched to run a full gate and not yet seen to finish."""
        return self._read()[1]

    def _change(
        self, edit: Callable[[dict[str, dict[str, Any]], dict[str, dict[str, Any]]], None]
    ) -> None:
        """Read, edit and replace under a lock, so two routes cannot lose a row."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows, gates = self._read()
            edit(rows, gates)
            handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(
                        {"version": 1, "workers": rows, "gates": gates},
                        stream,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        finally:
            os.close(lock)

    def change(self, edit: Callable[[dict[str, dict[str, Any]]], None]) -> None:
        self._change(lambda rows, _gates: edit(rows))

    def change_gates(self, edit: Callable[[dict[str, dict[str, Any]]], None]) -> None:
        self._change(lambda _rows, gates: edit(gates))

    def put(self, address: str, row: dict[str, Any]) -> None:
        self.change(lambda rows: rows.__setitem__(address, row))

    def update(self, address: str, **fields: Any) -> None:
        def edit(rows: dict[str, dict[str, Any]]) -> None:
            if address in rows:
                rows[address].update(fields)

        self.change(edit)

    def drop(self, address: str) -> None:
        def edit(rows: dict[str, dict[str, Any]]) -> None:
            rows.pop(address, None)

        self.change(edit)


def plan_gc(
    policy: Policy, facts: list[Facts], queued: Mapping[str, int], now: float
) -> tuple[list[Verdict], dict[str, int]]:
    """Decide keep or remove for each managed worker. Pure: no terminal is touched."""
    grace = policy.keep_idle_min * 60
    verdicts: list[Verdict] = []
    reusable = {id(fact.worker): fact.reusable for fact in facts}
    for fact in facts:
        worker = fact.worker
        age = now - fact.since
        left = f"{max(0, int((grace - age) // 60) + 1)} min of keep_idle_min left"
        held = False
        if worker.state in policy.never_kill.states:
            action, reason = "keep", f"never_kill: state={worker.state}"
        elif (
            fact.receipt in policy.never_kill.receipts
            or fact.receipt in ACTIVE_RECEIPTS
            or fact.receipt == "unknown"
        ):
            action, reason = "keep", f"never_kill: receipt {fact.receipt}"
        elif fact.receipt == "completed" and fact.handoff == "open":
            action, reason = "keep", "hand-off not merged or rejected yet"
        elif fact.receipt != "completed" and age < grace:
            what = "not dispatched yet" if fact.receipt == "none" else f"receipt {fact.receipt}"
            action, reason = "keep", f"{what}; {left}"
        elif fact.reusable and age < grace and queued.get(worker.task_class, 0) > 0:
            action, reason, held = "keep", f"reusable and its class has queued work; {left}", True
        elif fact.receipt == "completed":
            action, reason = "remove", f"receipt completed, hand-off {fact.handoff}"
        elif fact.receipt == "none":
            action, reason = "remove", "never dispatched and idle past keep_idle_min"
        else:
            action, reason = "remove", f"receipt {fact.receipt}, past keep_idle_min"
        verdicts.append(Verdict(worker, action, reason, fact.since, held))

    deficit: dict[str, int] = {}
    for task_class, size in policy.warm_pool.items():
        if size <= 0 or queued.get(task_class, 0) <= 0:
            continue
        of_class = [v for v in verdicts if v.worker.task_class == task_class]
        warm = sum(1 for v in of_class if v.action == "keep" and reusable[id(v.worker)])
        spare = sorted(
            (v for v in of_class if v.action == "remove" and reusable[id(v.worker)]),
            key=lambda v: v.since,
            reverse=True,
        )
        for verdict in spare[: max(0, size - warm)]:
            verdict.action, verdict.held = "keep", True
            verdict.reason = f"warm_pool: {task_class} keeps {size} idle while it has queued work"
            warm += 1
        if warm < size:
            deficit[task_class] = size - warm

    kept = Counter(v.worker.harness for v in verdicts if v.action == "keep")
    for verdict in sorted((v for v in verdicts if v.held), key=lambda v: v.since):
        harness = policy.harnesses.get(verdict.worker.harness)
        over_harness = harness is not None and kept[verdict.worker.harness] > harness.max_workers
        if over_harness or sum(kept.values()) > policy.max_total:
            verdict.action, verdict.held = "remove", False
            verdict.reason = "over capacity: a hold does not outrank max_workers or max_total"
            kept[verdict.worker.harness] -= 1
    return verdicts, deficit


class Pool:
    def __init__(
        self,
        policy: Policy,
        terminals: Terminals,
        layout: Layout,
        bridge: BridgeCalls,
        registry: Registry,
        handoffs: Handoffs,
        ledger: Ledger,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 3.0,
    ) -> None:
        self.policy = policy
        self.terminals = terminals
        self.layout = layout
        self.bridge = bridge
        self.registry = registry
        self.handoffs = handoffs
        self.ledger = ledger
        self.now = now
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    def _managed(self, detail: dict[str, Any], entry: dict[str, Any] | None) -> Worker | None:
        raw = detail.get("labels")
        labels = {
            key: value
            for key, value in (raw if isinstance(raw, dict) else {}).items()
            if isinstance(value, str)
        }
        created = detail.get("created_ms")
        if type(created) is not int:
            return None
        status = detail.get("status")
        state = status.get("state") if isinstance(status, dict) else None
        state = state if isinstance(state, str) else "unknown"
        address = str(detail["address"])
        if entry is not None and entry.get("created_ms") == created:
            return Worker(
                address,
                str(entry["name"]),
                str(entry["class"]),
                str(entry["harness"]),
                state,
                created,
                labels,
                entry,
            )
        # No proof of creation. Only a bare `hc run` worker qualifies: our name pattern,
        # launched in this project, and no App tab (a tab without a row is the owner's).
        named = self.policy.parse_worker_name(detail.get("name"))
        if named is None or "herdr.tab" in labels or detail.get("cwd") != self.policy.project:
            return None
        return Worker(
            address,
            str(detail["name"]),
            named.task_class,
            named.harness,
            state,
            created,
            labels,
            None,
        )

    async def workers(self) -> list[Worker]:
        rows = self.registry.load()
        found: list[Worker] = []
        for row in await self.terminals.live():
            address = row.get("address")
            entry = rows.get(address) if isinstance(address, str) else None
            if entry is not None and entry.get("project") != self.policy.project:
                entry = None
            # No filter on the list row: `hc ls` rows carry no name (address, agent, cwd,
            # state, status, turn, where). The name `hc run --name` set is read from
            # `hc get`, and _managed decides on that.
            detail = await self.terminals.get(str(address))
            if detail is None or detail.get("state") != "live":
                continue
            worker = self._managed(detail, entry)
            if worker is not None:
                found.append(worker)
        return found

    def capacity(self, workers: list[Worker], harness: Harness) -> str | None:
        """Return why another worker of this harness may not be created, else None."""
        if len(workers) >= self.policy.max_total:
            return f"max_total {self.policy.max_total} live workers in this project"
        mine = sum(1 for worker in workers if worker.harness == harness.name)
        if mine >= harness.max_workers:
            return f"max_workers {harness.max_workers} live {harness.name} workers"
        return None

    def gated(self, task_class: str) -> bool:
        """Whether a brief of this class ends in a full gate run the policy caps."""
        return (
            self.policy.max_concurrent_gates is not None and task_class in self.policy.gate_classes
        )

    async def _gate_status(
        self,
        key: str,
        row: Mapping[str, Any],
        listed: list[dict[str, Any]] | None,
    ) -> str | None:
        """Why this row still holds the gate (a status word), or None when it let go."""
        if listed is None:
            return "unknown"
        receipt = next(
            (
                item
                for item in listed
                if item.get("request_key") == key
                or (row.get("receipt_id") and item.get("id") == row["receipt_id"])
            ),
            None,
        )
        if receipt is None and row.get("receipt_id"):
            # The list holds the latest 100 receipts; this one may be older.
            try:
                receipt = await self.bridge.call("task_get", {"task_id": row["receipt_id"]})
            except BridgeError:
                return "unknown"
        if receipt is None:
            try:
                young = self.now() - float(row["reserved_at"]) < GATE_RESERVE_SECONDS
            except (KeyError, TypeError, ValueError):
                return "unknown"
            if not young:
                return None
            status = "reserved"
        else:
            status = receipt.get("status") if isinstance(receipt, dict) else None
            if status in TERMINAL:
                return None
            status = str(status) if status in ACTIVE_RECEIPTS else "unknown"
        # A terminal that is gone cannot be running a gate, whatever its receipt says (a
        # killed worker leaves its receipt `submitted` for good).
        try:
            detail = await self.terminals.get(local_address(row.get("address")))
            if detail is None or detail.get("state") != "live":
                return None
        except RoutingError:
            pass
        return status

    async def gate_state(self) -> tuple[list[dict[str, Any]], set[str]]:
        """This project's gate holders, and the request ids of rows that let go."""
        rows = {
            key: row
            for key, row in self.registry.gates().items()
            if row.get("project") == self.policy.project
        }
        if not rows:
            return [], set()
        listed: list[dict[str, Any]] | None
        try:
            found = (await self.bridge.call("task_list", {"query": "", "limit": 100}))["tasks"]
            listed = [item for item in found if isinstance(item, dict)]
        except (BridgeError, KeyError, TypeError):
            listed = None
        holders: list[dict[str, Any]] = []
        released: set[str] = set()
        for key, row in sorted(rows.items(), key=lambda pair: str(pair[1].get("reserved_at"))):
            status = await self._gate_status(key, row, listed)
            if status is None:
                released.add(key)
                continue
            holders.append(
                {
                    "request_id": key,
                    "status": status,
                    **{
                        name: row.get(name)
                        for name in ("address", "task", "lane", "class", "receipt_id")
                    },
                }
            )
        return holders, released

    def gate_busy(self, holders: list[dict[str, Any]]) -> str:
        return (
            f"gate_busy: {len(holders)} of {self.policy.max_concurrent_gates} full gates held "
            f"({', '.join(str(holder['request_id']) for holder in holders)}); queue the brief "
            "until a holder's receipt finishes"
        )

    async def gate_holders(self, task: str | None = None, lane: str | None = None) -> list[Any]:
        """Holders that count against a new brief; the same task and lane is a retry."""
        holders, _ = await self.gate_state()
        return [
            holder
            for holder in holders
            if task is None or (holder["task"], holder["lane"]) != (task, lane)
        ]

    async def reserve_gate(
        self, request_id: str, *, address: str, task: str, lane: str, task_class: str
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """Take a gate slot for a brief about to be submitted.

        Returns (slots now held, the other holders); the count is None when the cap is
        reached and nothing was recorded. Receipts are read before the lock; a row another
        route added meanwhile was not judged and counts as held.
        """
        cap = self.policy.max_concurrent_gates
        assert cap is not None
        holders, released = await self.gate_state()
        project = self.policy.project
        taken: list[int | None] = [None]

        def edit(gates: dict[str, dict[str, Any]]) -> None:
            for key in list(gates):
                row = gates[key]
                if row.get("project") != project:
                    continue
                if key in released or (row.get("task"), row.get("lane")) == (task, lane):
                    del gates[key]
            held = [key for key, row in gates.items() if row.get("project") == project]
            if len(held) >= cap:
                return
            gates[request_id] = {
                "project": project,
                "address": address,
                "task": task,
                "lane": lane,
                "class": task_class,
                "receipt_id": None,
                "reserved_at": self.now(),
            }
            taken[0] = len(held) + 1

        self.registry.change_gates(edit)
        others = [holder for holder in holders if (holder["task"], holder["lane"]) != (task, lane)]
        if taken[0] is None and len(others) < cap:
            # A concurrent route took the slot between the read and the lock.
            others, _ = await self.gate_state()
        return taken[0], others

    def gate_receipt(self, request_id: str, receipt_id: Any) -> None:
        def edit(gates: dict[str, dict[str, Any]]) -> None:
            if request_id in gates:
                gates[request_id]["receipt_id"] = receipt_id

        self.registry.change_gates(edit)

    def release_gate(self, request_id: str) -> None:
        """Let a reservation go when its submit is known to have left no receipt (a
        `provider_backoff` answer); otherwise it would lapse only after
        `GATE_RESERVE_SECONDS`."""

        def edit(gates: dict[str, dict[str, Any]]) -> None:
            gates.pop(request_id, None)

        self.registry.change_gates(edit)

    async def create(
        self, task_class: str, harness_name: str, day: str, *, gate_checked: bool = False
    ) -> dict[str, Any]:
        """Start one bare worker as a named tab in the project's App space.

        A worker for a class that runs a full gate is not started while the gate is held,
        unless the caller (`route`) already checked the gate for its own brief.
        """
        policy = self.policy
        harness = policy.harnesses.get(harness_name)
        if harness is None or not harness.creatable:
            raise RoutingError(f"Harness {harness_name!r} cannot be created from here")
        if task_class not in policy.classes:
            raise RoutingError(f"Unknown class {task_class!r}")
        full = self.capacity(await self.workers(), harness)
        if full is not None:
            raise AtCapacity(f"At capacity: {full}")
        if self.gated(task_class) and not gate_checked:
            holders = await self.gate_holders()
            if len(holders) >= (self.policy.max_concurrent_gates or 0):
                raise AtCapacity(self.gate_busy(holders))
        notes: list[str] = []
        waited = await self.wait_for_provider(harness)
        if waited is not None:
            notes.append(waited)
            # Minutes may have passed; the fleet may have filled up meanwhile.
            full = self.capacity(await self.workers(), harness)
            if full is not None:
                raise AtCapacity(f"At capacity: {full}")
        taken = {row.get("name") for row in await self.terminals.live()}
        taken |= {row.get("name") for row in self.registry.load().values()}
        n = 1
        while policy.worker_name(task_class, harness_name, day, n) in taken:
            n += 1
        name = policy.worker_name(task_class, harness_name, day, n)
        argv = list(harness.launch)
        try:
            if policy.project_space is None:
                raise LayoutError("the policy has no project_space")
            made = await self.layout.tab_new(
                space=policy.project_space, folder=policy.project, name=name, argv=argv
            )
            address = local_address(made.get("terminal"))
            placement = "tab.new"
        except LayoutError as error:
            notes.append(f"tab.new unavailable ({error}); used hc run")
            address = await self.terminals.run(name=name, cwd=policy.project, argv=argv)
            placement = "hc run"
        detail = await self.terminals.get(address)
        if detail is None or type(detail.get("created_ms")) is not int:
            raise RoutingError(f"Worker {name} ({address}) was created but cannot be read back")
        row: dict[str, Any] = {
            "name": name,
            "class": task_class,
            "harness": harness_name,
            "project": policy.project,
            "created_ms": detail["created_ms"],
            "created_at": self.now(),
            "placement": placement,
            # Every worker the pool creates runs on this machine. `placement` above says
            # how the terminal was made; it is not a machine.
            "machine": LOCAL,
        }
        # Recorded before anything else can fail: an unrecorded tab is unmanageable.
        self.registry.put(address, row)
        shown = placement == "tab.new"
        if placement == "hc run":
            try:
                await self.layout.keep(address)
                placement = "hc run + terminal.keep"
                shown = True
                detail = await self.terminals.get(address) or detail
            except LayoutError as error:
                notes.append(f"terminal.keep failed ({error}); the App sidebar will not list it")
        pointer = "unset"
        if shown:
            # After tab.new and after terminal.keep alike. Outside the tab.new attempt and
            # after the registry write, so a failure here can neither start a second
            # worker nor leave an unrecorded one.
            try:
                await self.layout.right_click(address)
                pointer = "menu"
            except RoutingError as error:
                notes.append(
                    f"terminal.right_click failed ({error}); right-clicks in the pane may "
                    "reach the worker's prompt"
                )
        raw = detail.get("labels")
        labels: dict[str, Any] = raw if isinstance(raw, dict) else {}
        visible = "herdr.tab" in labels and labels.get("herdr.space") == policy.project_space
        if not visible:
            notes.append(
                "not in the project's App space: expected herdr.space "
                f"{policy.project_space}, found {labels.get('herdr.space')}"
            )
        self.registry.update(address, placement=placement, tab=labels.get("herdr.tab"))
        return {
            "address": address,
            "created_ms": row["created_ms"],
            "peer_id": f"hc:{address}",
            "name": name,
            "class": task_class,
            "harness": harness_name,
            "model": harness.model,
            "placement": placement,
            "visible": visible,
            "pointer": pointer,
            "notes": notes,
        }

    async def pressure_hold(self, harness: Harness) -> dict[str, Any] | None:
        """The bridge's provider pause that holds launching `harness`, or None.

        A bridge without `pressure_status` (an older daemon) holds nothing.
        """
        provider = provider_of(harness.agent_kind or "")
        if provider == "unknown":
            return None
        try:
            status = await self.bridge.call("pressure_status", {})
        except BridgeError:
            return None
        now = self.now()
        held = [
            row
            for row in pressure_rows(status)
            if row["provider"] == provider
            and isinstance(row.get("level"), int)
            and row["level"] > 0
            and isinstance(row.get("until"), int | float)
            and row["until"] > now
        ]
        if not held:
            return None
        row = max(held, key=lambda row: row["until"])
        return {
            "provider": provider,
            "account_hint": row.get("account_hint"),
            "level": row["level"],
            "until": row["until"],
            "retry_after_s": round(row["until"] - now, 1),
        }

    async def wait_for_provider(self, harness: Harness) -> str | None:
        """Wait while the harness's provider is held; a note when it waited, else None.

        Re-asks every PRESSURE_POLL_SECONDS and gives up PRESSURE_GRACE_SECONDS after the
        first `until` it saw. Nothing is started, sent or stopped meanwhile.
        """
        hold = await self.pressure_hold(harness)
        if hold is None:
            return None
        started, deadline = self.now(), hold["until"] + PRESSURE_GRACE_SECONDS
        while hold is not None:
            now = self.now()
            if now >= deadline:
                raise RoutingError("provider backoff did not clear")
            await self.sleep(min(PRESSURE_POLL_SECONDS, max(0.0, deadline - now)))
            hold = await self.pressure_hold(harness)
        provider = provider_of(harness.agent_kind or "")
        return f"waited {self.now() - started:.0f} s for {provider} provider backoff"

    async def _hc_peers(self) -> dict[str, dict[str, Any]]:
        found = await self.bridge.call("peer_search", {"query": "", "provider": "hc"})
        return {
            peer["target"]: peer
            for peer in found.get("peers", [])
            if isinstance(peer, dict) and isinstance(peer.get("target"), str)
        }

    async def wait_ready(
        self, address: str, harness: Harness, *, created_ms: int
    ) -> dict[str, Any]:
        """Poll until this created instance is ready for delivery. Never sends anything."""
        deadline = self.now() + self.policy.prime_timeout_s
        reason = "not seen by the peer scanner (launch script still in the foreground?)"
        while True:
            try:
                peer = (await self._hc_peers()).get(address)
            except BridgeError as error:
                peer, reason = None, f"peer scan failed: {error}"
            if peer is not None:
                if peer.get("agent_kind") != harness.agent_kind:
                    return {
                        "status": "identity_mismatch",
                        "reason": f"foreground is {peer.get('agent_kind')}, "
                        f"expected {harness.agent_kind}; nothing was sent",
                    }
                if peer.get("agent_identity_source") != "foreground_argv":
                    reason = "agent identity is reported, not the foreground argv"
                elif peer.get("send_ready") is True:
                    detail = await self.terminals.get(address)
                    if (
                        detail is None
                        or type(detail.get("created_ms")) is not int
                        or detail["created_ms"] != created_ms
                    ):
                        return {
                            "status": "identity_mismatch",
                            "reason": "terminal instance changed or could not be verified; "
                            "nothing was sent",
                        }
                    return {"status": "ready", "peer": peer}
                else:
                    reason = str(peer.get("send_readiness_reason"))
            if self.now() >= deadline:
                break
            await self.sleep(self.poll_seconds)
        try:
            screen = (await self.bridge.call("hc_read", {"target": address, "lines": 40}))["text"]
        except (BridgeError, KeyError):
            screen = None
        return {
            "status": "not_ready",
            "reason": f"{reason} after {self.policy.prime_timeout_s:g} s; inspect the screen "
            "(approval prompt or sign-in?), do not resend",
            "screen": screen[-2000:] if isinstance(screen, str) else None,
        }

    async def _receipt(self, worker: Worker) -> tuple[str, float | None]:
        """The receipt status that decides whether this worker may be removed, and its time.

        The registry's receipt_id is only what `route` last recorded. A brief sent by hand,
        or by a route whose registry write was lost, leaves a newer receipt the row does not
        name, so the bridge is asked for the peer's receipts on every call. An unfinished
        receipt for the address wins over any finished one; a status this code does not
        know, or a list that cannot be read, is "unknown", which gc keeps. So is a miss in a
        full list: it holds only the newest receipts, and an older unfinished one may sit
        outside it. Receipt and worker timestamps come from different clocks, so they are
        not used to narrow that; only a list shorter than the window proves "none".
        """
        entry = worker.entry or {}
        try:
            listed = await self.bridge.call("task_list", {"query": "", "limit": RECEIPT_WINDOW})
            rows = listed["tasks"]
            if not isinstance(rows, list):
                return "unknown", None
            mine = []
            for row in rows:
                target = row.get("target") if isinstance(row, dict) else None
                if not isinstance(target, str):
                    return "unknown", None
                if target.startswith("hc:") and target.rsplit(":", 1)[-1] == worker.address:
                    mine.append(row)
            # The list holds the latest 100 receipts; the recorded one may be older.
            if entry.get("receipt_id") and all(
                row.get("id") != entry["receipt_id"] for row in mine
            ):
                mine.append(await self.bridge.call("task_get", {"task_id": entry["receipt_id"]}))
            if not mine:
                return ("none" if len(rows) < RECEIPT_WINDOW else "unknown"), None
            for row in mine:
                if row["status"] not in TERMINAL:
                    status = str(row["status"])
                    return (status if status in ACTIVE_RECEIPTS else "unknown"), None
            recorded = [row for row in mine if row.get("id") == entry.get("receipt_id")]
            latest = (recorded or mine)[0]
            updated = latest.get("updated")
            return str(latest["status"]), float(updated) if updated else None
        except (BridgeError, KeyError, ValueError, TypeError, AttributeError):
            return "unknown", None

    async def _facts(self, worker: Worker, peers: Mapping[str, dict[str, Any]]) -> Facts:
        entry = worker.entry or {}
        since = float(entry.get("created_at") or worker.created_ms / 1000)
        receipt, updated = await self._receipt(worker)
        if updated is not None:
            since = updated
        task_id = entry.get("task")
        handoff = self.handoffs.state(task_id) if isinstance(task_id, str) else "open"
        reusable = peers.get(worker.address, {}).get("send_ready") is True
        return Facts(worker, receipt, since, handoff, reusable)

    async def _teardown(self, worker: Worker) -> str:
        detail = await self.terminals.get(worker.address)
        if detail is None or detail.get("state") != "live":
            self.registry.drop(worker.address)
            return "already gone"
        current = self._managed(detail, dict(worker.entry) if worker.entry else None)
        if current is None or current.created_ms != worker.created_ms:
            return "skipped: the terminal at this address changed"
        if current.state in self.policy.never_kill.states:
            return f"skipped: state became {current.state}"
        # Asked again here: a brief can arrive between the plan and this call while the
        # agent still reads idle.
        receipt, _ = await self._receipt(current)
        if (
            receipt in ACTIVE_RECEIPTS
            or receipt == "unknown"
            or receipt in self.policy.never_kill.receipts
        ):
            return f"skipped: receipt became {receipt}"
        tab = current.labels.get("herdr.tab")
        closed = False
        if tab is not None and worker.entry is not None:
            try:
                # Closing the last tab of a space closes the space, and the close is sent
                # confirmed. That tab is left; only the worker's terminal is ended.
                alone = await self.layout.tab_terminals(tab) == [worker.address]
                if alone and len(await self.layout.space_tabs(tab) or []) > 1:
                    await self.layout.tab_close(tab)
                    closed = True
            except LayoutError:
                closed = False
        if not closed:
            await self.terminals.kill(worker.address)
            try:
                await self.terminals.remove(worker.address)
            except RoutingError:
                pass  # confirmed (or not) by the read below
        after = await self.terminals.get(worker.address)
        if after is not None and after.get("state") == "live":
            return "unconfirmed: still live after teardown; inspect it"
        self.registry.drop(worker.address)
        return "removed (tab closed)" if closed else "removed (hc kill, hc rm)"

    async def gc(self, *, apply: bool) -> dict[str, Any]:
        workers = await self.workers()
        try:
            peers = await self._hc_peers()
        except BridgeError:
            peers = {}
        queued = Counter(
            found.name
            for found in (classify(self.policy, text) for text in self.ledger.queued())
            if found.name is not None
        )
        facts = [await self._facts(worker, peers) for worker in workers]
        verdicts, deficit = plan_gc(self.policy, facts, queued, self.now())
        report = []
        for verdict in verdicts:
            item = verdict.public()
            if apply and verdict.action == "remove":
                item["outcome"] = await self._teardown(verdict.worker)
            report.append(item)
        if apply:
            live = {worker.address: worker.created_ms for worker in workers}
            stale = [
                address
                for address, row in self.registry.load().items()
                if row.get("project") == self.policy.project
                and live.get(address) != row.get("created_ms")
            ]
            for address in stale:
                self.registry.drop(address)
        return {
            "status": "applied" if apply else "planned",
            "workers": report,
            "remove": sum(1 for verdict in verdicts if verdict.action == "remove"),
            "keep": sum(1 for verdict in verdicts if verdict.action == "keep"),
            "queued_by_class": dict(queued),
            "warm_pool_deficit": deficit,
            "gates": {
                "cap": self.policy.max_concurrent_gates,
                "classes": sorted(self.policy.gate_classes),
                "holders": await self.gate_holders(),
            },
            # Lifecycle text the policy file carries and no code here acts on.
            "unenforced_lifecycle": list(self.policy.unenforced),
            "note": "owner-opened terminals are never candidates and are not listed",
        }

    async def listing(self) -> dict[str, Any]:
        plan = await self.gc(apply=False)
        keys = ("workers", "queued_by_class", "gates", "unenforced_lifecycle")
        return {"status": "listed", **{key: plan[key] for key in keys}}
