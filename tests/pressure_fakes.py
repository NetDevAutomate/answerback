"""Provider-pressure stores for the T-227 dispatcher tests.

`FakePressure` has the `PressureStore` surface the dispatchers use (`settings`, `clock`,
`status`, `wait`, `subscribe`, `snapshot`) with a scripted hold: a row is held from the
start, `wait()` blocks until the test calls `release()`, and every call is recorded.
`ClockedStore` is the real store on a fake clock, for tests of the real matchers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from answerback_bridge.pressure import Match, Pressure, PressureSettings, PressureStore

WALL = 1_800_000_000.0
MONO = 5_000.0
NOTICE = "Provider under rate pressure: fewer, larger tool calls; no parallel sub-agents."


def match(provider: str = "bedrock", harness: str = "codex") -> Match:
    return Match(provider, "default", harness, "native_evidence", "bedrock.throttling", "0" * 64)


class FakePressure:
    def __init__(
        self,
        held: dict[tuple[str, str], tuple[int, float]] | None = None,
        *,
        hold_on_wait: bool = True,
    ) -> None:
        """`held` maps (provider, account_hint) to (level, seconds until the pause ends).

        With `hold_on_wait` a wait blocks until `release()`; without it the pause passes
        at once, as a real store's wait returns only once the account is clear.
        """
        self.settings = PressureSettings()
        self.mono = MONO
        self.rows = dict(held or {})
        self.hold_on_wait = hold_on_wait
        self.waits: list[tuple[str, str]] = []
        self.signals: list[Match] = []
        self.listeners: list[Any] = []
        self._released: asyncio.Event | None = None

    # --- the PressureStore surface --------------------------------------------------
    def clock(self) -> float:
        return self.mono

    def wall(self) -> float:
        return WALL + (self.mono - MONO)

    def status(self, provider: str, account_hint: str = "default") -> Pressure:
        row = self.rows.get((provider, account_hint))
        if row is None:
            return Pressure(provider, account_hint, 0, None, None, None, None)
        level, seconds = row
        return Pressure(
            provider,
            account_hint,
            level,
            WALL + seconds,
            MONO + seconds,
            WALL - 1,
            None,
        )

    async def wait(self, provider: str, account_hint: str = "default", **_: Any) -> Pressure:
        self.waits.append((provider, account_hint))
        if (provider, account_hint) in self.rows:
            if self.hold_on_wait:
                await self.released().wait()
            else:
                # The pause passes at once: the account is clear when the wait returns.
                self.rows.pop((provider, account_hint), None)
        return self.status(provider, account_hint)

    def signal(self, match: Match) -> Pressure:
        """A detector's signal: recorded, and the account is held for a minute."""
        self.signals.append(match)
        key = (match.provider, match.account_hint)
        level = self.rows.get(key, (0, 0.0))[0] + 1
        self.rows[key] = (level, 60.0)
        return self.status(*key)

    def subscribe(self, listener: Any) -> None:
        self.listeners.append(listener)

    def snapshot(self) -> dict[str, Any]:
        return {
            f"{provider}:{hint}": {
                "provider": provider,
                "account_hint": hint,
                "level": level,
                "until": WALL + seconds,
            }
            for (provider, hint), (level, seconds) in self.rows.items()
        }

    # --- test controls ----------------------------------------------------------------
    def released(self) -> asyncio.Event:
        if self._released is None:
            self._released = asyncio.Event()
        return self._released

    def release(self) -> None:
        """End every pause: the rows clear and every waiter returns."""
        self.rows.clear()
        self.released().set()

    def hold(self, provider: str, level: int = 1, seconds: float = 60.0) -> None:
        self.rows[(provider, "default")] = (level, seconds)
        self._released = None


class ClockedStore(PressureStore):
    """Lane-detect's real store on a fake clock: `wait()` advances the clock by the
    remaining hold instead of sleeping it, so a test that runs a relaunch after a real
    60 s hold finishes at once. `advance()` moves both clocks for decay tests."""

    def __init__(self, path: Path | None, *, settings: PressureSettings | None = None) -> None:
        self.mono = MONO
        self.slept: list[float] = []
        super().__init__(path, settings=settings, clock=lambda: self.mono, wall=self._wall)

    def _wall(self) -> float:
        return WALL + (self.mono - MONO)

    def advance(self, seconds: float) -> None:
        self.mono += seconds

    async def _skip(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)

    async def wait(self, provider: str, account_hint: str = "default", **_: Any) -> Pressure:
        return await super().wait(provider, account_hint, sleep=self._skip)
