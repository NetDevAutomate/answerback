"""Deadline-bound debug leases of one bridge and a bounded, metadata-only event drawer."""

import copy
import json
import math
import os
import queue
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from ..adapters.catalog import definition
from ..bus.monitor import EVENT_FIELDS, safe_text
from ..transport import private_directory
from . import model
from .patch import diff

RESOURCE_SECONDS = 2.0
TAIL_SECONDS = 5.0
DEFAULT_SECONDS = 600
MAX_SECONDS = 3600
DRAWER_BYTES = 4 * 1024 * 1024  # Two rotating segments, at most 8 MiB total.
DRAWER_QUEUE_SIZE = 256
DRAWER_WRITES_PER_SECOND = 256  # Global ceiling, including projection baselines.
DRAWER_CLOSE_SECONDS = 1.0
MAX_SCOPES = 200
MAX_JSON_DEPTH = 64
# A chosen storage limit, not the length of the longest target. Of the forms
# with a bound that were analysed, the longest is a native CLI worker's
# ``claude_code:<alias>:<cwd>``: 12 + 32 + 1 characters before a working
# directory of up to 4,095 (Linux PATH_MAX), so 4,096 would be too small, and
# twice that leaves room. A Herdr target (``herdr:<session>:<terminal_id>``)
# has no length bound on the bridge's side; one past this limit gets no lease.
MAX_SCOPE_CHARS = 8192


class DebugDrawerNotRegularFile(OSError):
    """The opened drawer descriptor is not a regular file."""


def _check_json_depth(raw: str) -> None:
    """Refuse JSON nesting beyond the fixed bound without recursion.

    This lexical pass does not validate JSON. It only counts object and array
    openers outside strings; ``json.loads`` remains the syntax parser.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError(
                    f"Debug lease file is too deeply nested (maximum JSON depth {MAX_JSON_DEPTH})"
                )
        elif char in "]}" and depth:
            depth -= 1


def duration(value: str) -> int:
    """CLI duration: positive whole seconds/minutes, no more than one hour."""
    match = re.fullmatch(r"(\d{1,5})([sm])", value)
    seconds = int(match[1]) * (60 if match[2] == "m" else 1) if match else 0
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("--for must be 1s–60m (default 10m, hard maximum 60 minutes)")
    return seconds


def is_remote(value: str) -> bool:
    """Whether the text of ``value`` names another machine, by the forms the bridge emits.

    This reads the address only. False means that the text names no machine,
    not that the peer is known to be here. A Rex terminal id names no host;
    which host the terminal is on is a fact of the Rex inventory, and the debug
    toggle does not read it: a Rex terminal is not checked for its host. That
    exception is known. It is board row T-100, held for the owner, and it reads
    nothing remote: the Rex read of a terminal on another host is refused in
    ``rex.py`` whatever lease is held.

    Only Core emits an identity that names a machine. Its terminal address is
    ``<terminal>`` or ``<machine>/<terminal>`` and never holds a ':' (``core.py``,
    ``_ADDRESS``); the peer id is ``hc:<address>`` and the receipt target is
    ``hc:<session>:<address>`` (``core_tasks.py``, ``target_key``). So for
    ``hc`` the address is the text after the last ':', and a '/' or an '@' in
    it is the machine prefix. The session name before it is free text and is
    not read (T-56 review round 7, finding B3: reading the first part instead
    took the session for the address and missed the machine behind it).

    Every other provider's target is its own opaque text and is not read for a
    machine: a Crew chat key (``crew:<key>``), a working directory
    (``kiro:<alias>:<cwd>``) or a Rex terminal id
    (``rex:terminal:<session>/<block>``) may hold '/', '@' or a space, and none
    of it is a machine prefix (finding B2, and before it X6). A provider name
    never holds '/' or '@', so text that puts one before the first ':'
    (``<machine>/hc:t1``) is refused as remote. An ``hc`` identity with nothing
    after its last ':' has no address and is not an emitted form; it is judged
    by its whole target, toward refusal.
    """
    provider, separator, target = value.partition(":")
    if not separator:
        return False
    if "/" in provider or "@" in provider:
        return True
    if provider != "hc":
        return False
    address = target.rpartition(":")[2] or target
    return "/" in address or "@" in address


def local_peer(value: str) -> bool:
    """Well-formed address syntax that names no other machine, for text the *caller* typed.

    True says that the text is well formed and names no other machine, not
    that the peer is on this machine (`is_remote()` and its Rex exception).
    Combines a known provider, a non-empty target, the reading of the text
    for another machine (`is_remote`), and bounds on caller input: printable, no
    whitespace, at most 256 characters. A scope resolved through a receipt
    lookup is trusted content, not text the caller typed -- it is classified
    with `is_remote` alone in `resolve_scope()`, never put through these
    input bounds (T-56 finding X6).
    """
    provider, separator, target = value.partition(":")
    return bool(
        separator
        and definition(provider)
        and target
        and not is_remote(value)
        and not any(char.isspace() or not char.isprintable() for char in value)
        and len(value) <= 256
    )


def scope_refusal(scope: object, *, exclusion: bool = False) -> str | None:
    """Why `DebugController` may not store ``scope``; ``None`` when it may.

    The one rule for a lease key and for an exclusion of an ``all`` lease:
    `on()`, `off()` and the loader of the saved file all ask here, so whatever
    the controller stores it can load again (T-56 finding M-1/R-1). A stored
    scope is ``"all"`` (a lease only) or a ``<provider>:<peer>`` address whose
    text does not name another machine, of at most `MAX_SCOPE_CHARS`
    characters. That is a statement about the text, not about where the peer
    is: a Rex terminal is not checked for its host, the exception `is_remote()`
    documents (board row T-100). It is deliberately not
    `local_peer()`: that is the rule for text a caller typed, and a receipt's
    stored target may hold a space, be long, or name a provider that is no
    longer configured.
    """
    if not isinstance(scope, str) or not scope:
        return "Debug scope must be all or a local provider:peer address"
    if len(scope) > MAX_SCOPE_CHARS:
        return f"Debug scope is longer than {MAX_SCOPE_CHARS} characters"
    if scope == "all":
        return "Debug scope all cannot be an exclusion" if exclusion else None
    if is_remote(scope):
        return "Remote peer debug is unavailable until T-29"
    provider, separator, target = scope.partition(":")
    if not (provider and separator and target):
        return "Debug scope must be all or a local provider:peer address"
    return None


def lease_refusal(lease: object, now: float) -> str | None:
    """Why ``lease`` may not be one scope's stored record; ``None`` when it may.

    The record is an object with exactly the keys ``started`` and ``until``,
    both finite numbers (a boolean is not one), with a deadline after its start
    by at most `MAX_SECONDS` and a start that is not in the future. `on()`
    asks here before it stores a record, and the loader asks through
    `saved_refusal()`, so the two cannot disagree (T-56 review round 7, B1).
    """
    if not isinstance(lease, dict) or set(lease) != {"started", "until"}:
        return "Debug lease must hold exactly started and until"
    started, until = lease["started"], lease["until"]
    for value in (started, until):
        # A whole number is finite, and may be too large to become a float.
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            return "Debug deadline must be a finite number"
    try:
        length = until - started
    except OverflowError:  # A whole number too large for a float, beside a float.
        return "Debug deadline must be a finite number"
    if not 0 < length <= MAX_SECONDS or not started <= now:
        return "Debug duration must be positive and at most 60 minutes"
    return None


def saved_refusal(saved: object, now: float) -> str | None:
    """Why the loader may not take ``saved``, the parsed lease file; ``None`` when it may.

    The shape is the one `DebugController._save` writes: an object with exactly
    the keys ``leases`` and ``excluded``; ``leases`` an object from scope to
    record, ``excluded`` a list of scopes; at most `MAX_SCOPES` of each. Every
    scope passes `scope_refusal()` and every record `lease_refusal()`, the
    rules `on()` and `off()` store by. A container of another type is refused
    here, by type, before anything iterates it: an object in place of the list
    used to be read key by key as if it were the list (T-56 review round 7, B1).
    """
    if not isinstance(saved, dict) or set(saved) != {"leases", "excluded"}:
        return "Debug lease file must hold exactly leases and excluded"
    leases, excluded = saved["leases"], saved["excluded"]
    if not isinstance(leases, dict) or not isinstance(excluded, list):
        return "Debug leases must be an object and debug exclusions a list"
    if len(leases) > MAX_SCOPES or len(excluded) > MAX_SCOPES:
        return "Too many debug scopes"
    for scope, lease in leases.items():
        if (refusal := scope_refusal(scope) or lease_refusal(lease, now)) is not None:
            return refusal
    for peer in excluded:
        if (refusal := scope_refusal(peer, exclusion=True)) is not None:
            return refusal
    return None


def resolve_scope(scope: str, receipt_path: Path) -> tuple[str, bool]:
    """Resolve an exact receipt ID/request key before reading the scope as an address.

    Returns the resolved scope and whether it came from a receipt lookup, as
    opposed to being given directly as a ``provider:target`` address (or
    ``"all"``). ``"all"`` is checked first and always keeps its keyword
    meaning, even when a receipt happens to have the request key ``"all"``
    (T-56 finding X5). Otherwise the receipt lookup runs before any attempt
    to read ``scope`` itself as an address: a request key that looks like one
    (``kiro:debug1``) must resolve to its receipt, not be treated as a (wrong)
    address in its own right. A receipt's stored target is trusted content,
    not text the caller typed, so it is classified only by `is_remote` --
    never refused for holding whitespace or running past 256 characters
    (T-56 finding X6, inherited from before this row). Below, once neither
    the receipt nor `local_peer` accepts ``scope``, the refusal message is
    also chosen by `is_remote`, not by raw ``':'``/``'/'``/``'@'`` marker
    presence in the caller's text: an invalid scope whose text names no other
    machine (whitespace, an unknown provider) must not be told it is "remote"
    (rework round 1,
    finding M-2/R-3 -- the grant path already classified by `is_remote`, but
    this refusal path still used the cruder marker check it replaced).
    """
    if not isinstance(scope, str) or not scope or len(scope) > 256:
        raise ValueError("Debug scope must be a task ID, local peer address, or all")
    if scope == "all":
        return scope, False
    # No receipt body/result is loaded to resolve an ID.
    with closing(sqlite3.connect(f"{receipt_path.as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT target, CASE WHEN json_valid(metadata) "
            "THEN json_extract(metadata,'$.peer.peer_id') END FROM tasks "
            "WHERE id=? OR request_key=? LIMIT 1",
            (scope, scope),
        ).fetchone()
    if row:
        peer = row[1] or row[0]
        if isinstance(peer, str) and peer and not is_remote(peer):
            return peer, True
        raise ValueError("Remote peer debug is unavailable until T-29; run on its owning node")
    if local_peer(scope):
        return scope, False
    if is_remote(scope):
        raise ValueError("Remote peer debug is unavailable until T-29; use a local provider:peer")
    raise ValueError("Unknown debug task ID; use its receipt ID or a local provider:peer")


def is_receipt_target(scope: str, receipt_path: Path) -> bool:
    """Whether some receipt's resolution would land on ``scope``.

    Checks the same two columns `resolve_scope()` reads to resolve an ID --
    the stored ``target`` and the nested ``metadata.peer.peer_id`` -- against
    ``scope`` itself, not against an ID. A Kiro/native-CLI receipt target
    holds a path (``kiro:<alias>:<cwd>``); the discovered peer scan for that
    same worker reports a shorter identity with no path (``kiro:<alias>``,
    confirmed at ``adapters/kiro.py:41``). ``debug status`` echoes the
    receipt target, not the scan identity, back to the caller, so an operator
    who pastes that exact address into ``debug on`` -- not its task ID --
    must still be recognised as evidence-backed (``observed``), even though
    it never appears as a scanned peer (T-56 finding T-1, carried into
    WP-32's ``observed`` computation: a scope is observed when it is in the
    current peer scan *or* is some receipt's stored evidence).

    Pasting works only for a scope `local_peer()` accepts as typed text: a
    provider this bridge knows now, printable characters only, no whitespace,
    at most 256 characters. Text that is also the ID or the request key of a
    receipt names that receipt first (`resolve_scope()`). Any other status
    scope is refused by `resolve_scope()` before this function is asked, for
    ``debug on`` and for ``debug off``: such a lease is granted and removed by
    its receipt ID or request key, or removed with a plain ``debug off`` (T-56
    review round 7, N3; round 8, finding 3).

    A Core ``hc`` receipt instead carries its evidence in the nested
    ``metadata.peer.peer_id`` (`resolve_scope()` prefers it over ``target``
    when resolving an ID). Checking only ``target`` here left a lease
    resolved that way `observed` at grant time (`Bridge.debug_on()` has
    `resolve_scope()`'s own ``from_receipt`` flag to hand) but not on any
    later, independent `Bridge.debug_status()` call, which has only the
    resolved scope string left and no flag (rework round 1, finding R-2:
    `debug_on()`'s and `debug_status()`'s read of the same lease disagreed).
    Checking both columns against ``scope`` makes this the one, consistent
    source of receipt evidence both callers share.
    """
    with closing(sqlite3.connect(f"{receipt_path.as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT 1 FROM tasks WHERE target=? OR "
            "(json_valid(metadata) AND json_extract(metadata,'$.peer.peer_id')=?) LIMIT 1",
            (scope, scope),
        ).fetchone()
    return row is not None


def safe_raw(row: Any) -> dict[str, Any]:
    """Reapply the T-16 bus metadata allowlist; RAW never means unredacted payload."""
    if not isinstance(row, dict):
        return {}
    result: dict[str, Any] = {
        key: safe_text(row[key], limit)
        for key, limit in (
            ("event_id", 128),
            ("command_id", 128),
            ("type", 128),
            ("peer_id", 256),
            ("project", 1024),
        )
        if isinstance(row.get(key), str)
    }
    for key in ("seq", "created_at"):
        value = row.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = value
    payload = row.get("payload")
    result["payload"] = (
        {
            key: safe_text(value) if isinstance(value, str) else None
            for key, value in payload.items()
            if key in EVENT_FIELDS and (value is None or isinstance(value, str))
        }
        if isinstance(payload, dict)
        else {}
    )
    return result


class DebugController:
    """One bridge owns these leases. Readers share its lock; disk stores deadlines."""

    def __init__(self, runtime: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.directory = runtime / "debug"
        self.path = self.directory / "leases.json"
        self.clock = clock
        self.lock = threading.RLock()
        self.leases: dict[str, dict[str, float]] = {}
        self.excluded: set[str] = set()
        self.projection: dict[str, Any] | None = None
        self.run_peers: dict[str, str] = {}
        self.error: str | None = None
        self.generation = 0
        self._writes: queue.Queue[bytes] = queue.Queue(maxsize=DRAWER_QUEUE_SIZE)
        self._writer: threading.Thread | None = None
        self._closing = False
        self._abandoned = False
        self._pending_writes = 0  # Includes the in-flight write.
        self._queued_bytes = 0  # Includes the in-flight write.
        self._dropped_writes = 0
        self._write_window = monotonic()
        self._write_count = 0
        self._projection_lost = False
        try:
            raw = self.path.read_text()
            _check_json_depth(raw)
            saved = json.loads(raw)
            now = self.clock()
            if (refusal := saved_refusal(saved, now)) is not None:
                raise ValueError(refusal)
            for scope, lease in saved["leases"].items():
                if lease["until"] > now:
                    self.leases[scope] = lease
            self.excluded = set(saved["excluded"]) if "all" in self.leases else set()
        except FileNotFoundError:
            pass
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,  # Keep older interpreters' parser failure fail-closed.
        ) as error:
            self.leases.clear()
            self.excluded.clear()
            self.error = type(error).__name__

    def _save(self) -> None:
        private_directory(self.directory.parent)
        private_directory(self.directory)
        temporary = self.directory / "leases.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"leases": self.leases, "excluded": sorted(self.excluded)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def _expire(self) -> None:
        now = self.clock()
        expired = [scope for scope, lease in self.leases.items() if lease["until"] <= now]
        if expired:
            for scope in expired:
                del self.leases[scope]
            if "all" not in self.leases:
                self.excluded.clear()
            self.generation += 1
            try:
                self._save()
            except OSError as error:
                # Deadlines stay expired in memory even when the disk is unavailable.
                self.error = type(error).__name__

    def on(self, scope: str, seconds: float = DEFAULT_SECONDS) -> None:
        """Enable a scope until an absolute deadline, replacing its prior lease.

        ``scope`` is already resolved and trusted by the only production caller
        (`resolve_scope()`, through `Bridge.debug_on()`): it is only ever ``"all"``,
        a caller address `local_peer()` already validated, or a receipt's stored
        target. The check here is `scope_refusal()`, the rule the loader of the
        saved file applies too -- not a repeat of `local_peer()`'s caller-input
        bounds, which would wrongly refuse a receipt target that holds a space or
        runs past 256 characters (T-56 finding X6).
        """
        if type(seconds) not in (int, float) or not 0 < seconds <= MAX_SECONDS:
            raise ValueError("Debug duration must be positive and at most 60 minutes")
        if (refusal := scope_refusal(scope)) is not None:
            raise ValueError(refusal)
        with self.lock:
            self._expire()
            if scope not in self.leases and len(self.leases) >= MAX_SCOPES:
                raise ValueError("Debug scope limit reached")
            before, excluded = dict(self.leases), set(self.excluded)
            now = self.clock()
            lease = {"started": now, "until": now + seconds}
            if lease_refusal(lease, now) is not None:
                # Float rounding can put the deadline on its start, or just past the
                # maximum. The loader refuses such a lease, so it is never stored.
                raise ValueError("Debug duration must be positive and at most 60 minutes")
            self.leases[scope] = lease
            if scope == "all":
                self.excluded.clear()
            else:
                self.excluded.discard(scope)
            try:
                self._save()
            except Exception:
                self.leases, self.excluded = before, excluded
                raise
            self.projection = None  # The next event starts with a scoped baseline.
            self.generation += 1

    def off(self, scope: str | None = None) -> None:
        """Disable all scopes or one peer, including an exception to an all lease.

        One peer may become an exclusion in the saved file, so it passes the same
        `scope_refusal()` as a lease key. Nothing that rule refuses can be a lease,
        so a refusal here never keeps a lease from being removed.
        """
        if scope not in (None, "all"):
            if (refusal := scope_refusal(scope, exclusion=True)) is not None:
                raise ValueError(refusal)
        with self.lock:
            self._expire()
            if scope in (None, "all"):
                self.leases.clear()
                self.excluded.clear()
            else:
                if "all" in self.leases and len(self.excluded) >= MAX_SCOPES:
                    raise ValueError("Debug exclusion limit reached; use debug off")
                self.leases.pop(scope, None)
                if "all" in self.leases:
                    self.excluded.add(scope)
            self._save()
            self.generation += 1

    def until(self, peer: str) -> float | None:
        """Current effective deadline; every use checks expiry, including drawer writes.

        ``peer`` here is a discovered peer/run identity, or a lease key already
        validated by `on()` -- the same trusted-target reasoning applies, so only
        the text is read for another machine (`is_remote()`), without
        `local_peer()`'s caller-input bounds (T-56 finding X6).
        """
        with self.lock:
            self._expire()
            if is_remote(peer):
                return None
            scopes = [peer] + (["all"] if peer not in self.excluded else [])
            deadlines = [self.leases[s]["until"] for s in scopes if s in self.leases]
            return max(deadlines) if deadlines else None

    def status(self, peers: Iterable[str] = ()) -> dict[str, Any]:
        """Report leases, deadlines, exclusions and the node-local evidence location."""
        with self.lock:
            self._expire()
            return {
                "scopes": [
                    {
                        "scope": scope,
                        "untilMs": int(lease["until"] * 1000),
                        "until": datetime.fromtimestamp(lease["until"], UTC).isoformat(),
                    }
                    for scope, lease in sorted(self.leases.items())
                ],
                "excluded": sorted(self.excluded),
                "peers": {
                    peer: int(until * 1000)
                    for peer in sorted(set(peers) | (self.leases.keys() - {"all"}))
                    if (until := self.until(peer)) is not None
                },
                "drawer": str(self.directory / "trace.ndjson"),
                "drawerMaxBytes": DRAWER_BYTES * 2,
                "droppedWrites": self._dropped_writes,
                "error": self.error,
            }

    def _drop_write(self, error: str, event: model.Event) -> None:
        """With the lock held: report loss and repair a missing projection on the next event."""
        self._dropped_writes += 1
        self.error = error
        if event["type"].startswith("STATE_"):
            self._projection_lost = True

    def _write(self, ident: str, event: model.Event, peers: set[str], runs: set[str]) -> None:
        """With the lock held: bounded, nonblocking admission; no drawer filesystem work."""
        if self._closing:
            return
        now = monotonic()
        if now - self._write_window >= 1:
            self._write_window, self._write_count = now, 0
        if self._write_count >= DRAWER_WRITES_PER_SECOND:
            self._drop_write("DebugDrawerWriteRateLimited", event)
            return
        self._write_count += 1
        encoded = (
            json.dumps(
                {"id": ident, "peerIds": sorted(peers), "runIds": sorted(runs), "event": event},
                ensure_ascii=True,
            )
            + "\n"
        ).encode()
        if len(encoded) > DRAWER_BYTES:
            self._drop_write("DebugEventExceedsDrawerBound", event)
            return
        if self._queued_bytes + len(encoded) > DRAWER_BYTES:
            self._drop_write("DebugDrawerWriteQueueFull", event)
            return
        try:
            self._writes.put_nowait(encoded)
        except queue.Full:
            self._drop_write("DebugDrawerWriteQueueFull", event)
            return
        self._queued_bytes += len(encoded)
        self._pending_writes += 1
        if self._writer is None:
            self._writer = threading.Thread(
                target=self._drain, name="answerback-debug-writer", daemon=True
            )
            self._writer.start()

    def _drain(self) -> None:
        """One writer owns append/rotation; disk latency never holds the controller lock."""
        while True:
            try:
                encoded = self._writes.get(timeout=0.05)
            except queue.Empty:
                with self.lock:
                    if self._closing:
                        return
                continue
            try:
                with self.lock:
                    if self._abandoned:
                        return
                self._write_sync(encoded)
            except (OSError, ValueError, TypeError) as error:
                with self.lock:
                    if not self._abandoned:  # A close timeout already counted this record.
                        self.error = type(error).__name__
                        self._dropped_writes += 1
                        self._projection_lost = True
            finally:
                with self.lock:
                    self._queued_bytes -= len(encoded)
                    self._pending_writes -= 1
                self._writes.task_done()

    def flush(self) -> None:
        """Wait for admitted records to finish; async callers must use asyncio.to_thread."""
        self._writes.join()

    def close(self) -> None:
        """Wait at most one second for the writer; call outside the event loop.

        On timeout, count all unfinished records as lost and discard queued writes.
        A stuck daemon thread may remain until process exit; shutdown does not wait for it.
        """
        with self.lock:
            self._closing = True
            writer = self._writer
            if self._abandoned:
                return
        if writer is not None:
            writer.join(timeout=DRAWER_CLOSE_SECONDS)
            with self.lock:
                if writer.is_alive() and not self._abandoned:
                    self._abandoned = True
                    self._dropped_writes += self._pending_writes
                    self.error = "DebugDrawerCloseTimeout"
                    self._projection_lost = True
                    while True:
                        try:
                            encoded = self._writes.get_nowait()
                        except queue.Empty:
                            break
                        self._queued_bytes -= len(encoded)
                        self._pending_writes -= 1
                        self._writes.task_done()

    def _write_sync(self, encoded: bytes) -> None:
        path = self.directory / "trace.ndjson"
        private_directory(self.directory)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        for rotate in (True, False):
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "ab") as stream:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise DebugDrawerNotRegularFile()
                if rotate and info.st_size + len(encoded) > DRAWER_BYTES:
                    path.replace(self.directory / "trace.1.ndjson")
                    continue  # Reopen and recheck even if another process swaps the path.
                os.fchmod(fd, 0o600)
                stream.write(encoded)
                return

    def record(
        self,
        ident: str,
        event: model.Event,
        state: dict[str, Any],
        activities: dict[str, tuple[str, dict[str, Any]]],
    ) -> None:
        """Called after projection, under the hub lock. Never receives worker tails."""
        with self.lock:
            self._expire()
            if not self.leases:
                self.projection = None
                return
            try:
                if self._projection_lost:
                    self.projection = None
                    self._projection_lost = False
                self._record(ident, event, state, activities)
            except (OSError, ValueError, TypeError) as error:
                self.error = type(error).__name__  # Observer IO must not stop dispatch.

    def _record(
        self,
        ident: str,
        event: model.Event,
        state: dict[str, Any],
        activities: dict[str, tuple[str, dict[str, Any]]],
    ) -> None:
        tags = event.get("metadata", {}).get(model.NAMESPACE, {})
        peer, run = tags.get("peerId"), tags.get("runId")
        if peer and run:
            self.run_peers[run] = peer
            if len(self.run_peers) > 5000:
                del self.run_peers[next(iter(self.run_peers))]
        # Lifecycle events precede their board state. Preserve the RUN_STARTED link
        # so the immediately following STEP_STARTED can be attributed too.
        peer = peer or self.run_peers.get(run)
        peers = {
            peer
            for peer in [
                *state["peers"],
                *(run["peerId"] for run in state["runs"].values()),
                *(scope for scope in self.leases if scope != "all"),
            ]
            if self.until(peer) is not None
        }
        runs = {run for run, row in state["runs"].items() if row["peerId"] in peers}
        projected = {
            "peers": {key: value for key, value in state["peers"].items() if key in peers},
            "runs": {key: value for key, value in state["runs"].items() if key in runs},
            "activities": {
                key: value for key, value in state["activities"].items() if key[5:] in peers
            },
            "activityContent": {
                key: content for key, (_, content) in activities.items() if key[5:] in peers
            },
        }
        if self.projection is None:
            self._write(ident, model.state_snapshot(projected), peers, runs)
        elif changes := diff(self.projection, projected):
            self._write(ident, model.state_delta(changes), peers, runs)
        self.projection = copy.deepcopy(projected)
        kind = event["type"]
        if kind.startswith("STATE_"):
            return
        if kind == "RAW":
            row = safe_raw(event.get("event"))
            peer = row.get("peer_id")
            run = row["payload"].get("task_id")
            event = {**event, "event": row}
        elif kind.startswith("ACTIVITY_"):
            peer = event.get("messageId", "").removeprefix("tail:")
        elif kind == "CUSTOM":
            peer = event.get("value", {}).get("peerId", peer)
            run = event.get("value", {}).get("runId", run)
        if (peer and self.until(peer) is not None) or run in runs:
            linked_peers = {peer} if peer else peers
            linked_runs = (
                {run}
                if run
                else {key for key in runs if state["runs"][key]["peerId"] in linked_peers}
            )
            self._write(ident, event, linked_peers, linked_runs)
