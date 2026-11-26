"""Pure plan of at most one move of a worker to another machine; nothing here acts.

`plan_rebalance` reads no clock, file, environment or process and sends nothing. The
caller passes the time, two readings per machine, the machine facts, the managed
workers, the machines the bridge may send to and the log of moves. The answer names one
worker that should continue on another machine, or `NOTHING_MOVES` with the cause.

A move is a hand-over, not a transport: a running turn is never carried to another
machine. This module only names the move. No code applies one yet.

The classes are those of `assess` and the target is the one `choose` ranks first among
the takers, so `capacity`, `choose` and this plan cannot disagree on one reading.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal

from .assess import Assessment, State, assess
from .choose import MAX_LOAD, RECENT_SECONDS, Placement, choose
from .facts import Facts, NumberRefused, finite_from_zero
from .probe import LOCAL, Reading

NOTHING_MOVES = "NOTHING_MOVES"
# The machine that coordinates: it may give work and never takes any.
COORDINATOR = LOCAL
# A taker's load per core, with one more worker added, must be under this: 0.30 below the
# busy line of `assess` (0.70).
TAKER_CEILING = 0.40
# The giver's load per core must exceed the taker's projected one by this: the width of
# the dead band between the taker ceiling and the busy line.
MIN_GAP = 0.30
# The newest reading of a machine counts only when it is at most this old. The facts may
# set a smaller `max_age` and never a larger one for a plan.
MAX_AGE = 60.0
# Two readings of one machine count as two only this far apart: 60 s is the age limit of
# a reading, 600 s the largest `max_age` the facts allow.
MIN_SPACING = MAX_AGE
MAX_SPACING = 600.0
# A machine that gave work takes none for this long.
MACHINE_COOLDOWN = 300.0
# A worker that moved, and its successor, stay for this long.
WORKER_COOLDOWN = 1800.0
# Moves in the whole fleet at once, and so in one plan.
MAX_MOVES = 1

Work = Literal["queued", "idle", "working", "finished"]
Kind = Literal["resubmit_queued", "hand_over", "at_boundary"]
_KINDS: dict[str, Kind] = {
    "queued": "resubmit_queued",
    "idle": "hand_over",
    "working": "at_boundary",
}
_WORK_ORDER = {"queued": 0, "idle": 1, "working": 2}
# A receipt in one of these states is a person's to settle; its worker stays.
_STAYING_RECEIPTS = frozenset({"uncertain", "blocked", "unknown"})
_MEMORY_SIGNALS = frozenset(
    {"pressure", "compressor", "swap", "available", "stall", "largest_process", "memory"}
)
_SEVERITY: dict[str, int] = {"ok": 0, "busy": 1, "full": 2, "unknown": 3}
_NUMBER_FIELDS = {
    item.name: (int,) if "int" in str(item.type) else (int, float)
    for item in fields(Reading)
    if "int" in str(item.type) or "float" in str(item.type)
}


@dataclass(frozen=True)
class ManagedWorker:
    """One managed worker as the caller knows it. The planner looks nothing up.

    `work` is the state of its work: `queued` (not delivered), `idle` (idle with
    context), `working`, or `finished`. `lane_stays` is true for a lane that must stay on
    the coordinator and `gate_class` for a class that runs the test gate: the caller
    decides both from the policy, and neither has a default. `since` is the time the
    worker was created; the oldest moves first.
    """

    id: str
    machine: str
    harness: str
    task_class: str
    lane: str | None
    work: Work
    receipt: str | None
    opened_by_person: bool
    lane_stays: bool
    gate_class: bool
    since: float = 0.0
    task: str | None = None


@dataclass(frozen=True)
class MoveRecord:
    """One entry of the move log: the worker that gave, where, when, and its successor."""

    id: str
    worker: str
    giver: str
    taker: str
    at: float
    open: bool = False
    successor: str | None = None


@dataclass(frozen=True)
class MachineView:
    """How the plan saw one machine. `role` is `giver`, `taker` or empty.

    `why` says why the machine is not a taker and is empty for a taker. `spacing` says
    why the earlier reading does not count and is empty when it does.
    """

    machine: str
    state: State
    was: State | None
    load: float | None
    projected: float | None
    memory: State
    workers: int
    ceiling: int | None
    harness_processes: int | None
    role: str
    why: str
    spacing: str

    def to_json(self) -> dict[str, Any]:
        """Return a plain JSON-compatible record."""
        return {
            "machine": self.machine,
            "state": self.state,
            "was": self.was,
            "load_per_core": self.load,
            "projected_load_per_core": self.projected,
            "memory": self.memory,
            "workers": self.workers,
            "ceiling": self.ceiling,
            "harness_processes": self.harness_processes,
            "role": self.role,
            "why": self.why,
            "spacing": self.spacing,
        }


@dataclass(frozen=True)
class Move:
    """The one move of a plan. The readings are (earlier, newest) of each side."""

    worker: str
    task: str | None
    harness: str
    work: Work
    kind: Kind
    giver: str
    taker: str
    giver_load: float
    projected: float
    gap: float
    giver_readings: tuple[Reading, Reading]
    taker_readings: tuple[Reading, Reading]

    def to_json(self) -> dict[str, Any]:
        """Return a plain JSON-compatible record."""
        return {
            "worker": self.worker,
            "task": self.task,
            "harness": self.harness,
            "work": self.work,
            "kind": self.kind,
            "giver": self.giver,
            "taker": self.taker,
            "giver_load_per_core": self.giver_load,
            "projected_load_per_core": self.projected,
            "gap": self.gap,
            "giver_readings": [item.to_json() for item in self.giver_readings],
            "taker_readings": [item.to_json() for item in self.taker_readings],
        }


@dataclass(frozen=True)
class Plan:
    """`status` is `move`, with `id` and `move`, or `NOTHING_MOVES`, with `cause`."""

    status: str
    id: str | None
    cause: str | None
    notes: tuple[str, ...]
    move: Move | None
    machines: tuple[MachineView, ...]

    def to_json(self) -> dict[str, Any]:
        """Return a plain JSON-compatible record."""
        return {
            "status": self.status,
            "id": self.id,
            "cause": self.cause,
            "notes": list(self.notes),
            "move": None if self.move is None else self.move.to_json(),
            "machines": [view.to_json() for view in self.machines],
        }


def _assessed(
    reading: Reading, at: float, facts: Facts, agents: int, ceiling: int | None
) -> Assessment | None:
    """The assessment of `assess`, or None for a reading that is no measurement at `at`."""
    loads = (reading.load1, reading.load5, reading.load15)
    if not _numbers(reading) or not 0 <= at - reading.taken_at <= MAX_AGE:
        return None
    if not all(load is None or abs(load) <= MAX_LOAD for load in loads):
        return None
    return assess(
        reading, now=at, thresholds=facts.thresholds, active_agents=agents, agent_ceiling=ceiling
    )


def _numbers(reading: Reading) -> bool:
    """True when every number of the reading is a finite number of its kind, or absent.

    A reading that fails this is not measured: nothing is computed from it.
    """
    for name, kinds in _NUMBER_FIELDS.items():
        value = getattr(reading, name)
        if value is None and name != "taken_at":
            continue
        if type(value) not in kinds:
            return False
        try:
            if not math.isfinite(value):
                return False
        except OverflowError:
            # An integer too large to be a float.
            return False
    return True


def _memory(result: Assessment | None) -> State:
    states: list[State] = [
        signal.state
        for signal in (result.signals if result else ())
        if signal.name in _MEMORY_SIGNALS
    ]
    return max(states, key=_SEVERITY.__getitem__) if states else "unknown"


def _stays(worker: ManagedWorker, log: list[MoveRecord], now: float) -> str | None:
    """Why this worker may not move, or None."""
    if worker.work not in _KINDS:
        return "its work is finished" if worker.work == "finished" else "its work is not known"
    if worker.lane_stays:
        return "its lane stays on the coordinator"
    if worker.gate_class:
        return "its class runs the test gate"
    if worker.receipt in _STAYING_RECEIPTS:
        return f"its receipt is {worker.receipt}"
    if worker.opened_by_person:
        return "a person opened its terminal"
    for record in log:
        if worker.id in (record.worker, record.successor) and now - record.at < WORKER_COOLDOWN:
            return f"it moved in the last {WORKER_COOLDOWN / 60:.0f} min"
    return None


def _plan_id(move: Move) -> str:
    """An id made of the move and the times of its readings: same input, same id."""
    parts = [move.worker, move.giver, move.taker, move.kind]
    parts += [repr(item.taken_at) for item in (*move.giver_readings, *move.taker_readings)]
    return "R" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:8]


def plan_rebalance(
    now: float,
    readings: Mapping[str, Reading],
    previous: Mapping[str, Reading],
    facts: Facts,
    workers: Iterable[ManagedWorker],
    send_machines: Iterable[str],
    moves: Iterable[MoveRecord],
) -> Plan:
    """Name at most one worker that should continue on another machine.

    `now`, every `taken_at`, every `since` and every `at` come from one clock.
    `readings[m]` is the newest reading of machine `m` and `previous[m]` the one before
    it; the earlier one counts only when the two are `MIN_SPACING` to `MAX_SPACING`
    apart. A newest reading older than `MAX_AGE`, and a reading with a field that is
    not a number of its kind, is not measured: its machine neither gives nor takes.
    `workers` holds the managed workers only. `send_machines` is the bridge's list of
    machines it may send to. `moves` is the move log.

    A machine gives when `assess` calls it `full`, or `busy` on both readings, and it
    holds a managed worker that may move. A machine takes when it is `ok` on both
    readings, is not the coordinator, is listed in `send_machines`, has facts with a
    project folder and a harness recorded as ready, would stay under `TAKER_CEILING`
    with one more worker, and gave no work in the last `MACHINE_COOLDOWN` seconds. A
    machine that is `full`, or was not measured, never takes, and a machine that gives
    does not take in the same plan.

    A worker stays when its lane must stay on the coordinator, its class runs the test
    gate, its receipt is uncertain, blocked or unknown, a person opened its terminal,
    its work is finished, or it moved in the last `WORKER_COOLDOWN` seconds. With an
    open move in the log nothing moves.

    Givers are tried in this order: the coordinator, `full` before `busy`, the higher
    load, the name. Their workers in this order: queued and not delivered, idle with
    context, working; then the oldest; then the id. The target is the first machine in
    the ranking of `choose`, restricted to the takers; when its projected load does not
    lie at least `MIN_GAP` under the giver's load, the next worker is tried.

    Raises ValueError for a `now` or a time of a move that is not finite, a reading
    filed under another machine's name, a worker given twice, and a `worker_cost` the
    reader of the facts would refuse.
    """
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    for label, group in (("readings", readings), ("previous", previous)):
        for name, reading in group.items():
            if reading.machine != name:
                raise ValueError(f"{label} names {name!r} for a reading of {reading.machine!r}")
    try:
        cost = finite_from_zero(facts.worker_cost)
    except NumberRefused:
        raise ValueError("facts.worker_cost must be a finite number from 0") from None
    staff = sorted(workers, key=lambda worker: worker.id)
    for first, second in zip(staff, staff[1:], strict=False):
        if first.id == second.id:
            raise ValueError(f"worker {first.id!r} is given twice")
    log = list(moves)
    for record in log:
        if not math.isfinite(record.at):
            raise ValueError(f"move {record.id!r} has a time that is not finite")
    listed = frozenset(send_machines)

    live = {name: Counter[str]() for name in readings}
    for worker in staff:
        live.setdefault(worker.machine, Counter())[worker.harness] += 1
    stays = {worker.id: _stays(worker, log, now) for worker in staff}
    movable = {
        name: [worker for worker in staff if worker.machine == name and stays[worker.id] is None]
        for name in readings
    }
    recent = [Placement(record.taker, record.at) for record in log]
    placed = Counter(item.machine for item in recent if now - item.at <= RECENT_SECONDS)
    gave = {record.giver for record in log if now - record.at < MACHINE_COOLDOWN}

    state: dict[str, State] = {}
    was: dict[str, State | None] = {}
    load: dict[str, float | None] = {}
    projected: dict[str, float | None] = {}
    memory: dict[str, State] = {}
    spacing: dict[str, str] = {}
    for name, reading in readings.items():
        machine = facts.machines.get(name)
        ceiling = None if machine is None else machine.max_total
        agents = sum(live[name].values())
        result = _assessed(reading, now, facts, agents, ceiling)
        state[name] = "unknown" if result is None else result.state
        per_core = None if result is None else result.load_per_core
        load[name] = per_core
        memory[name] = _memory(result)
        projected[name] = None
        if per_core is not None and reading.cores:
            projected[name] = per_core + cost * (placed[name] + 1) / reading.cores
        earlier = previous.get(name)
        was[name] = None
        if earlier is None:
            spacing[name] = "first reading"
            continue
        if not _numbers(reading) or not _numbers(earlier):
            spacing[name] = "a reading is not a measurement"
            continue
        apart = reading.taken_at - earlier.taken_at
        if not MIN_SPACING <= apart <= MAX_SPACING:
            spacing[name] = (
                f"earlier reading {apart:.0f} s before: "
                f"{'too close' if apart < MIN_SPACING else 'too far apart'}"
            )
            continue
        spacing[name] = ""
        before = _assessed(earlier, earlier.taken_at, facts, agents, ceiling)
        was[name] = "unknown" if before is None else before.state

    loaded = [
        name
        for name in readings
        if state[name] == "full" or (state[name] == "busy" and was[name] == "busy")
    ]
    loaded.sort(
        key=lambda name: (
            name != COORDINATOR,
            state[name] != "full",
            -(load[name] or 0.0),
            name,
        )
    )
    givers = [name for name in loaded if movable[name]]

    def not_a_taker(name: str) -> str:
        machine = facts.machines.get(name)
        estimate = projected[name]
        if name == COORDINATOR:
            return "the coordinator takes no work"
        if state[name] != "ok":
            return "not measured" if state[name] == "unknown" else state[name]
        if was[name] is None:
            return spacing[name]
        if was[name] != "ok":
            return f"was {was[name]}"
        if machine is None:
            return "no machine facts"
        if machine.project is None:
            return "no project folder recorded"
        if not any(entry.ready for entry in machine.harnesses.values()):
            return "no harness recorded as ready"
        if name not in listed:
            return "not listed in hc_send_machines"
        if estimate is None or not estimate < TAKER_CEILING:
            shown = "?" if estimate is None else f"{estimate:.2f}"
            return f"projected load/core {shown} is not under {TAKER_CEILING:.2f}"
        if name in gave:
            return f"gave work in the last {MACHINE_COOLDOWN / 60:.0f} min"
        return ""

    why = {name: not_a_taker(name) for name in readings}
    takers = [name for name in readings if not why[name] and name not in loaded]
    views = tuple(
        MachineView(
            machine=name,
            state=state[name],
            was=was[name],
            load=load[name],
            projected=projected[name],
            memory=memory[name],
            workers=sum(live[name].values()),
            ceiling=None if name not in facts.machines else facts.machines[name].max_total,
            harness_processes=readings[name].harness_processes,
            role="giver" if name in givers else "taker" if name in takers else "",
            why="" if name in takers else why[name] or state[name],
            spacing=spacing[name],
        )
        for name in readings
    )

    def nothing(cause: str, *notes: str) -> Plan:
        return Plan(NOTHING_MOVES, None, cause, tuple(notes), None, views)

    for record in log:
        if record.open:
            return nothing(f"move {record.id} in flight")
    if not any(value is not None for value in was.values()):
        return nothing(
            "one reading only",
            f"A plan needs two readings of a machine, {MIN_SPACING:.0f} s to "
            f"{MAX_SPACING:.0f} s apart.",
            *(f"{name}: {spacing[name]}." for name in readings),
        )
    if not loaded:
        return nothing("no machine is full, or busy on two readings")
    if not givers:
        empty = [name for name in loaded if not live[name]]
        if empty:
            name = empty[0]
            twice = "" if state[name] == "full" else " on two readings"
            room = (
                f"New work would fit on {', '.join(takers)}."
                if takers
                else "No other machine has room; new work waits."
            )
            return nothing(
                f"no worker load on {name}",
                f"{name} is {state[name]}{twice}, but holds no managed worker.",
                "Its load comes from processes this command may not move.",
                room,
            )
        return nothing(
            f"no worker may move: every managed worker on {', '.join(loaded)} stays",
            *(
                f"{worker.id}: {stays[worker.id]}."
                for worker in staff
                if worker.machine in loaded and stays[worker.id]
            ),
        )
    if not takers:
        return nothing("no machine has room; new work waits")

    refusals: list[str] = []
    for giver in givers:
        giver_load = load[giver]
        if giver_load is None:
            continue
        candidates = sorted(
            movable[giver],
            key=lambda worker: (_WORK_ORDER[worker.work], worker.since, worker.id),
        )
        for worker in candidates:
            choice = choose(
                [readings[name] for name in takers],
                facts,
                harness=worker.harness,
                now=now,
                heavy=True,
                recent=recent,
                active=live,
            )
            if not choice.ranked:
                refusals.append(f"no taker is ready for {worker.harness} ({worker.id})")
                continue
            # Only the machine `choose` ranks first is asked; the next worker follows.
            taker = choice.ranked[0]
            estimate = projected[taker]
            if estimate is None:
                continue
            gap = giver_load - estimate
            if gap < MIN_GAP:
                refusals.append(f"gap {gap:.2f} from {giver} to {taker} is below {MIN_GAP:.2f}")
                continue
            move = Move(
                worker=worker.id,
                task=worker.task,
                harness=worker.harness,
                work=worker.work,
                kind=_KINDS[worker.work],
                giver=giver,
                taker=taker,
                giver_load=giver_load,
                projected=estimate,
                gap=gap,
                giver_readings=(previous.get(giver, readings[giver]), readings[giver]),
                taker_readings=(previous[taker], readings[taker]),
            )
            return Plan("move", _plan_id(move), None, (), move, views)
    unique = list(dict.fromkeys(refusals))
    return nothing(
        f"no worker may move: {unique[0]}" if unique else "no worker may move", *unique[1:]
    )
