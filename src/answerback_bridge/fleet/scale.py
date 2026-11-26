"""The pressure scale: the busy limit is 0.70 and the full limit 1.00 (spec section 5).

The limits are those of ``placement/assess.py``; they are read from its ``Thresholds``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..placement.assess import Thresholds
from ..placement.probe import Reading
from . import bounds

LIMITS = Thresholds()
BUSY = 0.70
FULL = 1.00
TOP = 2.0
NOT_FULL_TOP = 0.99
BUSY_TOP = 0.999
MEMORY_SIGNALS = frozenset(
    {"pressure", "compressor", "swap", "available", "stall", "largest_process"}
)


@dataclass(frozen=True)
class Point:
    """One signal on the scale: the published name, the fraction and two texts."""

    signal: str
    state: str
    fraction: float
    text: str
    sentence: str


def published(name: str) -> str:
    """The published signal word: ``cpu``, ``agents``, else ``memory``."""
    return name if name in ("cpu", "agents") else "memory"


def fraction(value: float, busy: float, full: float | None) -> float:
    """Map ``value`` so that ``busy`` is 0.70 and ``full`` is 1.00, never above 2.0.

    Linear from 0 to the busy limit, linear between the limits, the same slope beyond
    the full limit. Without a full limit the scale stops at 0.99.
    """
    if busy <= 0:
        return 0.0
    if full is None:
        return max(0.0, min(NOT_FULL_TOP, value / busy * BUSY))
    if value <= busy:
        result = value / busy * BUSY
    else:
        result = BUSY + (FULL - BUSY) * (value - busy) / (full - busy)
    return max(0.0, min(TOP, result))


def _band(value: float, state: str) -> float:
    """Keep the fraction inside the band of the state the assessment gave."""
    if state == "full":
        return round(max(FULL, min(TOP, value)), 3)
    if state == "busy":
        # Its value on the scale, below 1.00 so that a busy machine never shows a full
        # meter. A signal that cannot be full alone already stops at 0.99.
        return max(BUSY, min(BUSY_TOP, round(value, 3)))
    return round(max(0.0, min(BUSY - 0.01, value)), 3)


def _pct(value: float) -> str:
    return f"{min(round(value * 100), 9999)}%"


def _share(part: int | None, reading: Reading) -> float | None:
    total = reading.mem_total_kb
    if part is None or total is None or total <= 0:
        return None
    return part / total


def point(
    name: str,
    state: str,
    reading: Reading,
    load_per_core: float | None,
    *,
    active_agents: int | None = None,
    agent_ceiling: int | None = None,
) -> Point | None:
    """The scale point of one assessed signal, or None when it has no measured value."""
    t = LIMITS
    word = state
    value: float | None
    text: str
    sentence: str
    if name == "cpu":
        if load_per_core is None:
            return None
        value = fraction(load_per_core, t.cpu_busy, t.cpu_full)
        text = f"{min(load_per_core, 9999):.2f}"
        limit = t.cpu_full if word == "full" else t.cpu_busy
        sentence = f"load {load_per_core:.2f} per core ({word} at {limit:.2f})"
    elif name in ("compressor", "swap", "largest_process"):
        part = {
            "compressor": reading.compressor_kb,
            "swap": reading.swap_used_kb,
            "largest_process": reading.largest_process_kb,
        }[name]
        share = _share(part, reading)
        if share is None:
            return None
        busy, full = {
            "compressor": (t.compressor_busy, t.compressor_full),
            "swap": (t.swap_busy, None),
            "largest_process": (t.largest_process_busy, t.largest_process_full),
        }[name]
        value = fraction(share, busy, full)
        text = _pct(share)
        limit = full if word == "full" and full is not None else busy
        label = name.replace("_", " ")
        sentence = f"{label} {_pct(share)} of memory ({word} at {_pct(limit)})"
    elif name == "available":
        share = _share(reading.mem_available_kb, reading)
        if share is None:
            return None
        # A falling signal: measure the used share so that the scale still rises.
        value = fraction(1 - share, 1 - t.available_busy, 1 - t.available_full)
        text = _pct(share)
        limit = t.available_full if word == "full" else t.available_busy
        sentence = f"available {_pct(share)} of memory ({word} below {_pct(limit)})"
    elif name == "stall":
        if reading.psi_some_avg10 is None:
            return None
        value = fraction(reading.psi_some_avg10, t.stall_busy, None)
        text = f"{min(reading.psi_some_avg10, 999):.1f}"
        sentence = f"memory stall {reading.psi_some_avg10:.1f} ({word} at {t.stall_busy:g})"
    elif name == "pressure":
        level = reading.pressure_level
        if level is None:
            return None
        value = FULL if word == "full" else BUSY if word == "busy" else 0.0
        text = f"level {min(level, 999)}"
        sentence = f"memory pressure level {min(level, 999)} ({word})"
    elif name == "agents":
        if active_agents is None or agent_ceiling is None or agent_ceiling <= 0:
            return None
        value = min(TOP, active_agents / agent_ceiling)
        text = f"{min(active_agents, 999)}/{min(agent_ceiling, 99)}"
        sentence = f"{active_agents} active agents (ceiling {agent_ceiling})"
    else:
        return None
    return Point(
        published(name),
        word,
        _band(value, word),
        (bounds.clean(text, 8) or "?"),
        (bounds.clean(sentence, 80) or word),
    )
