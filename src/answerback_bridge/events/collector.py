"""The observer loop. It reads the receipt store, the peer scanner and board files, and
publishes what changed. It holds no dispatch slot, takes no admission lock and writes only
observer state (the hub and a temporary lifecycle index), never receipts or dispatch state.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..plugin import by_name, merged
from ..pressure.settings import PressureSettings
from ..pressure.signatures import harness_of_peer, match_lines
from ..pressure.store import publish_section
from . import model
from .debug import RESOURCE_SECONDS, TAIL_SECONDS, safe_raw
from .history import RECEIPT_COLUMNS
from .hub import EMPTY_METRICS, EventHub
from .patch import diff, escape
from .producers import (
    BOARD_OMISSIONS,
    MAX_PEERS,
    TAIL_ACTIVITY,
    RunTracker,
    bounded_identity,
    overdue_interrupts,
    peers_section,
    read_board,
    tail_content,
)
from .tracing import Tracer, TraceSettings

DEMAND_SECONDS = 120
PAGE = 500  # Receipt rows per read.
MAX_PAGES = 20  # Receipt reads per pass; a longer backlog continues on the next pass.
# Each pass re-reads this much before the newest receipt seen: an update is stamped before it
# is committed, so it can become visible after a later-stamped row was already read.
OVERLAP_SECONDS = 5.0
SWEEP_SECONDS = 60  # How often runs are checked against what the store still retains.
MAX_RECEIPTS = 50_000
PRESSURE_SEEN = 4096  # Matched screen lines remembered, so an unchanged screen signals once.
PRESSURE_SCAN_CHARS = 16_384  # The end of a screen that is matched for rate-limit lines.


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    receipt_seconds: float = 1.0
    scan_seconds: float = 15.0
    history: int = 2000
    projects: tuple[str, ...] = ()
    tracing: TraceSettings = field(default_factory=TraceSettings)
    pressure: PressureSettings = field(default_factory=PressureSettings)

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("events.enabled must be true or false")
        for name, low, high in (("receipt_seconds", 0.2, 60), ("scan_seconds", 2, 3600)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f"events.{name} must be {low}–{high} seconds")
        if type(self.history) is not int or not 16 <= self.history <= 100_000:
            raise ValueError("events.history must be 16–100000 events")
        if len(self.projects) > 8 or any(
            not isinstance(path, str) or not Path(path).is_absolute() for path in self.projects
        ):
            raise ValueError("events.projects must be at most 8 absolute paths")

    @classmethod
    def load(cls, config_path: Path) -> "Settings":
        """Optional `events.json` beside the bridge config. Absent means defaults."""
        path = config_path.expanduser().resolve().parent / "events.json"
        try:
            values = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("events.json has unknown settings")
        if "projects" in values:
            if not isinstance(values["projects"], list):
                raise ValueError("events.projects must be a list")
            values["projects"] = tuple(values["projects"])
        if "tracing" in values:
            if not isinstance(values["tracing"], dict) or (
                set(values["tracing"]) - set(TraceSettings.__dataclass_fields__)
            ):
                raise ValueError("events.tracing must be an object with known settings")
            values["tracing"] = TraceSettings(**values["tracing"])
        if "pressure" in values:
            values["pressure"] = PressureSettings.from_json(values["pressure"])
        return cls(**values)


# A position in the receipt store: the `updated` stamp and ID of the last row read. The ID
# breaks ties, so any number of rows sharing one stamp is walked exactly once.
Cursor = tuple[float, str]
Receipts = Callable[[Cursor | None], Awaitable[list[dict[str, Any]]]]
BusRows = tuple[list[dict[str, Any]], int | None]


async def _no_resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {}


@dataclass
class Sources:
    """Every input is a callable, so a test scripts receipts and peers without a bridge."""

    receipts: Receipts
    peers: Callable[[], Awaitable[dict[str, Any]]]
    tail: Callable[[str, dict[str, Any]], Awaitable[str | None]]
    board: Callable[[list[str]], Awaitable[dict[str, Any]]]
    bus: Callable[[int | None], Awaitable[BusRows]]
    resources: Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]] = _no_resources
    # The IDs of every retained receipt, or None when that cannot be told. Keyword-only, so
    # the positional order of the sources above is the same for every caller.
    retained: Callable[[], Awaitable[set[str] | None]] | None = field(default=None, kw_only=True)
    # The stored brief of a receipt, so a screen line that quotes it is not a signal.
    brief: Callable[[str], Awaitable[str | None]] | None = field(default=None, kw_only=True)
    # The provider pressure store (`answerback_bridge.pressure.PressureStore`) screen
    # matches are signalled to; None scans nothing.
    pressure: Any = field(default=None, kw_only=True)


async def _no_tail(peer_id: str, peer: dict[str, Any]) -> str | None:
    return None


async def _no_board(paths: list[str]) -> dict[str, Any]:
    return {"decisions": {}, "applyQueue": {}, "heads": {}}


async def _no_bus(cursor: int | None) -> BusRows:
    return [], cursor


def scripted(receipts: Receipts, peers: Callable[[], Awaitable[dict[str, Any]]]) -> Sources:
    return Sources(receipts, peers, _no_tail, _no_board, _no_bus)


@dataclass
class Collector:
    hub: EventHub
    sources: Sources
    settings: Settings = field(default_factory=Settings)
    instance_id: str = ""
    settings_error: str | None = None
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.tracker = RunTracker()
        self.tracer = Tracer(self.settings.tracing)
        self.cursor: Cursor | None = None
        self.newest: float | None = None
        self.backfilled = False
        self.next_sweep = 0.0
        self.bus_cursor: int | None = None
        self.partial: set[str] = set()
        self.interrupted: set[str] = set()
        self.errors: dict[str, str] = {}
        if self.settings_error:
            self.errors["settings"] = self.settings_error  # Defaults are in use.
        self.raw_peers: list[dict[str, Any]] = []
        self.resource_due: dict[str, float] = {}
        self.tail_due: dict[str, float] = {}
        self.next_scan = 0.0
        self.debug_generation = -1
        self.debug_deadlines: dict[str, float | None] = {}
        self.pressure_seen: OrderedDict[tuple[str, str | None, str, int], None] = OrderedDict()
        self.started = time.time()
        self.hub.set_section(
            "bridge",
            {
                "version": __version__,
                "instanceId": self.instance_id,
                "startedMs": model.now_ms(),
                "sourceErrors": {},
                "runsOmitted": 0,
                "runsPageable": self.hub.receipt_path is not None,
            },
        )
        if self.pressure is not None:
            publish_section(self.hub, self.pressure)

    @property
    def pressure(self) -> Any:
        return self.sources.pressure

    async def _guard(self, name: str, call: Awaitable[Any]) -> Any:
        """A failing source is reported in the state and skipped; it never stops the loop."""
        try:
            value = await call
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors[name] = type(error).__name__
            return None
        self.errors.pop(name, None)
        return value

    def _publish_errors(self) -> None:
        self.hub.set_section(
            "bridge",
            {
                **self.hub.state["bridge"],
                "sourceErrors": dict(self.errors),
                "runsOmitted": self.tracker.omitted,
                "runsPageable": self.hub.receipt_path is not None,
            },
        )

    async def tick_fast(self) -> None:
        """Receipts: lifecycle events first, then the state they imply.

        The first reads walk everything the store retains, so the board holds finished and
        failed receipts from before this bridge started; only what is new is announced."""
        for _ in range(MAX_PAGES):
            rows = await self._guard("receipts", self.sources.receipts(self.cursor))
            if rows is None:
                break
            for event in self.tracker.observe(rows, backfill=not self.backfilled):
                self.hub.publish(event)
            if rows:
                last = max(rows, key=lambda row: (row["updated"], row["id"]))
                self.cursor = (last["updated"], last["id"])
                self.newest = max(self.newest or last["updated"], last["updated"])
            if len(rows) < PAGE:
                # Drained. The next pass starts a little before the newest receipt seen (or
                # before this start, for an empty store), so a receipt that opens and closes
                # between two passes, or commits late, is still seen. Re-read rows change
                # nothing: the tracker announces a status once.
                self.backfilled = True
                base = self.started if self.newest is None else self.newest
                self.cursor = (base - OVERLAP_SECONDS, "")
                break
        await self._sweep()
        for event in self.tracker.attach(self.hub.state["peers"], self.partial):
            self.hub.publish(event)
        self.hub.set_section("runs", self.tracker.section())
        self._publish_errors()

    async def _sweep(self) -> None:
        """Runs leave the board when retention deletes their receipt, and only then."""
        if (
            (self.sources.retained is None and self.hub.receipt_path is None)
            or not self.backfilled
            or time.monotonic() < self.next_sweep
        ):
            return
        self.next_sweep = time.monotonic() + SWEEP_SECONDS
        if self.hub.receipt_path is not None:
            await self._guard(
                "retention", asyncio.to_thread(self.tracker.retain_store, self.hub.receipt_path)
            )
            return
        # Read after the receipts above: a run the tracker holds was in the store before this.
        assert self.sources.retained is not None
        receipts = await self._guard("retention", self.sources.retained())
        if isinstance(receipts, set):
            self.tracker.retain(receipts)

    async def tick_slow(self) -> None:
        """Peers, board files, tails and bus rows: the sources that cost a subprocess or IO."""
        self._decay_pressure()
        scan = await self._guard("peers", self.sources.peers())
        fresh: set[str] = set()
        if isinstance(scan, dict):
            section, self.partial = peers_section(
                scan, self.hub.state["peers"], self.tracker.open_by_peer()
            )
            rows = scan.get("peers")
            omitted = sum(
                1
                for row in (rows if isinstance(rows, list) else [])[:MAX_PEERS]
                if isinstance(row, dict)
                and isinstance(ident := row.get("peer_id"), str)
                and ident
                and not bounded_identity(ident)
            )
            if omitted:
                self.errors["peerIdentifiers"] = f"{omitted} peer identifiers omitted"
            else:
                self.errors.pop("peerIdentifiers", None)
            for event in self.tracker.attach(section, self.partial):
                self.hub.publish(event)
            self.hub.set_section("peers", section)
            self.hub.set_section("runs", self.tracker.section())
            fresh = {
                row["peer_id"]
                for row in scan.get("peers", [])
                if isinstance(row, dict) and row.get("peer_id") in section
            }
        peers = self.hub.state["peers"]
        self.raw_peers = (
            [
                row
                for row in (scan or {}).get("peers", [])
                if isinstance(row, dict) and row.get("peer_id") in fresh
            ]
            if isinstance(scan, dict)
            else []
        )
        selected = [
            row
            for row in self.raw_peers
            if (self.settings.tracing.enabled and not self._debugging(row["peer_id"]))
            or (
                self._debugging(row["peer_id"])
                and self.monotonic() >= self.resource_due.get(row["peer_id"], 0)
            )
        ]
        if selected:
            await self._resources(selected)
        for ident in peers.keys() - fresh:
            self.hub.patch_state(
                diff(
                    peers[ident]["metrics"], dict(EMPTY_METRICS), f"/peers/{escape(ident)}/metrics"
                )
            )
        paths = [*self.settings.projects, *(peer["project"] for peer in peers.values())]
        board = await self._guard("board", self.sources.board([path for path in paths if path]))
        if isinstance(board, dict):
            omissions = board.get("omissions", {})
            for kind, description in BOARD_OMISSIONS.items():
                count = omissions.get(kind, 0)
                if count:
                    self.errors[kind] = f"{count} {description} omitted"
                else:
                    self.errors.pop(kind, None)
            # A decision leaving the visible window is forgotten and may be announced
            # again if it returns. Keep memory bounded by the current board.
            self.interrupted.intersection_update(board["decisions"])
            for event in overdue_interrupts(board["decisions"], self.interrupted):
                self.hub.publish(event)
            for key in ("decisions", "applyQueue", "heads"):
                self.hub.set_section(key, board[key])
        hashes = await self._tails(fresh)
        if self.settings.tracing.enabled:
            # Provider reads can take seconds. Re-read receipts before detection so a
            # completion observed during those reads cannot become an undelivered alert.
            await self.tick_fast()
            self.tracer.observe(
                self.hub,
                self.tracker.runs,
                hashes,
                fresh if "receipts" not in self.errors else set(),
                self.clock(),
                receipts_fresh="receipts" not in self.errors,
            )
        rows = await self._guard("bus", self.sources.bus(self.bus_cursor))
        if rows is not None:
            events, self.bus_cursor = rows
            for row in events:
                self.hub.publish(model.raw(safe_raw(row), "answerback.bus"))
        self._publish_errors()

    def _decay_pressure(self) -> None:
        if self.pressure is None:
            return
        try:
            changed = self.pressure.decay()
        except Exception as error:
            self.errors["pressure"] = type(error).__name__
            return
        self.errors.pop("pressure", None)
        if changed or "pressure" not in self.hub.state:
            publish_section(self.hub, self.pressure)

    async def _brief(self, run: str | None) -> str | None:
        if run is None or self.sources.brief is None:
            return None
        brief = await self._guard("brief", self.sources.brief(run))
        return brief if isinstance(brief, str) else None

    async def _scan_pressure(self, ident: str, run: str | None, text: str) -> None:
        """Rate-limit lines on a terminal screen become signals. Only `Match` records (a
        class and a line digest) leave here; the text is never kept or published."""
        store = self.pressure
        if store is None or not ident.startswith("hc:"):
            return
        peer = self.hub.state["peers"].get(ident) or {}
        harness = harness_of_peer({"provider": "hc", "agent_kind": peer.get("agentKind")})
        text = text[-PRESSURE_SCAN_CHARS:]
        hints = store.settings.hints()
        matches = match_lines(text, harness=harness, source="hc_screen", account_hint=hints)
        if not matches:
            return
        # Seen before is by line and occurrence: an unchanged screen is one signal, and a
        # second copy of the same message on the screen is a second signal.
        occurrence: Counter[str] = Counter()
        fresh = []
        for match in matches:
            key = (ident, run, match.line_digest, occurrence[match.line_digest])
            occurrence[match.line_digest] += 1
            if key not in self.pressure_seen:
                fresh.append((key, match))
        if not fresh:
            return
        brief = await self._brief(run)
        allowed = (
            {match.line_digest for match in matches}
            if brief is None
            else {
                match.line_digest
                for match in match_lines(
                    text, exclude=brief, harness=harness, source="hc_screen", account_hint=hints
                )
            }
        )
        for key, match in fresh:
            self.pressure_seen[key] = None
            while len(self.pressure_seen) > PRESSURE_SEEN:
                self.pressure_seen.popitem(last=False)
            if match.line_digest in allowed:
                store.signal(match)

    def _debugging(self, ident: str) -> bool:
        return self.hub.debug is not None and self.hub.debug.until(ident) is not None

    async def _resources(self, rows: list[dict[str, Any]]) -> None:
        debugging = {row["peer_id"] for row in rows if self._debugging(row["peer_id"])}
        for row in rows:
            self.resource_due[row["peer_id"]] = self.monotonic() + RESOURCE_SECONDS
        measured = await self._guard("resources", self.sources.resources(rows))
        for row in rows:
            ident = row["peer_id"]
            # A provider read can outlast the lease. Discard its late debug result.
            if ident in debugging and not self._debugging(ident):
                continue
            before = self.hub.state["peers"].get(ident)
            if before is not None:
                metrics = {**EMPTY_METRICS, **(measured or {}).get(ident, {})}
                self.hub.patch_state(
                    diff(before["metrics"], metrics, f"/peers/{escape(ident)}/metrics")
                )

    async def _tails(self, fresh: set[str], *, debug_only: bool = False) -> dict[str, str]:
        peers = self.hub.state["peers"]
        open_runs = self.tracker.open_by_peer()
        working = [
            (ident, open_runs.get(ident))
            for ident, peer in peers.items()
            if ident in open_runs
            or (self.settings.tracing.enabled and peer["state"] == "working")
            or self._debugging(ident)
        ]
        hashes: dict[str, str] = {}
        semaphore = asyncio.Semaphore(self.settings.tracing.probe_concurrency)

        async def bounded_tail(ident: str) -> str | None:
            async with semaphore:
                async with asyncio.timeout(self.settings.tracing.source_timeout_seconds):
                    return await self.sources.tail(ident, peers[ident])

        async def collect_tail(ident: str, run: str | None) -> None:
            debugging = self._debugging(ident)
            if ident not in fresh or (debug_only and not debugging):
                return
            if debugging and self.monotonic() < self.tail_due.get(ident, 0):
                return
            self.tail_due[ident] = self.monotonic() + TAIL_SECONDS
            text = await self._guard(f"tail:{ident}", bounded_tail(ident))
            if debugging and not self._debugging(ident):
                return
            if isinstance(text, str):
                await self._scan_pressure(ident, run, text)
                # The text stops here: it is digested in this process and never published.
                known = self.hub.activities.get(f"tail:{ident}")
                content = tail_content(ident, run, text, previous=known[1] if known else None)
                # Activity metadata covers the returned tail; detection compares a bounded suffix.
                text = text[-self.settings.tracing.tail_bytes :]
                text = text.encode("utf-8", "replace")[-self.settings.tracing.tail_bytes :].decode(
                    "utf-8", "ignore"
                )
                hashes[ident] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                self.hub.set_activity(
                    f"tail:{ident}",
                    TAIL_ACTIVITY,
                    content,
                )

        await asyncio.gather(*(collect_tail(ident, run) for ident, run in working))
        for key in list(self.errors):
            if key.startswith("tail:") and key[5:] not in peers:
                del self.errors[key]
        live = {f"tail:{ident}" for ident, _ in working}
        if not debug_only:
            # Remembered lines go with their run: a peer that stops working, or starts
            # another run, is matched afresh.
            scanned = set(working)
            for key in [key for key in self.pressure_seen if (key[0], key[1]) not in scanned]:
                del self.pressure_seen[key]
        for message_id in [ident for ident in self.hub.activities if ident not in live]:
            self.hub.drop_activity(message_id)
        return hashes

    async def tick(self) -> None:
        """One full pass, in the order the loop produces: receipts, scan, receipts."""
        await self.tick_fast()
        await self.tick_slow()

    async def poll(self) -> None:
        """One scheduled pass; monotonic cadence, wall-clock lease expiry."""
        debug = self.hub.debug
        active = False
        if debug is not None:
            status = debug.status(self.hub.state["peers"])
            active = bool(status["scopes"])
            if debug.generation != self.debug_generation:
                for ident, until in status["peers"].items():
                    if until != self.debug_deadlines.get(ident):
                        self.resource_due.pop(ident, None)
                        self.tail_due.pop(ident, None)
                if active and (
                    not self.raw_peers
                    or set(status["peers"]) - {row["peer_id"] for row in self.raw_peers}
                ):
                    self.next_scan = 0.0
                self.debug_generation = debug.generation
            self.hub.set_section("bridge", {**self.hub.state["bridge"], "debug": status})
        await self.tick_fast()
        if self.monotonic() >= self.next_scan and (
            active or self.settings.tracing.enabled or self.hub.demand(DEMAND_SECONDS)
        ):
            self.next_scan = self.monotonic() + self.settings.scan_seconds
            await self.tick_slow()
            present = set(self.hub.state["peers"])
            self.resource_due = {k: v for k, v in self.resource_due.items() if k in present}
            self.tail_due = {k: v for k, v in self.tail_due.items() if k in present}
        if active:
            selected = (
                [
                    row
                    for row in self.raw_peers
                    if self._debugging(row["peer_id"])
                    and self.monotonic() >= self.resource_due.get(row["peer_id"], 0)
                ]
                if debug is not None
                else []
            )
            if selected:
                await self._resources(selected)
            await self._tails({row["peer_id"] for row in self.raw_peers}, debug_only=True)
            # RAW events are drained during debug too, not just at the ordinary scan.
            rows = await self._guard("bus", self.sources.bus(self.bus_cursor))
            if rows is not None:
                events, self.bus_cursor = rows
                for row in events:
                    self.hub.publish(model.raw(safe_raw(row), "answerback.bus"))
        if debug is not None:
            status = debug.status(self.hub.state["peers"])
            self.debug_deadlines = status["peers"]
            self.hub.set_section("bridge", {**self.hub.state["bridge"], "debug": status})
        self._publish_errors()

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.poll()
                self.errors.pop("collector", None)
            except Exception as error:
                self.errors["collector"] = type(error).__name__
            try:
                # Wake controls/expiry even when receipt_seconds is configured to 60.
                await asyncio.wait_for(stopping.wait(), min(self.settings.receipt_seconds, 1.0))
            except TimeoutError:
                pass


def read_receipts(path: Path, cursor: Cursor | None) -> list[dict[str, Any]]:
    """One page of receipt rows after `cursor` in (updated, id) order, through a read-only
    connection; from the oldest retained receipt when `cursor` is None. No bodies and no
    detail: the brief's first characters are read only to find its ledger task, and neither
    the result nor the detail text is selected at all."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(
            f"SELECT {RECEIPT_COLUMNS} FROM tasks "
            + ("" if cursor is None else "WHERE updated>? OR (updated=? AND id>?) ")
            + f"ORDER BY updated, id LIMIT {PAGE}",
            () if cursor is None else (cursor[0], cursor[0], cursor[1]),
        ).fetchall()
    return [dict(row) for row in rows]


def read_receipt_ids(path: Path) -> set[str] | None:
    """The IDs of every retained receipt; None when there are too many to hold."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT id FROM tasks LIMIT ?", (MAX_RECEIPTS + 1,)).fetchall()
    return None if len(rows) > MAX_RECEIPTS else {row[0] for row in rows}


def bridge_sources(bridge: Any) -> Sources:
    """The live inputs. Each one is a read the bridge already offers to any local client."""

    from .trace_sources import LiveResources

    # Receipt-only and tail-only adapters need no event configuration.
    settings = getattr(bridge, "event_settings", None)
    resources = LiveResources(bridge, settings.tracing if settings is not None else TraceSettings())

    async def receipts(cursor: Cursor | None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(read_receipts, bridge.store.path, cursor)

    async def retained() -> set[str] | None:
        return await asyncio.to_thread(read_receipt_ids, bridge.store.path)

    async def peers() -> dict[str, Any]:
        return await bridge.peer_search()

    # One tail source per provider, from the plugins the bridge composed; a provider two
    # plugins claim is a startup error.
    tails = merged(
        "event source",
        {},
        [
            (plugin.definition.registration, {s.provider: s for s in plugin.event_sources()})
            for plugin in by_name(getattr(bridge, "plugins", ()))
        ],
    )

    async def tail(peer_id: str, peer: dict[str, Any]) -> str | None:
        # Screen and chat text, prompts and results included. It is an input to a digest
        # (`tail_content`) and must never be put on the stream itself.
        provider, _, target = peer_id.partition(":")
        source = tails.get(provider)
        if source is None:
            return None
        return await source.tail(target, peer, resources)

    async def board(paths: list[str]) -> dict[str, Any]:
        return await asyncio.to_thread(read_board, paths)

    async def brief(run: str) -> str | None:
        # In process only: the brief is compared with screen lines and never published.
        def read() -> str | None:
            try:
                message = bridge.store.get(run).get("message")
            except (KeyError, ValueError):
                return None
            return message if isinstance(message, str) else None

        return await asyncio.to_thread(read)

    async def bus(cursor: int | None) -> BusRows:
        from ..bus.monitor import Monitor

        def read() -> BusRows:
            try:
                frame = Monitor(bridge.config_path)._read(cursor, 1 if cursor is None else 200)
            except FileNotFoundError:
                return [], cursor  # The bus is opt-in.
            # The first read only finds the end of the ledger: history is not replayed.
            if cursor is None:
                return [], frame["latest_cursor"]
            return frame["events"], frame["cursor"]

        return await asyncio.to_thread(read)

    return Sources(
        receipts,
        peers,
        tail,
        board,
        bus,
        resources,
        retained=retained,
        brief=brief,
        pressure=getattr(bridge, "pressure", None),
    )
