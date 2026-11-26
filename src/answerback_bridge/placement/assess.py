"""Pure capacity assessment and advice; callers own time, policy and any actions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .probe import LOCAL, Reading

State = Literal["ok", "busy", "full", "unknown"]
_SEVERITY: dict[State, int] = {"ok": 0, "busy": 1, "full": 2, "unknown": 3}
_PRESSURE_STATES: dict[int, State] = {1: "ok", 2: "busy", 4: "full"}


@dataclass(frozen=True)
class Thresholds:
    """Initial defaults for the owner to tune, expressed as ratios except PSI."""

    cpu_busy: float = 0.70
    cpu_full: float = 1.00
    compressor_busy: float = 0.25
    compressor_full: float = 0.40
    swap_busy: float = 0.25
    available_busy: float = 0.15
    available_full: float = 0.07
    stall_busy: float = 10
    largest_process_busy: float = 0.25
    largest_process_full: float = 0.50
    max_age: float = 60


@dataclass(frozen=True)
class Signal:
    """One independent observation and the state it implies."""

    name: str
    state: State
    text: str


@dataclass(frozen=True)
class Assessment:
    """The worst signal, with the measurements that explain it."""

    machine: str
    state: State
    load_per_core: float | None
    signals: tuple[Signal, ...]
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Return stable, plain JSON-compatible keys and lists."""
        return {
            "machine": self.machine,
            "state": self.state,
            "load_per_core": self.load_per_core,
            "signals": [asdict(signal) for signal in self.signals],
            "reasons": list(self.reasons),
        }


def _high(value: float, busy: float, full: float = math.inf) -> State:
    return "full" if value >= full else "busy" if value >= busy else "ok"


def assess(
    reading: Reading,
    *,
    now: float,
    thresholds: Thresholds = Thresholds(),
    active_agents: int | None = None,
    agent_ceiling: int | None = None,
) -> Assessment:
    """Assess a reading without I/O; missing essential evidence wins as unknown."""
    signals: list[Signal] = []
    if reading.error is not None:
        signals.append(Signal("reading", "unknown", f"Reading error: {reading.error}"))
    age = now - reading.taken_at
    if age > thresholds.max_age:
        signals.append(Signal("freshness", "unknown", f"Reading stale: {age:.1f}s old"))

    load = None
    if (
        reading.cores is None
        or reading.cores <= 0
        or reading.load1 is None
        or reading.load5 is None
        or reading.load15 is None
    ):
        signals.append(Signal("cpu", "unknown", "CPU cores or loads missing"))
    else:
        load = max(reading.load1, reading.load5) / reading.cores
        signals.append(
            Signal(
                "cpu",
                _high(load, thresholds.cpu_busy, thresholds.cpu_full),
                f"CPU load/core {load:.3f}",
            )
        )

    def ratio(name: str, value: int | None, busy: float, full: float = math.inf) -> None:
        if value is not None and reading.mem_total_kb is not None and reading.mem_total_kb > 0:
            fraction = value / reading.mem_total_kb
            signals.append(
                Signal(
                    name,
                    _high(fraction, busy, full),
                    f"{name} {fraction:.2%} of physical memory",
                )
            )

    if reading.os == "Darwin":
        if reading.pressure_level is not None:
            state: State = _PRESSURE_STATES.get(reading.pressure_level, "busy")
            signals.append(
                Signal(
                    "pressure",
                    state,
                    f"Memory pressure level {reading.pressure_level}",
                )
            )
        ratio(
            "compressor",
            reading.compressor_kb,
            thresholds.compressor_busy,
            thresholds.compressor_full,
        )
        ratio("swap", reading.swap_used_kb, thresholds.swap_busy)
    elif reading.os == "Linux":
        if (
            reading.mem_available_kb is not None
            and reading.mem_total_kb is not None
            and reading.mem_total_kb > 0
        ):
            available = reading.mem_available_kb / reading.mem_total_kb
            state = (
                "full"
                if available < thresholds.available_full
                else "busy"
                if available < thresholds.available_busy
                else "ok"
            )
            signals.append(
                Signal(
                    "available",
                    state,
                    f"available {available:.2%} of physical memory",
                )
            )
        if reading.psi_some_avg10 is not None:
            signals.append(
                Signal(
                    "stall",
                    _high(reading.psi_some_avg10, thresholds.stall_busy),
                    f"Memory stall some avg10 {reading.psi_some_avg10:.2f}%",
                )
            )
    ratio(
        "largest_process",
        reading.largest_process_kb,
        thresholds.largest_process_busy,
        thresholds.largest_process_full,
    )
    if not any(signal.name in {"pressure", "compressor", "available"} for signal in signals):
        signals.append(Signal("memory", "unknown", "No usable memory signal"))
    if active_agents is not None and agent_ceiling is not None:
        signals.append(
            Signal(
                "agents",
                "full" if active_agents >= agent_ceiling else "ok",
                f"Active agents {active_agents}, ceiling {agent_ceiling}",
            )
        )
    return Assessment(
        reading.machine,
        max((signal.state for signal in signals), key=_SEVERITY.__getitem__),
        load,
        tuple(signals),
        tuple(signal.text for signal in signals if signal.state != "ok"),
    )


def rank(
    assessments: Iterable[Assessment],
    *,
    coordinator: str = LOCAL,
    heavy: bool = True,
    order: Sequence[str] = (),
) -> list[str]:
    """Rank only ok machines by quarter-core load band, configured order and name."""
    positions = {name: index for index, name in reversed(list(enumerate(order)))}
    candidates = [
        row
        for row in assessments
        if row.state == "ok"
        and row.load_per_core is not None
        and (not heavy or row.machine != coordinator)
    ]
    candidates.sort(
        key=lambda row: (
            math.floor(row.load_per_core / 0.25) if row.load_per_core is not None else math.inf,
            positions.get(row.machine, len(order)),
            row.machine,
        )
    )
    return [row.machine for row in candidates]


@dataclass(frozen=True)
class Advice:
    """A suggestion for one machine; it never performs the suggested action."""

    machine: str
    level: int
    text: str
    targets: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Return a plain JSON-compatible advice record."""
        return {
            "machine": self.machine,
            "level": self.level,
            "text": self.text,
            "targets": list(self.targets),
        }


def advise(
    assessments: Iterable[Assessment],
    *,
    coordinator: str = LOCAL,
    previous: Iterable[Assessment] | None = None,
) -> list[Advice]:
    """Describe safe stage-boundary responses, using prior readings when supplied."""
    rows = list(assessments)
    targets = tuple(rank(rows, coordinator=coordinator))
    prior = {row.machine: row.state for row in previous or ()}
    advice = []
    for row in rows:
        if row.state == "ok":
            continue
        if row.state == "unknown":
            level = 0
            text = "Place nothing there until it can be measured."
        elif row.state == "full" and row.machine == coordinator:
            level = 3
            text = (
                "Place nothing new there; hand over working agents: each commits or writes "
                "a hand-over note and reports, then a new worker continues on a target."
            )
        elif row.state == "full" or prior.get(row.machine) == "busy":
            level = 2
            text = "Place nothing new there; move each lane's next stage at its boundary."
        else:
            level = 1
            text = "Place nothing heavy there; running turns finish."
        advice.append(Advice(row.machine, level, text, targets))
    return advice
