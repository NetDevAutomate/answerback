"""Pure choice of a machine for one new worker; the caller owns time, I/O and the send.

`choose` reads no clock, file or process and sends nothing. It takes the measurement's
readings and the policy file's machine facts, and answers with one machine or with
`at_capacity`. Only a machine that `assess` calls `ok` is ever chosen.
`plan_rebalance` uses it only for the read-only `answerback rebalance` preview;
`route`, the pool and `answerback capacity` do not import this module.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from .assess import Assessment, assess, rank
from .facts import Facts, MachineFacts, NumberRefused, finite_from_zero
from .probe import LOCAL, Reading

RECENT_SECONDS = 120.0
# A load beyond this, either way, is not a measurement; `rank` could not band it.
MAX_LOAD = 1e9


@dataclass(frozen=True)
class Placement:
    """One placement the caller made at time `at`: a new worker, or a send to a terminal."""

    machine: str
    at: float


@dataclass(frozen=True)
class Skip:
    """A machine that was not eligible, and why."""

    machine: str
    reason: str


@dataclass(frozen=True)
class Choice:
    """The answer: `placed` with a machine and its project folder, or `at_capacity`.

    `ranked` holds every eligible machine, the chosen one first. `assessed` holds the
    assessment of every machine that reached `assess`, made on its reading with the
    recent placements added. `skipped` names every machine
    that is not in `ranked` once, with the reason.
    `skipped` and `assessed` follow the order of the readings; machines with facts and no
    reading come after them in `skipped`, by name.
    """

    status: Literal["placed", "at_capacity"]
    machine: str | None
    project: str | None
    ranked: tuple[str, ...]
    skipped: tuple[Skip, ...]
    assessed: tuple[Assessment, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return a plain JSON-compatible record."""
        return {
            "status": self.status,
            "machine": self.machine,
            "project": self.project,
            "ranked": list(self.ranked),
            "skipped": [{"machine": row.machine, "reason": row.reason} for row in self.skipped],
            "assessed": [row.to_json() for row in self.assessed],
        }


def _not_eligible(
    machine: MachineFacts, harness: str, heavy: bool, tools: frozenset[str]
) -> str | None:
    """Why the recorded facts keep this machine out, or None."""
    if heavy and machine.name == LOCAL:
        return "the coordinator takes no heavy work"
    if machine.project is None:
        return "no project folder recorded"
    entry = machine.harnesses.get(harness)
    if entry is None:
        return f"harness {harness} is not recorded there"
    if not entry.ready:
        return f"harness {harness} is not ready there" + (f": {entry.why}" if entry.why else "")
    if missing := sorted(tools - machine.tools):
        return f"tools not recorded there: {', '.join(missing)}"
    return None


def _live(active: Mapping[str, Mapping[str, int]] | None) -> dict[str, dict[str, int]]:
    """The caller's counts of live workers, checked: a count is a whole number from 0."""
    result: dict[str, dict[str, int]] = {}
    for machine, counts in (active or {}).items():
        for harness, count in counts.items():
            if type(count) is not int or count < 0:
                raise ValueError(f"active[{machine!r}][{harness!r}] must be an int from 0")
        result[machine] = dict(counts)
    return result


def choose(
    readings: Iterable[Reading],
    facts: Facts,
    *,
    harness: str,
    now: float,
    heavy: bool = True,
    tools: Iterable[str] = (),
    recent: Iterable[Placement] = (),
    active: Mapping[str, Mapping[str, int]] | None = None,
) -> Choice:
    """Choose a machine for one new worker of `harness`, or answer `at_capacity`.

    `now`, every `taken_at` and every `at` come from one clock. `recent` is the caller's
    list of placements; each one dated less than or exactly `RECENT_SECONDS` before `now`,
    or after `now`, adds `facts.worker_cost` to each of the one- and five-minute load that
    the reading of its machine holds, before that machine is assessed.
    `active[machine][harness]` is the caller's count of live workers, which must include
    a worker from the moment the caller starts it: the ceilings `max_workers` and
    `max_total` are held against these counts alone, and a machine that has a ceiling for
    this choice and no entry in `active` is skipped.

    A machine is eligible only when it has a reading and facts; the facts name a project
    folder, the harness as ready and every tool of `tools`; the reading is dated at or
    before `now`; every load in it is a number from `-MAX_LOAD` to `MAX_LOAD` and, with
    the recent placements added, at most `MAX_LOAD`; no ceiling is reached; and `assess`, with the
    thresholds of the facts, calls it `ok`. The facts are a person's
    record: nothing here measures a tool, a folder or a harness.

    Heavy work never goes to the coordinator (the reading named `local`); light work goes
    there only when no other machine is eligible. Among the others the order is:
    quarter-core band of the load with the recent placements added, fewest recent
    placements, position in `facts.order`, name. No machine eligible gives `at_capacity`
    with no machine: there is no fallback.

    The readings are as `parse_reading` makes them, and `choose` relies on the bounds that
    parser sets on every number. What `choose` does with a `Reading` built by hand whose
    numbers lie outside those bounds is not defined. For every value of the facts other
    than `worker_cost`, `choose` relies on facts as `parse_facts` makes them: what it does
    with a `Facts`, `MachineFacts`, `HarnessFacts` or `Thresholds` built directly with a
    value that the reader would refuse is not defined.

    Each of `readings`, `tools` and `recent` is read once. `choose` keeps nothing between
    calls, so a caller that places several pieces of work must pass each earlier placement
    in `recent`. A second call with the same values in the same float representation, the
    sign of a zero included, read in the same order, gives an equal answer; two `Facts`
    that are equal count as the same value here. Readings that differ only in the sign of
    a zero give the same status, machine, project and ranking; the diagnostic text in
    `assessed` may differ.

    Raises ValueError for two readings of one machine, a `now` or an `at` that is not
    finite, and a count in `active` that is not an int from 0. Raises ValueError also when
    `facts.worker_cost` is a value that `parse_facts` would refuse as `worker_cost`, with
    or without a reading or a recent placement.
    """
    try:
        cost = finite_from_zero(facts.worker_cost)
    except NumberRefused:
        raise ValueError("facts.worker_cost must be a finite number from 0") from None
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    measured: dict[str, Reading] = {}
    for reading in readings:
        if reading.machine in measured:
            raise ValueError(f"Two readings for one machine: {reading.machine}")
        measured[reading.machine] = reading
    needed = frozenset(tools)
    live = _live(active)
    placed: Counter[str] = Counter()
    for item in recent:
        if not math.isfinite(item.at):
            raise ValueError(f"Placement on {item.machine} has a time that is not finite")
        if now - item.at <= RECENT_SECONDS:
            placed[item.machine] += 1

    skipped: list[Skip] = []
    assessed: list[Assessment] = []
    for name in (*measured, *sorted(name for name in facts.machines if name not in measured)):
        reading, machine = measured.get(name), facts.machines.get(name)
        if reading is None:
            skipped.append(Skip(name, "not measured"))
            continue
        if machine is None:
            skipped.append(Skip(name, "no machine facts"))
            continue
        if reason := _not_eligible(machine, harness, heavy, needed):
            skipped.append(Skip(name, reason))
            continue
        if not reading.taken_at <= now:
            skipped.append(Skip(name, "not measured: the reading is not dated at or before now"))
            continue
        loads = (reading.load1, reading.load5, reading.load15)
        if not all(load is None or abs(load) <= MAX_LOAD for load in loads):
            skipped.append(Skip(name, "not measured: a load is out of range"))
            continue
        ceiling = machine.harnesses[harness].max_workers
        if name not in live and (ceiling is not None or machine.max_total is not None):
            skipped.append(Skip(name, "it has a ceiling and active holds no count for it"))
            continue
        workers = live.get(name, {})
        if ceiling is not None and workers.get(harness, 0) >= ceiling:
            skipped.append(Skip(name, f"at max_workers {ceiling} for {harness}"))
            continue
        added = placed[name] * cost
        if added:
            load1 = None if reading.load1 is None else reading.load1 + added
            load5 = None if reading.load5 is None else reading.load5 + added
            # The cost was checked on entry: it is a finite float from 0, and the count is
            # a whole number from 0. So `added`, not zero here, is above 0 (it may be
            # infinite), and the sum cannot fall below the raw load's bound.
            if not all(load is None or load <= MAX_LOAD for load in (load1, load5)):
                reason = "not measured: the load with the recent placements added is out of range"
                skipped.append(Skip(name, reason))
                continue
            reading = replace(reading, load1=load1, load5=load5)
        result = assess(
            reading,
            now=now,
            thresholds=facts.thresholds,
            active_agents=sum(workers.values()),
            agent_ceiling=machine.max_total,
        )
        assessed.append(result)
        if result.state != "ok":
            skipped.append(Skip(name, f"{result.state}: {'; '.join(result.reasons)}"))

    position = {name: index for index, name in enumerate(facts.order)}
    order = sorted(
        (row.machine for row in assessed),
        key=lambda name: (placed[name], position.get(name, len(position)), name),
    )
    ranked = rank(assessed, coordinator=LOCAL, heavy=heavy, order=order)
    if LOCAL in ranked:
        ranked = [name for name in ranked if name != LOCAL] + [LOCAL]
    if not ranked:
        return Choice("at_capacity", None, None, (), tuple(skipped), tuple(assessed))
    project = facts.machines[ranked[0]].project
    return Choice("placed", ranked[0], project, tuple(ranked), tuple(skipped), tuple(assessed))
