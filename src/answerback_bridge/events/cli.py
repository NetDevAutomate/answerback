"""`answerback events`: follow the bridge's observe-only AG-UI 1.0 event stream."""

import argparse
import json
import math
import sys
import time
from collections.abc import Iterator
from typing import Any

from ..process import BridgeError
from .alerts import alert_event
from .hub import EVENT_ID
from .sse import INCOMPLETE, PAGE, EventServer, EventSource, RemoteSource, frame


def add_parser(commands: Any) -> None:
    events = commands.add_parser(
        "events",
        help="Follow the observe-only AG-UI 1.0 event stream",
        description=(
            "Print the bridge's AG-UI 1.0 events, one JSON object per line: a state snapshot, "
            "then every change. Read-only; work is dispatched with peer_submit or bus_submit."
        ),
    )
    events.add_argument("--after", help="Resume after this event ID (from --sse or an SSE client)")
    events.add_argument("--run", help="Only this run's lifecycle events, as a single-run stream")
    events.add_argument("--count", type=int, help="Stop after this many events")
    events.add_argument("--timeout", type=float, help="Stop after this many seconds")
    events.add_argument("--sse", action="store_true", help="Print SSE frames, including IDs")
    events.add_argument(
        "--alerts-only",
        action="store_true",
        help="Only new anomalies and interrupts, for an external alert consumer",
    )
    events.add_argument(
        "--serve", action="store_true", help="Serve the stream over HTTP on 127.0.0.1"
    )
    events.add_argument(
        "--port", type=int, default=0, help="With --serve: port; 0 chooses an unused port"
    )


def follow(
    source: EventSource,
    *,
    after: str | None = None,
    run: str | None = None,
    count: int | None = None,
    timeout: float | None = None,
    wait: float = 10,
    alerts_only: bool = False,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Pages from the source until `count` events or `timeout` seconds; forever otherwise."""
    if after is not None and EVENT_ID.fullmatch(after) is None:
        raise ValueError("--after must be an event ID from this stream")
    if alerts_only and run is not None:
        raise ValueError("--alerts-only cannot be combined with --run")
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("--count must be positive")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("--timeout must be positive")
    deadline = None if timeout is None else time.monotonic() + timeout
    emitted = 0
    page = source.read(after, PAGE, 0, run)
    while True:
        if page.get("incomplete"):
            raise ValueError(INCOMPLETE)
        for item in page["events"]:
            if alerts_only and not alert_event(item["event"]):
                continue
            yield item["id"], item["event"]
            emitted += 1
            if count is not None and emitted >= count:
                return
        remaining = wait if deadline is None else min(wait, deadline - time.monotonic())
        if remaining <= 0:
            return
        page = source.read(page["cursor"], PAGE, remaining, run)


def run_cli(args: argparse.Namespace) -> None:
    source = RemoteSource(args.config)
    try:
        if args.serve:
            if args.alerts_only:
                raise ValueError("--alerts-only is a terminal follower option")
            if any(value is not None for value in (args.after, args.run, args.count)) or args.sse:
                raise ValueError("--after, --run, --count and --sse are terminal options")
            source.read(None, 1, 0, None)  # Fail now, with a reason, if the bridge is not up.
            with EventServer(source, port=args.port) as server:
                if args.timeout is not None and (
                    not math.isfinite(args.timeout) or args.timeout <= 0
                ):
                    raise ValueError("--timeout must be positive")
                print(f"Answerback events (SSE, read-only): {server.url}", flush=True)
                print(f"Authorization: Bearer {server.token}", flush=True)
                print("Private local stream · Ctrl+C stops it.", flush=True)
                server.thread.join(args.timeout)
            return
        if args.port:
            raise ValueError("--port requires --serve")
        for ident, event in follow(
            source,
            after=args.after,
            run=args.run,
            count=args.count,
            timeout=args.timeout,
            alerts_only=args.alerts_only,
        ):
            if args.sse:
                sys.stdout.write(frame(ident, event).decode())
            else:
                sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except (OSError, ValueError, BridgeError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
