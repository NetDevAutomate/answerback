"""Per-account rate pressure: level, hold, decay, persisted in `pressure.sqlite`.

One bridge owns the file (the runtime owner lock), so there is one writer. Wall-clock
times are what is stored and shown; holds are measured on the monotonic clock of this
process, rebased from the stored wall time when the store is opened, so neither a
restart nor a wall-clock jump can lengthen a hold past the configured cap.
"""

import asyncio
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import KNOWN_PROVIDERS, PressureSettings
from .signatures import Match

MAX_LEVEL = 16
SIGNAL_RING = 1000
_HINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


@dataclass(frozen=True, slots=True)
class Pressure:
    provider: str
    account_hint: str
    level: int  # 0 = clear
    until_wall: float | None  # time.time() seconds; None when level == 0
    until_mono: float | None  # time.monotonic() seconds in this process; None when level == 0
    last_signal_wall: float | None
    clean_since_wall: float | None

    @property
    def held(self) -> bool:
        """Whether dispatch for this account waits now (on this process's monotonic clock)."""
        return self.retry_after(time.monotonic()) > 0

    def retry_after(self, now_mono: float) -> float:
        if self.level <= 0 or self.until_mono is None:
            return 0.0
        return max(0.0, self.until_mono - now_mono)


Listener = Callable[[str, Pressure], None]  # (transition, pressure): enter, raise or exit


@dataclass
class _Row:
    level: int = 0
    until_wall: float | None = None
    until_mono: float | None = None
    last_signal_wall: float | None = None
    last_signal_mono: float | None = None
    clean_since_wall: float | None = None
    clean_since_mono: float | None = None


class PressureStore:
    def __init__(
        self,
        path: Path | None,
        *,
        settings: PressureSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps the rows in memory only (tests and tools)."""
        self.path = path
        self.settings = settings or PressureSettings()
        self.clock = clock
        self.wall = wall
        self.error: str | None = None  # The last persistence or listener failure, by class.
        self.last: tuple[Match, float] | None = None  # The last signal and its wall time.
        self._rows: dict[tuple[str, str], _Row] = {}
        self._listeners: list[Listener] = []
        if path is not None:
            self._open(path)

    # Persistence.

    def _open(self, path: Path) -> None:
        from ..transport import private_directory

        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Pressure database must not be a symlink")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS pressure (provider TEXT NOT NULL, "
                "account_hint TEXT NOT NULL, level INTEGER NOT NULL, until_wall REAL, "
                "last_signal_wall REAL, clean_since_wall REAL, "
                "PRIMARY KEY (provider, account_hint))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS signals (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "provider TEXT NOT NULL, account_hint TEXT NOT NULL, harness TEXT NOT NULL, "
                "source TEXT NOT NULL, signature TEXT NOT NULL, model TEXT, "
                "observed_at REAL NOT NULL)"
            )
            rows = db.execute(
                "SELECT provider,account_hint,level,until_wall,last_signal_wall,"
                "clean_since_wall FROM pressure"
            ).fetchall()
        path.chmod(0o600)
        now, wall = self.clock(), self.wall()

        def back(stamp: float | None) -> float | None:
            return None if stamp is None else now - max(0.0, wall - stamp)

        for row in rows:
            level = row["level"] if type(row["level"]) is int else 0
            level = max(0, min(MAX_LEVEL, level))
            until_wall = row["until_wall"] if level else None
            until_mono = None
            if until_wall is not None:
                # A restart cannot trust the old monotonic clock: what is left of the hold
                # by the wall clock, never more than one capped delay.
                left = min(self.settings.cap(row["provider"]), max(0.0, until_wall - wall))
                until_mono = now + left
            self._rows[(row["provider"], row["account_hint"])] = _Row(
                level,
                until_wall,
                until_mono,
                row["last_signal_wall"],
                back(row["last_signal_wall"]),
                row["clean_since_wall"],
                back(row["clean_since_wall"]),
            )

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        db = sqlite3.connect(self.path, timeout=1.0)
        db.row_factory = sqlite3.Row
        return db

    def _persist(self, key: tuple[str, str], row: _Row, signal: tuple[Match, float] | None) -> None:
        if self.path is None:
            return
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO pressure(provider,account_hint,level,until_wall,last_signal_wall,"
                "clean_since_wall) VALUES(?,?,?,?,?,?) ON CONFLICT(provider,account_hint) DO "
                "UPDATE SET level=excluded.level,until_wall=excluded.until_wall,"
                "last_signal_wall=excluded.last_signal_wall,"
                "clean_since_wall=excluded.clean_since_wall",
                (*key, row.level, row.until_wall, row.last_signal_wall, row.clean_since_wall),
            )
            if signal is not None:
                match, observed = signal
                db.execute(
                    "INSERT INTO signals(provider,account_hint,harness,source,signature,model,"
                    "observed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        match.provider,
                        match.account_hint,
                        match.harness,
                        match.source,
                        match.signature,
                        match.model,
                        observed,
                    ),
                )
                db.execute(
                    "DELETE FROM signals WHERE seq <= (SELECT MAX(seq) FROM signals) - ?",
                    (SIGNAL_RING,),
                )

    # Reads.

    def _view(self, provider: str, account_hint: str) -> Pressure:
        row = self._rows.get((provider, account_hint)) or _Row()
        return Pressure(
            provider,
            account_hint,
            row.level,
            row.until_wall if row.level else None,
            row.until_mono if row.level else None,
            row.last_signal_wall,
            row.clean_since_wall,
        )

    def status(self, provider: str, account_hint: str = "default") -> Pressure:
        return self._view(provider, account_hint)

    def snapshot(self) -> dict[str, Any]:
        """Every configured account and every account with a row, keyed `provider:hint`.
        Wall-clock seconds only; whether a hold is still running is `until` against now."""
        keys = {(name, self.settings.account_hint(name)) for name in self.settings.providers}
        keys.update(self._rows)
        result: dict[str, Any] = {}
        for provider, hint in sorted(keys):
            view = self._view(provider, hint)
            result[f"{provider}:{hint}"] = {
                "provider": provider,
                "account_hint": hint,
                "level": view.level,
                "until": view.until_wall,
                "last_signal": view.last_signal_wall,
                "clean_since": view.clean_since_wall,
            }
        return result

    def signals(self, limit: int = 20) -> list[dict[str, Any]]:
        """The most recent signal records (class only, never text), newest first."""
        if self.path is None:
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT provider,account_hint,harness,source,signature,model,observed_at "
                "FROM signals ORDER BY seq DESC LIMIT ?",
                (max(0, min(limit, SIGNAL_RING)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # Changes.

    def _fire(self, transition: str, pressure: Pressure) -> None:
        for listener in list(self._listeners):
            try:
                listener(transition, pressure)
            except Exception as error:
                # A listener (event stream, notification) never undoes or blocks a change.
                self.error = type(error).__name__

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def signal(self, match: Match) -> Pressure:
        """Raise the account's level and set its hold. It never raises for a storage
        failure: the change is kept in memory and `error` names the failure."""
        if not self.settings.enabled:
            return self.status(match.provider, match.account_hint)
        now, wall = self.clock(), self.wall()
        key = (match.provider, match.account_hint)
        row = self._rows.setdefault(key, _Row())
        held = row.level > 0 and row.until_mono is not None and row.until_mono > now
        row.level = min(MAX_LEVEL, row.level + 1) if held else max(1, row.level)
        delay = self.settings.delay(match.provider, row.level)
        row.until_mono, row.until_wall = now + delay, wall + delay
        row.last_signal_mono, row.last_signal_wall = now, wall
        row.clean_since_mono = row.clean_since_wall = None
        self.last = (match, wall)
        try:
            self._persist(key, row, self.last)
        except (sqlite3.Error, OSError) as error:
            self.error = type(error).__name__
        pressure = self._view(*key)
        self._fire("raise" if held else "enter", pressure)
        return pressure

    def decay(self) -> list[Pressure]:
        """One level down for each account with no signal for a whole clean window since
        its last signal or last step down; level 0 ends the hold. Returns the changed."""
        now, wall = self.clock(), self.wall()
        changed: list[tuple[str, Pressure]] = []
        for key, row in self._rows.items():
            if row.level <= 0 or (row.until_mono is not None and row.until_mono > now):
                continue
            since = max(
                row.last_signal_mono if row.last_signal_mono is not None else -float("inf"),
                row.clean_since_mono if row.clean_since_mono is not None else -float("inf"),
            )
            if since != -float("inf") and now - since < self.settings.clean_seconds:
                continue
            row.level -= 1
            row.clean_since_mono, row.clean_since_wall = now, wall
            if row.level == 0:
                row.until_mono = row.until_wall = None
            self._persist(key, row, None)
            changed.append(("exit" if row.level == 0 else "", self._view(*key)))
        for transition, pressure in changed:
            if transition:
                self._fire(transition, pressure)
        return [pressure for _, pressure in changed]

    def clear(self, provider: str, account_hint: str = "default") -> Pressure:
        """Level 0 now (an operator override); `exit` fires when a level was set."""
        # Both messages are reviewed public literals (errors.py), so the CLI shows them.
        if provider not in KNOWN_PROVIDERS:
            raise ValueError("Unknown pressure provider; use kiro or bedrock")
        if not isinstance(account_hint, str) or not _HINT.fullmatch(account_hint):
            raise ValueError("Pressure account hint must be a 1–64 character label")
        key = (provider, account_hint)
        was = self._rows.get(key)
        row = _Row(clean_since_wall=self.wall(), clean_since_mono=self.clock())
        if was is not None:
            row.last_signal_wall, row.last_signal_mono = was.last_signal_wall, was.last_signal_mono
        self._persist(key, row, None)
        self._rows[key] = row
        pressure = self._view(*key)
        if was is not None and was.level > 0:
            self._fire("exit", pressure)
        return pressure

    async def wait(
        self,
        provider: str,
        account_hint: str = "default",
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> Pressure:
        """Return at once when the account is not held; otherwise sleep until its hold ends,
        re-reading after each wake because a new signal can extend it."""
        while True:
            pressure = self.status(provider, account_hint)
            remaining = pressure.retry_after(self.clock())
            if remaining <= 0:
                return pressure
            await sleep(remaining)


def stream_listener(hub: Any, store: PressureStore) -> Listener:
    """Publish `answerback.provider_throttled` for each signal and keep the `pressure`
    state section current. Class only: no text and no digest leave through here."""
    from ..events import model

    def listener(transition: str, pressure: Pressure) -> None:
        if transition in ("enter", "raise") and store.last is not None:
            match, observed = store.last
            hub.publish(
                model.custom(
                    "answerback.provider_throttled",
                    {
                        "provider": pressure.provider,
                        "account_hint": pressure.account_hint,
                        "model": match.model,
                        "harness": match.harness,
                        "source": match.source,
                        "signature": match.signature,
                        "observed_at": observed,
                        "level": pressure.level,
                        "until": pressure.until_wall,
                    },
                )
            )
        publish_section(hub, store)

    return listener


def publish_section(hub: Any, store: PressureStore) -> None:
    """Set the `pressure` state section; the first time, as an `add` of the new key (the
    hub's base state has no such key, and a replace needs one)."""
    snapshot = store.snapshot()
    if "pressure" in hub.state:
        hub.set_section("pressure", snapshot)
    else:
        hub.patch_state([{"op": "add", "path": "/pressure", "value": snapshot}])
