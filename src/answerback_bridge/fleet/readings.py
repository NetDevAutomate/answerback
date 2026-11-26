"""One machine's reading: what was measured, its expiry, and its entry in the document."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..placement.assess import Assessment, assess
from ..placement.probe import Reading
from . import bounds, scale

MAX_AGE_S = 60.0
MEASURED = ("ok", "busy", "full")
STATES = ("ok", "busy", "full", "unknown")
ERRORS = {
    "timeout": "probe timed out",
    "unparsed": "probe answer not understood",
    "unreachable": "probe failed",
}
EXPIRED = "reading expired"
NO_LINK = "no link"
PAUSED = "probes paused"
INCOMPLETE = "reading incomplete"


@dataclass(frozen=True)
class Measured:
    """The last good reading of a machine and the moment its probe was sent."""

    reading: Reading
    assessment: Assessment
    sent_at: float
    sent_mono: float
    active: int | None
    ceiling: int | None


@dataclass(frozen=True)
class Memo:
    """What the writer remembers of a machine between cycles."""

    last: Measured | None = None
    error: str | None = None
    jumped: bool = False


def record(
    memo: Memo,
    reading: Reading,
    sent_at: float,
    sent_mono: float,
    *,
    active: int | None,
    ceiling: int | None,
) -> Memo:
    """The memo after one probe. A failure keeps the last good reading and its stamp."""
    if reading.error is not None:
        return replace(memo, error=ERRORS.get(reading.error, "probe failed"))
    with_agents = ceiling is not None and active is not None
    assessment = assess(
        reading,
        now=reading.taken_at,
        active_agents=active if with_agents else None,
        agent_ceiling=ceiling if with_agents else None,
    )
    if assessment.state not in MEASURED:
        return replace(memo, error=INCOMPLETE)
    return Memo(Measured(reading, assessment, sent_at, sent_mono, active, ceiling), None, False)


def fail(memo: Memo, error: str) -> Memo:
    """The memo when no probe answered for the stated reason."""
    return replace(memo, error=error)


def wait(memo: Memo, error: str) -> Memo:
    """No probe was started: keep the last reading without an error, else say why."""
    return memo if memo.last is not None else replace(memo, error=error)


def expire(memo: Memo) -> Memo:
    """Treat the last reading as expired (after a clock jump)."""
    return replace(memo, jumped=True) if memo.last is not None else memo


def is_current(memo: Memo, now_mono: float) -> bool:
    """True when the last probe succeeded and its reading is at most 60 s old."""
    last = memo.last
    return (
        last is not None
        and memo.error is None
        and not memo.jumped
        and now_mono - last.sent_mono <= MAX_AGE_S
    )


def is_fresh(memo: Memo, now_mono: float) -> bool:
    """True when the last good reading is at most 60 s old, whatever the last probe did."""
    last = memo.last
    return last is not None and not memo.jumped and now_mono - last.sent_mono <= MAX_AGE_S


def _memory(reading: Reading, assessment: Assessment) -> dict[str, Any]:
    states = [
        s.state for s in assessment.signals if s.name in scale.MEMORY_SIGNALS or s.name == "memory"
    ]
    order = {"ok": 0, "busy": 1, "full": 2, "unknown": 3}
    worst = max(states, key=order.__getitem__) if states else "unknown"
    parts: list[str] = []
    total = reading.mem_total_kb or 0

    def pct(kb: int | None) -> str | None:
        return None if kb is None or total <= 0 else f"{min(round(kb / total * 100), 9999)}%"

    gib = f"{total / 1024 / 1024:.0f}G" if total > 0 else "?"
    if reading.os == "Darwin":
        if reading.pressure_level is not None:
            parts.append(f"pressure {min(reading.pressure_level, 999)}")
        if pct(reading.compressor_kb):
            parts.append(f"compressor {pct(reading.compressor_kb)}")
        if reading.swap_used_kb is not None:
            parts.append(f"swap {reading.swap_used_kb / 1024 / 1024:.1f}G used")
    else:
        if pct(reading.mem_available_kb):
            parts.append(f"available {pct(reading.mem_available_kb)} of {gib}")
        if reading.swap_total_kb and reading.swap_free_kb is not None:
            used = max(0, reading.swap_total_kb - reading.swap_free_kb)
            share = min(round(used / reading.swap_total_kb * 100), 100)
            parts.append(f"swap {share}% of {reading.swap_total_kb / 1024 / 1024:.1f}G")
        if reading.psi_some_avg10 is not None:
            parts.append(f"stall {reading.psi_some_avg10:.1f}")
    if pct(reading.largest_process_kb):
        parts.append(f"largest process {pct(reading.largest_process_kb)}")
    return {"state": worst, "text": bounds.clean(", ".join(parts), bounds.TEXT)}


def _measured_fields(last: Measured) -> dict[str, Any]:
    r = last.reading
    loads = [r.load1, r.load5, r.load15]
    lpc = last.assessment.load_per_core
    return {
        "load_per_core": None if lpc is None else round(lpc, 3),
        "loads": loads if None not in loads else None,
        "cores": r.cores,
        "memory": _memory(r, last.assessment),
        "suites": r.suites_running,
        "harness_processes": r.harness_processes,
    }


def _decided(last: Measured, state: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if state not in ("busy", "full"):
        return [], None
    points = []
    for signal in last.assessment.signals:
        if signal.state != state:
            continue
        p = scale.point(
            signal.name,
            signal.state,
            last.reading,
            last.assessment.load_per_core,
            active_agents=last.active,
            agent_ceiling=last.ceiling,
        )
        if p is not None:
            points.append(p)
    decided = [{"signal": p.signal, "state": p.state, "text": p.sentence} for p in points]
    if not points:
        return decided, None
    top = max(points, key=lambda p: p.fraction)
    return decided, {"signal": top.signal, "fraction": top.fraction, "text": top.text}


def entry(
    name: str | None,
    memo: Memo,
    *,
    link: str,
    now_mono: float,
    active: int | None,
    ceiling: int | None,
    lanes: int | None = None,
) -> dict[str, Any]:
    """The machine's entry of the document; ``name`` None is the coordinator."""
    last = memo.last
    reading: dict[str, Any] = {
        "taken_at": None,
        "error": bounds.clean(memo.error, bounds.TEXT),
        "state": "unknown",
        "was": None,
        "decided_by": [],
        "pressure": None,
        "load_per_core": None,
        "loads": None,
        "cores": None,
        "memory": {"state": None, "text": None},
        "agents": {"active": active, "ceiling": ceiling, "lanes": lanes},
        "suites": None,
        "harness_processes": None,
    }
    os_word = None
    if last is not None:
        measured = last.assessment.state
        os_word = (last.reading.os or "").lower() or None
        reading.update(_measured_fields(last))
        reading["taken_at"] = last.sent_at
        if is_current(memo, now_mono):
            reading["state"] = measured
            reading["decided_by"], reading["pressure"] = _decided(last, measured)
        else:
            reading["was"] = measured
            reading["error"] = bounds.clean(memo.error, bounds.TEXT) or EXPIRED
    elif reading["error"] is None:
        reading["error"] = NO_LINK if link not in ("connected", "self") else "not measured yet"
    return {
        "name": name,
        "self": name is None,
        "role": "coordinator" if name is None else "worker",
        "os": os_word,
        "link": {
            "state": bounds.clean(link, bounds.NAME) or "unknown",
            "since_ms": None,
            "why": None,
        },
        "reading": reading,
    }
