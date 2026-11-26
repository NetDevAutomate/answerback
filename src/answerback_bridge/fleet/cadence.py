"""When to measure and when to write (spec sections 4 and 9). Pure: every time is an argument."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..placement.assess import Assessment
from ..placement.probe import Reading

INTERVALS = {
    "normal": 15.0,
    "memory_busy": 30.0,
    "memory_full": 45.0,
    "paused": 45.0,
    "unmeasured": 45.0,
}
PRESSURE_CRITICAL = 4
SWAP_EXHAUSTED = 0.95
MIN_GAP_S = 5.0
HEARTBEAT_S = 30.0
CLOCK_JUMP_S = 5.0
LEAST_SLEEP_S = 0.1
_NOT_MEMORY = frozenset({"cpu", "agents"})


@dataclass(frozen=True)
class Pause:
    """Why worker probes are paused."""

    reason: str
    text: str


@dataclass(frozen=True)
class Cadence:
    """The mode word, the time to the next cycle, and the pause if there is one."""

    mode: str
    interval_s: float
    pause: Pause | None


def emergency(reading: Reading | None) -> Pause | None:
    """The emergency of the coordinator: macOS pressure level 4, or Linux swap at 95%.

    On macOS the swap total grows with use, so its share is never a stop there.
    """
    if reading is None or reading.error is not None:
        return None
    if reading.os == "Darwin" and reading.pressure_level == PRESSURE_CRITICAL:
        return Pause("pressure_critical", "kernel memory pressure level 4")
    total, free = reading.swap_total_kb, reading.swap_free_kb
    if reading.os == "Linux" and total and free is not None and total > 0:
        used = max(0, total - free) / total
        if used >= SWAP_EXHAUSTED:
            return Pause(
                "swap_exhausted",
                f"swap {min(round(used * 100), 100)}% in use (stop at 95%)",
            )
    return None


def choose(reading: Reading | None, assessment: Assessment | None) -> Cadence:
    """The mode from the coordinator's current reading. CPU load never pauses probes.

    Without a current reading the mode is ``unmeasured``: the longest interval, and worker
    probes go on (the emergency rule needs a reading).
    """
    if reading is None or assessment is None or reading.error is not None:
        return Cadence("unmeasured", INTERVALS["unmeasured"], None)
    pause = emergency(reading)
    if pause is not None:
        return Cadence("paused", INTERVALS["paused"], pause)
    states = {s.state for s in assessment.signals if s.name not in _NOT_MEMORY}
    mode = "memory_full" if "full" in states else "memory_busy" if "busy" in states else "normal"
    return Cadence(mode, INTERVALS[mode], None)


def should_write(
    changed: bool, since_last_write: float | None, since_last_attempt: float | None = None
) -> bool:
    """Write when changed and 5 s passed, or when 30 s passed (the heartbeat).

    The 5-second gap also holds between two attempts, successful or not.
    """
    if since_last_attempt is not None and since_last_attempt < MIN_GAP_S:
        return False
    if since_last_write is None:
        return True
    if since_last_write >= HEARTBEAT_S:
        return True
    return changed and since_last_write >= MIN_GAP_S


def next_wake(
    *,
    tick: float,
    last_write: float | None,
    pending: bool,
    last_attempt: float | None = None,
) -> float:
    """The earliest of the next tick, the heartbeat, and a change that waits for the gap.

    No wake other than the tick comes earlier than 5 s after the last attempt.
    """
    floor = -math.inf if last_attempt is None else last_attempt + MIN_GAP_S
    wakes = [tick]
    if last_write is not None:
        wakes.append(max(floor, last_write + HEARTBEAT_S))
        if pending:
            wakes.append(max(floor, last_write + MIN_GAP_S))
    elif pending and last_attempt is not None:
        wakes.append(floor)
    return min(wakes)


def next_tick(scheduled: float, interval: float, now: float) -> tuple[float, int]:
    """The next tick after a cycle that was due at ``scheduled``; missed ticks are skipped."""
    tick = scheduled + interval
    skipped = 0
    while tick < now:
        tick += interval
        skipped += 1
    return tick, skipped


def clock_jump(
    previous_wall: float | None, previous_mono: float | None, wall: float, mono: float
) -> float | None:
    """The disagreement of the two clocks since the last cycle when it is over 5 s."""
    if previous_wall is None or previous_mono is None:
        return None
    drift = (wall - previous_wall) - (mono - previous_mono)
    return drift if abs(drift) > CLOCK_JUMP_S else None
