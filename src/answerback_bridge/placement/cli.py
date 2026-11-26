"""Read-only capacity command, independent of a running bridge."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from typing import Any, NoReturn

from ..config import Config
from .assess import advise, assess, rank
from .probe import LOCAL, Runner, clean_process_name, read, validate_machines


def _machine(value: str) -> str:
    try:
        return validate_machines([value])[0]
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise argparse.ArgumentTypeError("--timeout must be between 1 and 30 seconds")
    return seconds


def add_parser(commands: argparse._SubParsersAction) -> None:
    """Register capacity next to the other feature commands."""
    parser = commands.add_parser("capacity", help="Measure machine capacity and suggest placement")
    parser.add_argument(
        "--machine",
        action="append",
        type=_machine,
        default=[],
        metavar="NAME",
        help="Also measure this machine; repeat for more (maximum 16 including local)",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON snapshot")
    parser.add_argument(
        "--require", choices=("ok", "busy"), help="Fail if required machines exceed this state"
    )
    parser.add_argument(
        "--advise", action="store_true", help="Show advice and targets for heavy work"
    )
    parser.add_argument(
        "--timeout",
        type=_timeout,
        default=10.0,
        metavar="SECONDS",
        help="Per-machine probe timeout, 1-30 seconds (default: 10)",
    )
    parser.set_defaults(capacity_parser=parser)


async def dispatch(
    args: argparse.Namespace,
    *,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Collect a snapshot, assessment and advice with an injectable process runner."""
    machines = validate_machines([LOCAL, *args.machine])
    binary, session = "hc", None
    if any(machine != LOCAL for machine in machines):
        config_path = args.config.expanduser().absolute()
        if config_path.is_file():
            config = Config.load(config_path)
            binary, session = config.hc_binary, config.hc_session
    taken_at = clock()
    readings = await read(
        machines,
        runner=runner,
        timeout=args.timeout,
        clock=clock,
        hc_binary=binary,
        hc_session=session,
    )
    now = clock()
    assessments = [assess(reading, now=now) for reading in readings.values()]
    return {
        "taken_at": taken_at,
        "coordinator": LOCAL,
        "machines": [
            {**row.to_json(), "reading": readings[row.machine].to_json()} for row in assessments
        ],
        "ranked_for_heavy_work": rank(assessments),
        "advice": [item.to_json() for item in advise(assessments)],
    }


def _text(snapshot: dict[str, Any], *, show_advice: bool) -> str:
    lines = []
    memory_signals = {"pressure", "compressor", "swap", "available", "stall", "largest_process"}
    for row in snapshot["machines"]:
        reading = row["reading"]
        load = "?" if row["load_per_core"] is None else f"{row['load_per_core']:.3f}"
        loads = "/".join(
            "?" if reading[key] is None else f"{reading[key]:.2f}"
            for key in ("load1", "load5", "load15")
        )
        memory = (
            "; ".join(
                signal["text"] for signal in row["signals"] if signal["name"] in memory_signals
            )
            or "memory unknown"
        )
        name = clean_process_name(reading["largest_process_name"] or "?")
        size = reading["largest_process_kb"]
        line = (
            f"{row['machine']} {row['state']} load/core={load} "
            f"loads={loads} cores={reading['cores'] if reading['cores'] is not None else '?'}; "
            f"{memory}; largest={name} ({size if size is not None else '?'} kB); "
            f"processes={reading['process_count'] if reading['process_count'] is not None else '?'}"
        )
        if row["reasons"]:
            line += "; reasons: " + "; ".join(row["reasons"])
        lines.append(line)
    if show_advice:
        for item in snapshot["advice"]:
            lines.append(f"{item['machine']} level {item['level']}: {item['text']}")
        targets = ", ".join(snapshot["ranked_for_heavy_work"]) or "none; queue heavy work"
        lines.append(f"Heavy-work targets: {targets}")
    return "\n".join(lines)


async def run_cli(args: argparse.Namespace, *, runner: Runner | None = None) -> NoReturn:
    """Render once and exit through the shared mapper, leaving other commands intact."""
    from ..cli import exit_code_for

    try:
        validate_machines([LOCAL, *args.machine])
    except ValueError as error:
        args.capacity_parser.error(str(error))
    snapshot = await dispatch(args, runner=runner)
    print(json.dumps(snapshot, indent=2) if args.json else _text(snapshot, show_advice=args.advise))
    required = set(args.machine or [LOCAL])
    allowed = {"ok", "busy"} if args.require == "busy" else {"ok"}
    success = args.require is None or all(
        row["state"] in allowed for row in snapshot["machines"] if row["machine"] in required
    )
    raise SystemExit(exit_code_for(args, {"success": success}))
