"""What the dispatchers do with provider pressure: hold, wait, notify, prepend.

The store (`PressureStore`) decides whether a provider account is held. This module turns
that into the dispatcher's answers: a `provider_backoff` result for `peer_submit`, the
one-line notice prepended to the next assignment, and the owner-facing notification text.
It never cancels, kills or messages work that is already running.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ..config import NATIVE_PROVIDERS
from . import Pressure, PressureStore, harness_of_peer, provider_of

Schedule = Callable[[float, Callable[[], None]], Any]  # (delay, callback) -> handle with cancel()

# The lowest wait an unheld `retry_after_s` can show; a held row always reports > 0.
_MIN_RETRY = 0.1


def clock_time(wall: float | None) -> str:
    """`HH:MM:SS` in local time for a `time.time()` value; `--:--:--` without one."""
    if wall is None:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(wall))


def account_hint(store: PressureStore, provider: str) -> str:
    """The configured account label for `provider` (never derived from a credential)."""
    return store.settings.account_hint(provider)


def held(store: PressureStore, provider: str, hint: str | None = None) -> Pressure | None:
    """The provider account's pressure while it holds dispatch, else None.

    `retry_after` is read against the store's own monotonic clock, so a store built with
    a fake clock gives the same answer as its `wait()`.
    """
    if provider == "unknown":
        return None
    pressure = store.status(provider, account_hint(store, provider) if hint is None else hint)
    return pressure if pressure.retry_after(store.clock()) > 0 else None


def hold_for(store: PressureStore, peer: dict[str, Any]) -> Pressure | None:
    """The pressure holding dispatch to this resolved peer, or None (also for `unknown`)."""
    return held(store, provider_of(harness_of_peer(peer)))


def backoff_result(pressure: Pressure, store: PressureStore | None = None) -> dict[str, Any]:
    """`peer_submit`'s answer while held: a result, like `needs_selection`; nothing was sent."""
    now = store.clock() if store is not None else time.monotonic()
    return {
        "status": "provider_backoff",
        "provider": pressure.provider,
        "account_hint": pressure.account_hint,
        "level": pressure.level,
        "until": pressure.until_wall,
        "retry_after_s": max(_MIN_RETRY, round(pressure.retry_after(now), 1)),
    }


def notice(store: PressureStore, provider: str, hint: str | None = None) -> str | None:
    """The line prepended to an assignment delivered while `provider` is held, else None."""
    if held(store, provider, hint) is None:
        return None
    return store.settings.notice + "\n"


def in_flight(bridge: Any) -> int:
    """Bridge-owned work in flight: Kiro ACP jobs, native CLI jobs and hc deliveries."""
    native = sum(
        len(getattr(adapter.controls, "jobs", ()))
        for adapter in bridge.adapters
        if adapter.name in NATIVE_PROVIDERS and getattr(adapter, "controls", None) is not None
    )
    return len(bridge.workers.jobs) + native + len(bridge.core_tasks.jobs)


def pressure_text(transition: str, pressure: Pressure, in_flight: int) -> str:
    """The owner notification for an `enter`, `resume` or `exit` transition."""
    if transition == "exit":
        return f"{pressure.provider} pressure cleared"
    if transition == "resume":
        return f"{pressure.provider} backoff ended: dispatch resumed (level {pressure.level})"
    return (
        f"{pressure.provider} throttled: dispatch paused until "
        f"{clock_time(pressure.until_wall)} (level {pressure.level}), "
        f"{in_flight} workers in flight"
    )


def request_id(transition: str, pressure: Pressure) -> str:
    """One notification per provider, account, transition and `until` second.

    An `exit` has no `until` (level 0), so it is keyed by the second the row went clean;
    keyed by 0 every later exit would dedupe into the first and never reach the inbox.
    A `resume` is keyed by the `until` it was armed for, one per hold.
    """
    stamp = pressure.until_wall or pressure.clean_since_wall or pressure.last_signal_wall
    return (f"pressure-{pressure.provider}-{pressure.account_hint}-{transition}-{int(stamp or 0)}")[
        :128
    ]


def _loop_call_later(delay: float, callback: Callable[[], None]) -> Any:
    """`call_later` on the running loop; None (nothing armed) when no loop runs here."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay, callback)


class ResumeTimers:
    """One pending "dispatch resumed" callback per held account, due when its hold ends.

    The store fires `exit` only when the level decays to 0, one clean window after the
    hold ended; the owner wants to hear at `until`. `arm()` on every `enter` or `raise`
    re-schedules the callback at the new `until` (a new signal extends the hold), and the
    callback re-reads the store before firing, so a hold extended in between is waited
    out again. `disarm()` on `exit` cancels a pending callback and says whether this hold's
    resume notice already went, so a decay to 0 after it adds no second notice.
    """

    def __init__(
        self,
        store: PressureStore,
        fire: Callable[[Pressure], None],
        schedule: Schedule = _loop_call_later,
    ) -> None:
        self.store = store
        self.fire = fire
        self.schedule = schedule
        self.pending: dict[tuple[str, str], Any] = {}
        self.told: set[tuple[str, str]] = set()

    def arm(self, pressure: Pressure) -> None:
        key = (pressure.provider, pressure.account_hint)
        self._cancel(key)
        self.told.discard(key)
        delay = pressure.retry_after(self.store.clock())
        if delay <= 0:
            return
        handle = self.schedule(delay, lambda: self._due(key))
        if handle is not None:
            self.pending[key] = handle

    def disarm(self, pressure: Pressure) -> bool:
        """Cancel the account's pending callback; True when its resume notice already went."""
        key = (pressure.provider, pressure.account_hint)
        self._cancel(key)
        told = key in self.told
        self.told.discard(key)
        return told

    def cancel_all(self) -> None:
        for key in list(self.pending):
            self._cancel(key)

    def _cancel(self, key: tuple[str, str]) -> None:
        handle = self.pending.pop(key, None)
        if handle is not None:
            handle.cancel()

    def _due(self, key: tuple[str, str]) -> None:
        self.pending.pop(key, None)
        current = self.store.status(*key)
        if current.retry_after(self.store.clock()) > 0:
            self.arm(current)  # Extended since this was armed: wait out the new hold.
            return
        if current.level <= 0:
            return  # Cleared meanwhile: `exit` already told the owner.
        self.told.add(key)
        self.fire(current)
