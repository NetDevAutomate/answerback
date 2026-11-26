"""``answerback fleet serve``, ``show`` and ``configure`` (spec section 2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

from . import bounds, settings
from .agents import DEFAULT_LABEL, label_ok
from .loop import Clock, Settings, Writer
from .probes import Runner, default_runner
from .publish import NAME, LockHeld, Publisher, PublishRefused, default_folder
from .service import Call, remote
from .settings import FileSettings, SettingsError

EXIT_LOCKED = 3
READ_LIMIT = 1024 * 1024

# Replaceable in tests: the real runner of children and the real service call.
make_runner = default_runner
make_call = remote


def _machine(value: str) -> str:
    if not bounds.machine_name_ok(value):
        raise argparse.ArgumentTypeError(
            "machine names match ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ and are not 'local'"
        )
    return value


def _ceiling(value: str) -> tuple[str, int]:
    name, separator, number = value.partition("=")
    if not separator or not bounds.machine_name_ok(name) or not number.isdigit():
        raise argparse.ArgumentTypeError("--ceiling takes NAME=N with N from 1 to 99")
    n = int(number)
    if not 1 <= n <= 99:
        raise argparse.ArgumentTypeError("--ceiling takes NAME=N with N from 1 to 99")
    return name, n


def _lane_label(value: str) -> str:
    if not label_ok(value):
        raise argparse.ArgumentTypeError(
            "--lane-label takes K=V: 1 to 64 letters, digits, '.', '_' or '-' each, "
            "the key starting with a letter or a digit"
        )
    return value


def _settings_options(parser: argparse.ArgumentParser) -> None:
    """The options ``serve`` takes on its command line and ``configure`` writes to the file."""
    parser.add_argument("--dir", type=Path, help="Published folder (default: fleet/ by config)")
    parser.add_argument(
        "--machine",
        action="append",
        type=_machine,
        default=[],
        metavar="NAME",
        help="A worker to measure; repeat (default: every machine of hc ls --json)",
    )
    parser.add_argument(
        "--ceiling",
        action="append",
        type=_ceiling,
        default=[],
        metavar="NAME=N",
        help="A worker's agent ceiling, 1-99; repeat",
    )
    parser.add_argument(
        "--lane-label",
        type=_lane_label,
        default=None,
        metavar="K=V",
        help=f"The label that marks an unattended lane (default: {DEFAULT_LABEL})",
    )


def add_parser(commands: argparse._SubParsersAction) -> None:
    """Register ``fleet`` with its three actions."""
    parser = commands.add_parser("fleet", help="Publish the fleet document for the Herdr App")
    actions = parser.add_subparsers(dest="fleet_action", required=True)
    serve = actions.add_parser("serve", help="Measure the fleet and publish fleet.json")
    _settings_options(serve)
    serve.add_argument(
        "--settings",
        type=Path,
        metavar="FILE",
        help="Read the settings fleet configure wrote; excludes the options above",
    )
    serve.add_argument("--once", action="store_true", help="Run one cycle, publish, exit")
    serve.set_defaults(fleet_parser=serve)
    show = actions.add_parser("show", help="Print the published fleet document")
    show.add_argument("--dir", type=Path, help="Published folder (default: fleet/ by config)")
    show.add_argument("--json", action="store_true", help="Print the document")
    configure = actions.add_parser(
        "configure", help="Write the settings file that fleet serve --settings reads"
    )
    _settings_options(configure)
    configure.add_argument(
        "--settings",
        type=Path,
        metavar="FILE",
        help=f"Where to write (default: {settings.FILE} beside the bridge configuration file)",
    )
    configure.set_defaults(fleet_parser=configure)


def _given(args: argparse.Namespace) -> list[str]:
    """The settings options the command line carries, as the user wrote them."""
    given = []
    if args.dir is not None:
        given.append("--dir")
    if args.machine:
        given.append("--machine")
    if args.ceiling:
        given.append("--ceiling")
    if args.lane_label is not None:
        given.append("--lane-label")
    return given


def _wrong(args: argparse.Namespace, text: str) -> NoReturn:
    """End the command as argparse does for a wrong option: usage, the text, exit code 2."""
    args.fleet_parser.error(text)
    raise SystemExit(2)  # argparse has exited above; this line is for the type checker


def _from_options(args: argparse.Namespace) -> FileSettings:
    """The settings of a command line without ``--settings``; a wrong one ends the command."""
    try:
        return settings.check(
            list(args.machine),
            dict(args.ceiling),
            None if args.dir is None else args.dir.absolute(),
            DEFAULT_LABEL if args.lane_label is None else args.lane_label,
        )
    except SettingsError as error:
        _wrong(args, str(error))


def _settings(args: argparse.Namespace) -> FileSettings:
    """What the writer runs with: the file of ``--settings``, else the options."""
    if args.settings is None:
        return _from_options(args)
    given = _given(args)
    if given:
        _wrong(args, f"--settings cannot be combined with {', '.join(given)}")
    try:
        return settings.read(args.settings)
    except SettingsError as error:
        _wrong(args, str(error))


def _folder(chosen: FileSettings, args: argparse.Namespace) -> Path:
    """The folder the writer publishes to under ``chosen``."""
    return chosen.dir or default_folder(args.config)


def _overlap(path: Path, folder: Path) -> str | None:
    """One line when the settings file ``path`` is at or below the published ``folder``.

    The publisher refuses a folder that holds a file it does not own, so ``serve`` would
    refuse to start; the command says so before anything is written or published.
    """
    if not settings.inside(path, folder):
        return None
    return f"fleet settings file {path} is inside the published folder {folder}; choose another"


def configure(args: argparse.Namespace) -> int:
    """Write the settings file and say where it is."""
    chosen = _from_options(args)
    path = (args.settings or settings.default_path(args.config)).absolute()
    overlap = _overlap(path, _folder(chosen, args))
    if overlap is not None:
        _say(overlap)
        return 2
    try:
        settings.write(path, chosen)
    except OSError as error:
        _say(f"fleet settings not written to {path}: {error.strerror or error}")
        return 1
    print(f"fleet settings written to {path}")
    return 0


def _hc(config_path: Path) -> tuple[str, str | None]:
    from ..config import Config

    path = config_path.expanduser().absolute()
    if path.is_file():
        config = Config.load(path)
        return config.hc_binary, config.hc_session
    return "hc", None


def _say(text: str) -> None:
    print(text, file=sys.stderr)


async def serve(args: argparse.Namespace) -> int:
    """Run the writer; the exit code is the result."""
    chosen = _settings(args)
    folder = _folder(chosen, args)
    if args.settings is not None:
        overlap = _overlap(args.settings.absolute(), folder)
        if overlap is not None:
            _say(overlap)
            return 2
    binary, session = _hc(args.config)
    runner: Runner
    runner, binary = make_runner(binary, session)
    call: Call = make_call(args.config)
    publisher = Publisher(folder)
    try:
        publisher.open()
    except LockHeld as error:
        _say(str(error))
        return EXIT_LOCKED
    except (PublishRefused, OSError) as error:
        _say(f"fleet writer refused to start: {error}")
        return 1
    loop = asyncio.get_running_loop()
    writer = Writer(
        Settings(
            machines=list(chosen.machines),
            ceilings=dict(chosen.ceilings),
            hc_binary=binary,
            hc_session=session,
            lane_label=chosen.lane_label,
        ),
        runner=runner,
        call=call,
        sink=publisher,
        clock=Clock(time.time, time.monotonic, asyncio.sleep),
    )
    stop = asyncio.Event()
    # What each signal had before. asyncio's removal puts its own default back, not this.
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    for signum in before:
        loop.add_signal_handler(signum, stop.set)
    try:
        if args.once:
            # A signal ends the cycle's children, keeps the last file, and exits 0.
            published = await writer.once_until(stop)
            return 0 if published is None or published else 1
        await writer.serve(stop)
        return 0
    finally:
        for signum, handler in before.items():
            loop.remove_signal_handler(signum)
            if handler is not None:  # None: not set from Python, and Python cannot put it back
                signal.signal(signum, handler)
        publisher.close()


def _line(machine: dict[str, Any]) -> str:
    raw = machine.get("reading")
    reading: dict[str, Any] = raw if isinstance(raw, dict) else {}
    name = machine.get("name") or "coordinator"
    parts = [str(name), str(reading.get("state"))]
    if reading.get("was"):
        parts.append(f"(was {reading['was']})")
    pressure = reading.get("pressure")
    if isinstance(pressure, dict):
        parts.append(f"{pressure.get('signal')} {pressure.get('text')}")
    load = reading.get("load_per_core")
    if isinstance(load, int | float):
        parts.append(f"load {load:.2f} per core")
    link_raw = machine.get("link")
    link = link_raw.get("state") if isinstance(link_raw, dict) else None
    if link not in ("self", "connected"):
        parts.append(f"link {link}")
    if reading.get("error"):
        parts.append(f"· {reading['error']}")
    return bounds.clean(" ".join(parts), bounds.TEXT) or str(name)


def show(args: argparse.Namespace) -> int:
    """Print the age of the document and one line for each machine, or the document."""
    path = (args.dir or default_folder(args.config)) / NAME
    try:
        with path.open("rb") as handle:
            data = handle.read(READ_LIMIT + 1)
        doc = json.loads(data) if len(data) <= READ_LIMIT else None
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("machines"), list):
        _say(f"no fleet document at {path}")
        return 1
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    written = doc.get("written_at")
    age = time.time() - written if isinstance(written, int | float) else math.nan
    writer = doc.get("writer")
    mode = writer.get("mode") if isinstance(writer, dict) else None
    print(f"fleet.json {age:.0f} s old · mode {mode} · {path}")
    for machine in doc["machines"]:
        if isinstance(machine, dict):
            print(_line(machine))
    return 0


async def run_cli(args: argparse.Namespace) -> NoReturn:
    """Dispatch ``fleet serve``, ``show`` or ``configure`` and exit with its code."""
    if args.fleet_action == "serve":
        code = await serve(args)
    elif args.fleet_action == "configure":
        code = configure(args)
    else:
        code = show(args)
    raise SystemExit(code)
