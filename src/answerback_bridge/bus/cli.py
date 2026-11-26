"""Neutral CLI for durable commands; existing provider registration remains compatible."""

import argparse
import asyncio
import json
import math
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..cli_kit import bounded_file
from ..config import Config, default_config_path
from ..lifecycle import start
from ..process import BridgeError
from ..transport import RemoteBridge
from .api import BusAPI
from .config import LAUNCHER_STATUS_ERROR, BusConfig, launcher_shim
from .extra import broker_class, jetstream_bus_class


class BusParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.add_argument("--config", type=Path, default=argparse.SUPPRESS)


def parser(*, nested: bool = False) -> argparse.ArgumentParser:
    result = BusParser(prog="agent-bus")
    if not nested:
        result.set_defaults(config=default_config_path())
    commands = result.add_subparsers(dest="operation", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--server", default=shutil.which("nats-server"))
    setup.add_argument("--port", type=int, default=4222)
    for name in ("start", "stop", "status"):
        commands.add_parser(name)
    submit = commands.add_parser("submit")
    submit.add_argument("peer_id")
    submit.add_argument("--project", required=True)
    body = submit.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--message-file", type=Path)
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--ttl", type=int, default=3600)
    submit.add_argument(
        "--offline", action="store_true", help="Store locally without a publish attempt"
    )
    for name in ("result", "cancel"):
        commands.add_parser(name).add_argument("command_id")
    events = commands.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)
    monitor = commands.add_parser(
        "monitor", help="Follow lifecycle events without consuming commands"
    )
    monitor.add_argument("--after", type=int)
    monitor.add_argument("--count", type=int)
    monitor.add_argument("--timeout", type=float)
    monitor.add_argument("--interval", type=float)
    monitor.add_argument("--json", action="store_true", help="Write one event JSON object per line")
    monitor.add_argument("--web", action="store_true", help="Open the local live dashboard")
    monitor.add_argument(
        "--board",
        action="store_true",
        help="Show runs, peers and decisions as tables, from the AG-UI event stream",
    )
    monitor.add_argument("--once", action="store_true", help="With --board: print once and exit")
    monitor.add_argument(
        "--all",
        action="store_true",
        help="With --board: every retained run, not only the last 24 hours plus unfinished "
        "and failed ones",
    )
    monitor.add_argument(
        "--rows", type=int, help="With --board: rows per table (default 12); 0 shows every row"
    )
    monitor.add_argument(
        "--page",
        type=int,
        help="With --board --all: retained receipt page (100 runs, starting at 1)",
    )
    monitor.add_argument(
        "--port", type=int, default=0, help="Dashboard port; 0 chooses an unused port"
    )
    monitor.add_argument(
        "--no-open", action="store_true", help="Print the dashboard URL without opening"
    )
    return result


async def _monitor_events(args: argparse.Namespace) -> None:
    from .monitor import BusNotConfigured, Monitor

    def banner() -> None:
        print("Following local lifecycle events. Ctrl+C stops monitoring.", file=sys.stderr)

    try:
        # The banner comes from the read that decides: none without a configured bus.
        async for event in Monitor(args.config).follow(
            after=args.after,
            count=args.count,
            interval=args.interval if args.interval is not None else 1,
            on_configured=None if args.json else banner,
        ):
            if args.json:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            else:
                stamp = datetime.fromtimestamp(event["created_at"], UTC).strftime("%H:%M:%S")
                print(
                    f"{stamp} UTC  #{event['seq']:<6} {event['type']:<24} "
                    f"{event['peer_id']:<22} command={event['command_id']}",
                    flush=True,
                )
    except BusNotConfigured as notice:
        # The bus is opt-in: no events to follow is not an error, so no "error" key and exit 0.
        text = str(notice)
        print(
            json.dumps({"configured": False, "message": text}) if args.json else text,
            file=sys.stderr,
        )


def monitor_board(args: argparse.Namespace, source: Any = None) -> None:
    """The board in a terminal: a fold over the bridge's event stream, redrawn on change."""
    from ..events.board import Board
    from ..events.cli import follow
    from ..events.sse import RemoteSource

    if args.rows is not None and not 0 <= args.rows <= 100_000:
        raise ValueError("--rows must be 0–100000")
    rows = 12 if args.rows is None else args.rows
    if args.page is not None and (not args.all or not 1 <= args.page <= 10_000_001):
        raise ValueError("--page requires --all and a positive page number")
    source = source or RemoteSource(args.config)
    board = Board()
    live = sys.stdout.isatty() and not args.once
    shown = ""

    def history_page() -> None:
        if (
            args.all
            and board.state
            and (board.state["bridge"].get("runsPageable") or args.page is not None)
        ):
            board.run_page = source.runs(((args.page or 1) - 1) * 100, 100)

    for ident, event in follow(
        source,
        after=None,
        count=args.count,
        timeout=0.5 if args.once and args.timeout is None else args.timeout,
        wait=args.interval if args.interval is not None else 2,
    ):
        board.apply(ident, event)
        if not live:
            continue
        if event["type"] in {"STATE_SNAPSHOT", "STATE_DELTA"}:
            history_page()
        frame = board.render(rows=rows, history=args.all)
        if frame != shown:
            shown = frame
            # Home and clear: the stream's text is already reduced to printable characters.
            print("\x1b[H\x1b[2J" + frame, flush=True)
    if not live:
        history_page()
        print(board.render(rows=rows, history=args.all), flush=True)


async def monitor_events(args: argparse.Namespace) -> None:
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        raise ValueError("Monitor timeout must be positive")
    try:
        async with asyncio.timeout(args.timeout):
            await _monitor_events(args)
    except TimeoutError:
        return


def configure(args: argparse.Namespace) -> dict[str, Any]:
    if not args.server:
        raise ValueError("Install nats-server before configuring the bus")
    config = BusConfig.create(args.config, server_binary=args.server, port=args.port)
    if not args.config.exists():
        Config(provider_selection=[]).save(args.config)
    api = BusAPI(args.config)
    api.store()
    return {"configured": True, **config.public()}


def body(args: argparse.Namespace) -> str:
    if args.message_file is None:
        return args.message
    if args.message_file.stat().st_size > 100_000:
        raise ValueError("Message file exceeds 100000 bytes")
    # st_size is 0 for a FIFO or a device: the read itself must be bounded too.
    return bounded_file(args.message_file, 100_000)


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    api = BusAPI(args.config)
    if args.operation == "configure":
        return await asyncio.to_thread(configure, args)
    if args.operation == "result":
        return api.result(args.command_id)
    if args.operation == "events":
        return api.events(args.after, args.limit)
    if args.operation == "cancel":
        return api.cancel(args.command_id)
    if args.operation == "submit":
        # Without the extra a publish attempt is impossible: refuse before anything is stored.
        bus_class = None if args.offline else jetstream_bus_class()
        message = await asyncio.to_thread(body, args)
        row = api.submit(args.peer_id, args.project, message, args.request_id, args.ttl)
        if bus_class is not None:
            try:
                async with bus_class(BusConfig.load(args.config), consume=False) as bus:
                    errors = await bus.flush_outbox(api.store())
                    if errors:
                        return {**row, "publish_errors": errors, "stored_locally": True}
            except Exception as error:
                return {**row, "publish_error": type(error).__name__, "stored_locally": True}
        return {**row, "stored_locally": True}
    if args.operation == "status":
        status = api.status()
        if not status["configured"]:
            return status
        config = BusConfig.load(args.config)
        broker = broker_class()(config)
        status["broker_reachable"] = await broker.reachable()
        if launcher_shim(config.server_binary):
            status["broker_error"] = LAUNCHER_STATUS_ERROR
        elif broker.identity_error is not None:
            status["broker_identity_error"] = broker.identity_error
        elif not status["broker_reachable"]:
            status["broker_error"] = await broker.failure_reason()
        try:
            runtime = await RemoteBridge(args.config).call("bus_status", {})
            status["coordinator"] = runtime["coordinator"]
        except BridgeError:
            status["coordinator"] = None
        return status
    config = BusConfig.load(args.config)
    if args.operation == "stop":
        return await broker_class()(config).stop()
    if args.operation == "start":
        remote = RemoteBridge(args.config)
        current = await remote.status()
        if current["running"] and current.get("version") != __version__:
            raise BridgeError("Restart the coordinator on the installed version after tasks settle")
        await broker_class()(config).start()
        await start(args.config)
        for _ in range(30):
            state = await remote.call("bus_status", {})
            if state["coordinator"]["connected"]:
                return state
            await asyncio.sleep(0.2)
        raise BridgeError("Coordinator has not connected; inspect agent-bus status")
    raise ValueError("Unknown operation")


def run_cli(args: argparse.Namespace) -> None:
    try:
        if args.operation == "monitor":
            if args.web:
                from .web import serve_dashboard

                if (
                    any(
                        value is not None
                        for value in (
                            args.after,
                            args.count,
                            args.timeout,
                            args.interval,
                        )
                    )
                    or args.json
                ):
                    raise ValueError(
                        "--after, --count, --timeout, --interval and --json are terminal options"
                    )
                if (
                    args.board
                    or args.once
                    or args.all
                    or args.rows is not None
                    or args.page is not None
                ):
                    raise ValueError(
                        "--board, --once, --all, --page and --rows are terminal options; "
                        "--web has the board and its own All control"
                    )
                serve_dashboard(args.config, port=args.port, open_browser=not args.no_open)
            else:
                if args.port or args.no_open:
                    raise ValueError("--port and --no-open require --web")
                if args.board:
                    if args.json or args.after is not None:
                        raise ValueError("--json and --after follow lifecycle events, not --board")
                    monitor_board(args)
                elif args.once or args.all or args.rows is not None or args.page is not None:
                    raise ValueError("--once, --all, --page and --rows require --board")
                else:
                    asyncio.run(monitor_events(args))
            return
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("delivery") in {"rejected", "expired", "uncertain"} or (
            result.get("task") or {}
        ).get("status") in {"failed", "uncertain", "cancelled"}:
            raise SystemExit(1)
    except (OSError, ValueError, BridgeError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def main() -> None:
    run_cli(parser().parse_args())


if __name__ == "__main__":
    main()
