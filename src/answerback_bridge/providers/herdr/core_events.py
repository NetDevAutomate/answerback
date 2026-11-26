"""Follow Herdr Core's ``hc events --json`` stream into a bounded terminal view.

Interface used: the ``hc events --json`` CLI of hc 0.1.21 (Herdr, Apache-2.0),
read as one JSON value per line. Observed output (T-85 sample): the first line is
the bare ``events.subscribe`` result ``{"channel", "seq", "terminals"}``; later
lines are JSON-RPC 2.0 notifications ``{"jsonrpc", "method", "params"}`` whose
method is ``event`` (params ``seq``, ``type``, ``terminal``, ``data``),
``heartbeat`` (params ``seq``, every 15 s) or ``missed`` (a fresh snapshot for a
subscriber that fell behind). The ``event`` and ``missed`` shapes follow
``hc schema``; the sample contained no live event, so their ``data`` handling is
deliberately tolerant and resyncs when it cannot apply a message exactly.

``Core`` (``core.py``) owns one follower when the ``hc_events`` config switch is on
and lists peers from its snapshot; with the switch off (the default) nothing here runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from ...process import BridgeError, spawn_owned, stop_child

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TERMINALS = 256
MAX_TEXT = 4096
MAX_ARGV = 256
MAX_LABELS = 128
MAX_WAITERS = 256
_MAX_UINT = 2**64 - 1
_ADDRESS = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}/)?t[A-Za-z0-9]{1,127}\Z")
_SCRUBBED_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_SESSION")


class EventStreamError(BridgeError):
    """The event stream cannot be applied; the follower must resynchronise."""


class GapError(EventStreamError):
    """A message's sequence number skipped ahead, or it cannot be applied exactly."""


class ProtocolError(EventStreamError):
    """A line is malformed, oversized or not shaped like Core's event stream."""


class ViewLimitError(EventStreamError):
    """The stream would grow the view past its terminal cap."""


class FollowerStale(EventStreamError):
    """The follower lost its stream; callers should fall back to polling."""


@dataclass(frozen=True, slots=True)
class StatusRecord:
    """A program's own OSC 7501 status word, as Core reports it."""

    state: str
    app: str | None = None
    kind: str | None = None
    msg: str | None = None
    progress: int | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ForegroundRecord:
    """What runs in a terminal right now."""

    pid: int
    name: str
    argv: tuple[str, ...]
    cwd: str


@dataclass(frozen=True, slots=True)
class TerminalRecord:
    """One terminal's bounded state. ``truncated`` marks any shortened field.

    ``unverified`` marks a record that an event of a type this module does not
    model has touched since its last full record; its fields may be out of date.
    """

    address: str
    state: str
    argv: tuple[str, ...] = ()
    cwd: str = ""
    labels: Mapping[str, str] = MappingProxyType({})
    title: str = ""
    turn: int = 0
    status: StatusRecord | None = None
    status_ms: int | None = None
    foreground: ForegroundRecord | None = None
    name: str | None = None
    parent: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    reason: str | None = None
    keep: bool = False
    created_ms: int | None = None
    finished_ms: int | None = None
    cols: int | None = None
    rows: int | None = None
    truncated: bool = False
    unverified: bool = False


@dataclass(frozen=True, slots=True)
class Change:
    """What one applied message did: its kind and the terminal it touched."""

    kind: str
    address: str | None = None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_line(line: bytes, *, max_bytes: int = MAX_LINE_BYTES) -> dict[str, Any]:
    """Decode one stream line into a JSON object, rejecting anything unbounded."""
    if len(line) > max_bytes:
        raise ProtocolError("Event line exceeds its size bound")
    try:
        value = json.loads(
            line.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (ValueError, RecursionError):
        raise ProtocolError("Event line is not valid JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError("Event line is not a JSON object")
    return value


class _Bounds:
    def __init__(self, max_text: int, max_argv: int, max_labels: int) -> None:
        self.max_text = max_text
        self.max_argv = max_argv
        self.max_labels = max_labels
        self.truncated = False

    def text(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ProtocolError(f"Core event field {field} is not text")
        if len(value) > self.max_text:
            self.truncated = True
            return value[: self.max_text]
        return value

    def optional_text(self, value: Any, field: str) -> str | None:
        return None if value is None else self.text(value, field)

    def argv(self, value: Any, field: str) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise ProtocolError(f"Core event field {field} is not a list")
        if len(value) > self.max_argv:
            self.truncated = True
            value = value[: self.max_argv]
        return tuple(self.text(item, field) for item in value)

    def labels(self, value: Any) -> Mapping[str, str]:
        if not isinstance(value, dict):
            raise ProtocolError("Core event labels are not an object")
        items = list(value.items())
        if len(items) > self.max_labels:
            self.truncated = True
            items = items[: self.max_labels]
        return MappingProxyType(
            {self.text(key, "labels"): self.text(item, "labels") for key, item in items}
        )

    def status(self, value: Any) -> StatusRecord | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ProtocolError("Core event status is not an object")
        return StatusRecord(
            state=self.text(value.get("state"), "status.state"),
            app=self.optional_text(value.get("app"), "status.app"),
            kind=self.optional_text(value.get("kind"), "status.kind"),
            msg=self.optional_text(value.get("msg"), "status.msg"),
            progress=_optional_int(value.get("progress"), "status.progress", 0, 255),
            title=self.optional_text(value.get("title"), "status.title"),
        )

    def foreground(self, value: Any) -> ForegroundRecord | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ProtocolError("Core event foreground is not an object")
        return ForegroundRecord(
            pid=_int(value.get("pid"), "foreground.pid", -(2**31), 2**31 - 1),
            name=self.text(value.get("name", ""), "foreground.name"),
            argv=self.argv(value.get("argv", []), "foreground.argv"),
            cwd=self.text(value.get("cwd", ""), "foreground.cwd"),
        )


def _int(value: Any, field: str, low: int = 0, high: int = _MAX_UINT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProtocolError(f"Core event field {field} is not a bounded integer")
    return value


def _optional_int(value: Any, field: str, low: int = 0, high: int = _MAX_UINT) -> int | None:
    return None if value is None else _int(value, field, low, high)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"Core event field {field} is not a boolean")
    return value


def _address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ProtocolError("Core event carries an invalid terminal address")
    return value


_I32 = (-(2**31), 2**31 - 1)
_FIELDS: dict[str, Callable[[_Bounds, Any], Any]] = {
    "state": lambda b, v: b.text(v, "state"),
    "argv": lambda b, v: b.argv(v, "argv"),
    "cwd": lambda b, v: b.text(v, "cwd"),
    "labels": lambda b, v: b.labels(v),
    "title": lambda b, v: b.text(v, "title"),
    "turn": lambda b, v: _int(v, "turn"),
    "status": lambda b, v: b.status(v),
    "status_ms": lambda b, v: _optional_int(v, "status_ms"),
    "foreground": lambda b, v: b.foreground(v),
    "name": lambda b, v: b.optional_text(v, "name"),
    "parent": lambda b, v: b.optional_text(v, "parent"),
    "pid": lambda b, v: _optional_int(v, "pid", 0, 2**32 - 1),
    "exit_code": lambda b, v: _optional_int(v, "exit_code", *_I32),
    "signal": lambda b, v: _optional_int(v, "signal", *_I32),
    "reason": lambda b, v: b.optional_text(v, "reason"),
    "keep": lambda b, v: _bool(v, "keep"),
    "created_ms": lambda b, v: _optional_int(v, "created_ms"),
    "finished_ms": lambda b, v: _optional_int(v, "finished_ms"),
    "cols": lambda b, v: _optional_int(v, "cols", 0, 65535),
    "rows": lambda b, v: _optional_int(v, "rows", 0, 65535),
}
# Event types whose whole ``data`` may be the bare new value of one field.
_SCALAR_EVENTS = {"status": "status", "turn": "turn", "title": "title", "keep": "keep"}
# The only record fields each patch event may change. Every other key in its
# ``data`` (``address``, ``state``, ``cwd``, ...) is ignored, so a patch can never
# be promoted into a replacement of the record or rename it.
_PATCH_FIELDS: dict[str, frozenset[str]] = {
    "status": frozenset({"status", "status_ms"}),
    "turn": frozenset({"turn"}),
    "title": frozenset({"title"}),
    "labels": frozenset({"labels"}),
    "keep": frozenset({"keep"}),
    "size": frozenset({"cols", "rows"}),
    "finished": frozenset({"exit_code", "signal", "reason", "finished_ms"}),
    "lost": frozenset({"reason"}),
}
# Fields an event forces regardless of its ``data``; these always win.
_FORCED: dict[str, dict[str, Any]] = {
    "finished": {"state": "finished", "foreground": None, "status": None},
}
_REPLACE_EVENTS = frozenset({"created", "restarted"})
# Event types known to change no terminal record, so they leave it trusted.
_NO_RECORD_EVENTS = frozenset({"doc"})


class TerminalView:
    """Pure, bounded model of Core's terminals, advanced one message at a time.

    It performs no I/O. ``apply`` raises an :class:`EventStreamError` subclass
    whenever the message cannot be applied exactly; the view is then left as it
    was before that message, and the owner should resubscribe.
    """

    def __init__(
        self,
        *,
        max_terminals: int = MAX_TERMINALS,
        max_text: int = MAX_TEXT,
        max_argv: int = MAX_ARGV,
        max_labels: int = MAX_LABELS,
    ) -> None:
        self.max_terminals = max_terminals
        self._limits = (max_text, max_argv, max_labels)
        self.seq: int | None = None
        self._terminals: dict[str, TerminalRecord] = {}

    @property
    def terminals(self) -> Mapping[str, TerminalRecord]:
        """A read-only view of the current records by address."""
        return MappingProxyType(self._terminals)

    @classmethod
    def parse_record(cls, row: Any, **limits: int) -> TerminalRecord:
        """Parse one Core ``Terminal`` object into a bounded record."""
        view = cls(**limits)
        return view._record(row)

    def _bounds(self) -> _Bounds:
        return _Bounds(*self._limits)

    def _record(self, row: Any) -> TerminalRecord:
        if not isinstance(row, dict):
            raise ProtocolError("Core terminal is not an object")
        bounds = self._bounds()
        values = {key: parse(bounds, row[key]) for key, parse in _FIELDS.items() if key in row}
        if "state" not in values:
            raise ProtocolError("Core terminal has no state")
        return TerminalRecord(
            address=_address(row.get("address")), truncated=bounds.truncated, **values
        )

    def _patch(
        self, record: TerminalRecord, data: dict[str, Any], allowed: frozenset[str]
    ) -> TerminalRecord:
        bounds = self._bounds()
        values = {key: _FIELDS[key](bounds, data[key]) for key in allowed if key in data}
        return replace(record, truncated=record.truncated or bounds.truncated, **values)

    def _replace_all(self, payload: Any) -> Change:
        if not isinstance(payload, dict):
            raise ProtocolError("Core snapshot is not an object")
        seq = _int(payload.get("seq"), "seq")
        rows = payload.get("terminals")
        if not isinstance(rows, list):
            raise ProtocolError("Core snapshot has no terminal list")
        if len(rows) > self.max_terminals:
            raise ViewLimitError("Core snapshot exceeds the terminal cap")
        terminals: dict[str, TerminalRecord] = {}
        for row in rows:
            record = self._record(row)
            if record.address in terminals:
                raise ProtocolError("Core snapshot repeats a terminal address")
            terminals[record.address] = record
        self.seq = seq
        self._terminals = terminals
        return Change("snapshot")

    def apply(self, message: Mapping[str, Any]) -> Change:
        """Apply one decoded stream message; see the module docstring for shapes."""
        method = message.get("method")
        if method is None and "terminals" in message:
            return self._replace_all(message)
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ProtocolError("Core event line has an unsupported shape")
        if method == "missed":
            if "terminals" not in params:
                raise GapError("Core reported missed events without a fresh snapshot")
            change = self._replace_all(params)
            return Change("missed", change.address)
        if self.seq is None:
            raise ProtocolError("Core event arrived before its snapshot")
        if method == "heartbeat":
            if _int(params.get("seq"), "seq") > self.seq:
                raise GapError("Core heartbeat is ahead of the last applied event (gap)")
            return Change("heartbeat")
        if method != "event":
            return Change("ignored")
        return self._event(params)

    def _event(self, params: dict[str, Any]) -> Change:
        assert self.seq is not None
        seq = _int(params.get("seq"), "seq")
        if seq <= self.seq:
            # Schema: after a missed snapshot, events not above its seq are ignored.
            return Change("ignored")
        if seq != self.seq + 1:
            raise GapError(f"Core event sequence gap: expected {self.seq + 1}, got {seq}")
        kind = params.get("type")
        if not isinstance(kind, str):
            raise ProtocolError("Core event has no type")
        address = _address(params.get("terminal"))
        data = params.get("data")
        if kind == "removed":
            self._terminals.pop(address, None)
        elif kind in _REPLACE_EVENTS:
            record = self._record(data)
            if record.address != address:
                raise ProtocolError("Core event record names a different terminal")
            if address not in self._terminals and len(self._terminals) >= self.max_terminals:
                raise ViewLimitError("Core event would exceed the terminal cap")
            self._terminals[address] = record
        elif kind in _PATCH_FIELDS:
            current = self._terminals.get(address)
            if current is None:
                raise GapError("Core event patches a terminal the view does not hold")
            self._terminals[address] = self._apply_patch(current, kind, data)
        elif kind not in _NO_RECORD_EVENTS:
            # An unknown type never changes a record's fields, whatever its data. It
            # may describe a change this view cannot apply, so the record stops being
            # trusted until a full record for it arrives. Flagging is once per record.
            current = self._terminals.get(address)
            if current is not None and not current.unverified:
                self._terminals[address] = replace(current, unverified=True)
        self.seq = seq
        return Change(kind, address)

    def _apply_patch(self, record: TerminalRecord, kind: str, data: Any) -> TerminalRecord:
        field = _SCALAR_EVENTS.get(kind)
        if kind == "labels" and isinstance(data, dict) and "labels" not in data:
            data = {"labels": data}
        elif field is not None and not (isinstance(data, dict) and field in data):
            data = {field: data}
        elif data is None:
            data = {}
        if not isinstance(data, dict):
            raise ProtocolError(f"Core {kind} event data is not an object")
        allowed = _PATCH_FIELDS[kind]
        forced = _FORCED.get(kind)
        if forced is not None:
            data = {**data, **forced}
            allowed = allowed | forced.keys()
        return self._patch(record, data, allowed)

    def clear(self) -> None:
        """Forget all state, as before the first snapshot."""
        self.seq = None
        self._terminals = {}


Sleep = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class _Waiter:
    address: str
    predicate: Callable[[TerminalRecord], bool]
    future: asyncio.Future[TerminalRecord]


class EventFollower:
    """Run ``hc events --json`` as a child and keep a :class:`TerminalView` current.

    Any gap, malformed line, EOF or child exit marks the view stale, stops the
    child and restarts it after an exponential, capped backoff. ``snapshot``
    returns ``None`` while stale so callers can fall back to polling.
    """

    def __init__(
        self,
        binary: str = "hc",
        *,
        session: str | None = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        stable_seconds: float = 60.0,
        max_line_bytes: int = MAX_LINE_BYTES,
        max_terminals: int = MAX_TERMINALS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise ValueError("Core executable is required")
        if session is not None and (not session or len(session) > 128 or "\x00" in session):
            raise ValueError("Core session must be a nonempty name")
        if not 0 < backoff_initial <= backoff_max:
            raise ValueError("Backoff must satisfy 0 < initial <= max")
        self.binary = binary
        self.session = session
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.stable_seconds = stable_seconds
        self.max_line_bytes = max_line_bytes
        self.view = TerminalView(max_terminals=max_terminals)
        self.restarts = 0
        self.last_error: str | None = None
        self._clock = clock
        self._sleep = sleep
        self._stale = True
        self._fresh_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self._waiters: dict[str, list[_Waiter]] = {}
        self._waiter_count = 0

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in _SCRUBBED_ENV:
            env.pop(key, None)
        if self.session is not None:
            env["HC_SESSION"] = self.session
        return env

    async def __aenter__(self) -> EventFollower:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start following in a background task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the follower, kill and reap its child, and mark the view stale."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        self._mark_stale("stopped")

    async def run(self) -> None:
        """Follow until cancelled, restarting the child with capped backoff."""
        failures = 0
        try:
            while True:
                started = self._clock()
                try:
                    reason = await self._session()
                except EventStreamError as error:
                    reason = str(error)
                except (OSError, BridgeError) as error:
                    reason = f"Core events unavailable: {type(error).__name__}"
                healthy = self._stale is False and self._clock() - started >= self.stable_seconds
                self._mark_stale(reason)
                if healthy:
                    failures = 0
                delay = min(self.backoff_max, self.backoff_initial * 2 ** min(failures, 60))
                failures += 1
                self.restarts += 1
                await self._sleep(delay)
        finally:
            self._mark_stale(self.last_error or "stopped")

    async def _session(self) -> str:
        proc = await spawn_owned(
            self.binary,
            "events",
            "--json",
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
            limit=self.max_line_bytes + 1,
        )
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    raise ProtocolError("Event line exceeds its size bound") from None
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise ProtocolError("Event stream ended inside a line")
                self._apply(parse_line(line, max_bytes=self.max_line_bytes))
            try:
                code = await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                return "Core events stream closed"
            return f"Core events exit {code}" if code else "Core events stream ended"
        finally:
            await stop_child(proc, cleanup_seconds=2)

    def _apply(self, message: Mapping[str, Any]) -> None:
        change = self.view.apply(message)
        self._fresh_at = self._clock()
        if change.kind in {"snapshot", "missed"}:
            self._stale = False
        if change.kind in {"status", "turn"} and change.address is not None:
            record = self.view.terminals.get(change.address)
            if record is not None:
                self._notify(record)

    def _notify(self, record: TerminalRecord) -> None:
        # Only this terminal's waiters are visited: cost does not grow with the
        # number of terminals or with waiters on other terminals.
        for waiter in list(self._waiters.get(record.address, ())):
            if waiter.future.done():
                continue
            try:
                matched = waiter.predicate(record)
            except Exception as error:  # A caller's predicate fails only its own wait.
                waiter.future.set_exception(error)
                continue
            if matched:
                waiter.future.set_result(record)

    def _mark_stale(self, reason: str) -> None:
        self._stale = True
        self.last_error = reason
        self.view.clear()
        waiters, self._waiters = self._waiters, {}
        self._waiter_count = 0
        for waiter in (item for group in waiters.values() for item in group):
            if not waiter.future.done():
                waiter.future.set_exception(FollowerStale(f"Core events stale: {reason}"))

    def snapshot(self, max_age: float) -> Mapping[str, TerminalRecord] | None:
        """Current records, or ``None`` when stale or older than ``max_age`` seconds."""
        if not max_age > 0:
            raise ValueError("max_age must be positive")
        if self._stale or self.view.seq is None or self._clock() - self._fresh_at > max_age:
            return None
        return MappingProxyType(dict(self.view.terminals))

    async def wait_for(
        self,
        address: str,
        predicate: Callable[[TerminalRecord], bool],
        timeout: float,  # noqa: ASYNC109 — the read API the wiring task consumes
    ) -> TerminalRecord:
        """Resolve on the next status or turn event for ``address`` matching ``predicate``.

        Raises ``TimeoutError`` after ``timeout`` seconds and :class:`FollowerStale`
        if the stream is, or becomes, stale while waiting.
        """
        if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
            raise ValueError("Core requires an exact tID or machine/tID terminal address")
        if self._stale:
            raise FollowerStale(f"Core events stale: {self.last_error or 'not started'}")
        if self._waiter_count >= MAX_WAITERS:
            raise EventStreamError("Too many concurrent Core event waiters")
        waiter = _Waiter(address, predicate, asyncio.get_running_loop().create_future())
        group = self._waiters.setdefault(address, [])
        group.append(waiter)
        self._waiter_count += 1
        try:
            return await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        finally:
            group = self._waiters.get(address)
            if group is not None and waiter in group:
                group.remove(waiter)
                self._waiter_count -= 1
                if not group:
                    del self._waiters[address]
            if not waiter.future.done():
                waiter.future.cancel()
