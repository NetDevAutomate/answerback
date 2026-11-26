"""CLI surfaces owned by the Herdr App/core and TUI providers."""

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...cli_kit import binary_argument, bounded_file
from ...plugin import CliRunner

if TYPE_CHECKING:
    from ...transport import Operations


def _message_body(args: argparse.Namespace) -> str:
    return bounded_file(args.message_file, 100_000) if args.message_file else args.message


def hc(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    core = commands.add_parser("hc-configure", help="Enable Herdr App/core discovery and delivery")
    core.add_argument("--binary", default="hc")
    core.add_argument("--session", help="Optional herdr-core session, not an App space")
    core_read = commands.add_parser("read-hc", help="Read an explicit core terminal")
    core_read.add_argument("target")
    core_read.add_argument("--lines", type=int, default=100)

    async def run_configure(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "hc_configure", {"binary": binary_argument(args.binary), "session": args.session}
        )

    async def run_read(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "hc_read", {"target": args.target.removeprefix("hc:"), "lines": args.lines}
        )

    run(core, run_configure)
    run(core_read, run_read)


def herdr(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    send = commands.add_parser("send-herdr", help="Submit work to herdr")
    send.add_argument("target")
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--message-file", type=Path)
    send.add_argument("--request-id", required=True)
    send.add_argument("--binding", help="Identity binding returned by find/peer_search")
    read = commands.add_parser("read-herdr")
    read.add_argument("target")
    read.add_argument("--lines", type=int, default=100)

    async def run_send(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        # Herdr delivery only exists behind discovery: peer_submit resolves the target
        # and the adapter rechecks that identity before anything is sent.
        return await bridge.call(
            "peer_submit",
            {
                "selector": args.target,
                "message": _message_body(args),
                "request_id": args.request_id,
                "provider": "herdr",
                "binding": args.binding,
            },
        )

    async def run_read(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call("herdr_read_agent", {"target": args.target, "lines": args.lines})

    run(send, run_send)
    run(read, run_read)
