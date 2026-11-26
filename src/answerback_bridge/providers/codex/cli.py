"""CLI surface owned by the Codex Desktop provider."""

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...cli_kit import bounded_file
from ...plugin import CliRunner

if TYPE_CHECKING:
    from ...transport import Operations


def _message_body(args: argparse.Namespace) -> str:
    return bounded_file(args.message_file, 100_000) if args.message_file else args.message


def codex(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    desktop = commands.add_parser("desktop-sessions", help="Browse indexed Desktop/IDE chats")
    desktop.add_argument("query", nargs="?", default="")
    desktop.add_argument("--cursor")
    desktop.add_argument("--limit", type=int, default=50)
    send = commands.add_parser("send-desktop", help="Submit work to desktop")
    send.add_argument("target")
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--message-file", type=Path)
    send.add_argument("--request-id", required=True)
    read = commands.add_parser("read-desktop")
    read.add_argument("alias")

    async def run_sessions(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "desktop_sessions",
            {
                "query": args.query,
                "cursor": args.cursor,
                "limit": args.limit,
            },
        )

    async def run_send(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "desktop_submit",
            {"alias": args.target, "message": _message_body(args), "request_id": args.request_id},
        )

    async def run_read(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call("desktop_read", {"alias": args.alias})

    run(desktop, run_sessions)
    run(send, run_send)
    run(read, run_read)
