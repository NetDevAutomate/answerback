"""`answerback rebalance`: a preview of at most one move. It applies nothing.

The command measures the machines with the probe of `answerback capacity`, keeps the
last two readings of each machine in a file of its own beside the bridge configuration,
asks the pure planner of `rebalance` and prints what it answers. It starts, stops,
interrupts and sends to nothing, and it writes no file but its readings file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, NoReturn

from ..config import Config
from ..routing.pool import REGISTRY_NAME, Registry
from .cli import _machine
from .facts import Facts, load_facts
from .probe import LOCAL, Reading, Runner, read, validate_machines
from .rebalance import (
    NOTHING_MOVES,
    TAKER_CEILING,
    MachineView,
    ManagedWorker,
    Plan,
    plan_rebalance,
)

READINGS_NAME = "placement-readings.json"
READINGS_VERSION = 1
# Readings kept of each machine: the newest and the one before it.
KEPT = 2
MAX_READINGS_BYTES = 256 * 1024
# The policy file of the project the command is run in; its `placement` block holds the facts.
POLICY_PATH = Path("status") / "routing.json"
# The probe's time bound per machine, the default of `answerback capacity`.
PROBE_TIMEOUT = 10.0
_FIELDS = {item.name: str(item.type) for item in fields(Reading)}
_WORK_WORDS = {"queued": "queued, not delivered", "idle": "idle with context", "working": "working"}


def wall_clock() -> float:
    """The one clock of the command; the planner never reads it."""
    return time.time()


def add_parser(commands: argparse._SubParsersAction) -> None:
    """Register the preview. It has no option that applies or forces a move."""
    parser = commands.add_parser(
        "rebalance",
        help="Preview at most one move of a worker to another machine; changes nothing",
        description=(
            "Measure the machines, keep the last two readings of each, and print the one "
            "move the planner would name, or NOTHING_MOVES with the cause. This command "
            "is a preview: it starts, stops and sends to nothing."
        ),
    )
    parser.add_argument(
        "--machine",
        action="append",
        type=_machine,
        default=[],
        metavar="NAME",
        help="Also measure this machine; repeat for more (maximum 16 including local)",
    )
    parser.add_argument("--json", action="store_true", help="Print the plan as JSON")
    parser.set_defaults(rebalance_parser=parser)


def readings_path(config_path: Path) -> Path:
    """The readings file: beside the bridge configuration."""
    return config_path.expanduser().absolute().parent / READINGS_NAME


def _finite(value: Any) -> bool:
    """True when `value` is an int or a float, not a boolean, and finite; never an error.

    A negative number is finite: the time of a reading is the command's clock, and a
    clock set before 1970 gives one. An integer too large for a float is not finite.
    """
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _reading(name: str, row: Any) -> Reading:
    if not isinstance(row, dict) or not set(row) <= set(_FIELDS) or row.get("machine") != name:
        raise ValueError("not a reading of its machine")
    for key, value in row.items():
        kind = _FIELDS[key]
        if value is None and key not in ("machine", "taken_at"):
            continue
        if "float" in kind:
            fits = _finite(value)
        elif "int" in kind:
            fits = type(value) is int
        else:
            fits = isinstance(value, str)
        if not fits:
            raise ValueError(f"field {key}")
    return Reading(**row)


def load_readings(path: Path) -> dict[str, list[Reading]]:
    """The kept readings by machine, oldest first; empty when there is no file yet.

    Raises ValueError for a file that cannot be read as this command wrote it. The file
    is then left as it is: a person decides what to do with it.
    """
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_READINGS_BYTES + 1)
    except FileNotFoundError:
        return {}
    try:
        if len(raw) > MAX_READINGS_BYTES:
            raise ValueError("too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != READINGS_VERSION:
            raise ValueError("version")
        machines = data.get("machines")
        if not isinstance(machines, dict):
            raise ValueError("machines")
        kept: dict[str, list[Reading]] = {}
        for name, rows in machines.items():
            if not isinstance(rows, list) or len(rows) > KEPT:
                raise ValueError("rows")
            kept[validate_machines([name])[0]] = [_reading(name, row) for row in rows]
        return kept
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            f"The readings file cannot be read: {path}. Remove it to start with a first reading."
        ) from None


def save_readings(path: Path, kept: dict[str, list[Reading]]) -> None:
    """Write the file whole, mode 0600, and move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {
                    "version": READINGS_VERSION,
                    "machines": {
                        name: [item.to_json() for item in rows] for name, rows in kept.items()
                    },
                },
                stream,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def row_machine(row: dict[str, Any]) -> str:
    """The machine of a worker row; a row without the field is of this machine."""
    machine = row.get("machine")
    return machine if isinstance(machine, str) and machine else LOCAL


def _since(value: object) -> float:
    """The `created_at` of a worker row as a finite number; anything else is 0.0, never an error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def managed_workers(config_path: Path) -> list[ManagedWorker]:
    """The rows of the worker registry, read and never written.

    The state of a worker's work and of its receipt is known to the running bridge and
    the terminal host only, and this command asks neither. So each worker is passed as
    `working` with an `unknown` receipt, which the planner protects: the preview counts
    the workers of a machine and names none of them for a move.
    """
    rows = Registry(config_path.parent / REGISTRY_NAME).load()
    workers = []
    for address, row in sorted(rows.items()):
        task = row.get("task")
        workers.append(
            ManagedWorker(
                id=address,
                machine=row_machine(row),
                harness=str(row.get("harness") or "?"),
                task_class=str(row.get("class") or "?"),
                lane=None,
                work="working",
                receipt="unknown",
                opened_by_person=False,
                lane_stays=False,
                gate_class=False,
                since=_since(row.get("created_at")),
                task=task if isinstance(task, str) else None,
            )
        )
    return workers


def _machine_line(view: MachineView, width: int) -> str:
    load = "?" if view.load is None else f"{view.load:.2f}"
    was = view.spacing or "first reading" if view.was is None else f"was {view.was}"
    workers = str(view.workers) if view.ceiling is None else f"{view.workers}/{view.ceiling}"
    processes = "?" if view.harness_processes is None else str(view.harness_processes)
    role = view.role if view.role == "taker" else f"not a taker: {view.why}"
    if view.role == "giver":
        role = f"giver; {role}"
    return (
        f"{view.machine:<{width}}  {view.state:<7}  load/core {load} ({was})  "
        f"memory {view.memory}  workers {workers}  harness processes {processes}  {role}"
    )


def text(plan: Plan, path: Path) -> str:
    """One line per machine, an empty line, and the plan or `NOTHING_MOVES: <cause>`."""
    width = max((len(view.machine) for view in plan.machines), default=0)
    lines = [_machine_line(view, width) for view in plan.machines]
    lines.append("")
    kept = f"changed: nothing (readings kept in {path})"
    move = plan.move
    if move is None:
        lines.append(f"{NOTHING_MOVES}: {plan.cause}.")
        lines += [f"  {note}" for note in plan.notes]
        lines.append(f"plan: none        {kept}")
        return "\n".join(lines)
    was = {view.machine: view.was for view in plan.machines}
    state = {view.machine: view.state for view in plan.machines}
    giver = state[move.giver] + (" twice" if was[move.giver] == state[move.giver] else "")
    new = f"a new worker on {move.taker}"
    how = {
        "resubmit_queued": f"the queued task goes to {new}. Nothing was delivered yet.",
        "hand_over": f"hand-over brief, then {new}. The conversation is not carried.",
        "at_boundary": f"when its turn ends: hand-over brief, then {new}. "
        "The conversation is not carried.",
    }[move.kind]
    lines += [
        f"plan {plan.id}: 1 move",
        f"  {move.task or '-'}  {_WORK_WORDS[move.work]}  {move.harness}  "
        f"{move.giver}/{move.worker} -> {move.taker}",
        f"  why: {move.giver} {giver}; {move.taker} ok twice; gap {move.gap:.2f}; "
        f"projected {move.projected:.2f} < {TAKER_CEILING:.2f}",
        f"  how: {how}",
        f"{kept}.   apply: not built yet; this plan is a preview.",
    ]
    return "\n".join(lines)


async def dispatch(
    args: argparse.Namespace, *, runner: Runner | None = None
) -> tuple[Plan, Path, float]:
    """Measure, keep the readings and plan. Returns the plan, the readings file and the time."""
    machines = validate_machines([LOCAL, *args.machine])
    config_path = args.config.expanduser().absolute()
    path = readings_path(config_path)
    kept = load_readings(path)
    facts = load_facts(POLICY_PATH) if POLICY_PATH.is_file() else Facts()
    binary, session, send = "hc", None, []
    if config_path.is_file():
        config = Config.load(config_path)
        binary, session, send = config.hc_binary, config.hc_session, list(config.hc_send_machines)
    workers = managed_workers(config_path)
    readings = await read(
        machines,
        runner=runner,
        timeout=PROBE_TIMEOUT,
        clock=lambda: wall_clock(),
        hc_binary=binary,
        hc_session=session,
    )
    now = wall_clock()
    previous = {name: kept[name][-1] for name in readings if kept.get(name)}
    # No move store exists yet, so the move log is empty.
    plan = plan_rebalance(now, readings, previous, facts, workers, send, [])
    for name, reading in readings.items():
        kept[name] = [*kept.get(name, []), reading][-KEPT:]
    save_readings(path, kept)
    return plan, path, now


async def run_cli(args: argparse.Namespace, *, runner: Runner | None = None) -> NoReturn:
    """Print the preview and exit 0; a wrong argument exits 2 before anything is read."""
    try:
        validate_machines([LOCAL, *args.machine])
    except ValueError as error:
        args.rebalance_parser.error(str(error))
    plan, path, now = await dispatch(args, runner=runner)
    if args.json:
        record = plan.to_json()
        machines = record.pop("machines")
        print(
            json.dumps(
                {
                    "taken_at": now,
                    "coordinator": LOCAL,
                    "machines": machines,
                    "plan": record,
                    "changed": "nothing",
                    "readings_file": str(path),
                },
                indent=2,
            )
        )
    else:
        print(text(plan, path))
    raise SystemExit(0)
