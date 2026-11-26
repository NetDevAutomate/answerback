"""CLI surface owned by the Rex provider."""

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...cli_kit import binary_argument, bounded_file
from ...plugin import CliRunner

if TYPE_CHECKING:
    from ...transport import Operations


def rex(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    configure = commands.add_parser(
        "rex-configure", help="Probe and configure a local Rex endpoint"
    )
    configure.add_argument("--binary", default="rex")
    configure.add_argument("--server")
    read = commands.add_parser("read-rex", help="Read an explicit Rex terminal snapshot")
    read.add_argument("--session", required=True)
    read.add_argument("--block", required=True)
    read.add_argument("--max-bytes", type=int, default=16384)
    enroll = commands.add_parser("rex-enroll", help="Enroll an explicit cooperative conversation")
    enroll.add_argument("name")
    enroll.add_argument("--here", action="store_true", help="Use caller REX_SESSION and REX_BLOCK")
    enroll.add_argument("--session")
    enroll.add_argument("--block")
    enroll.add_argument("--request-id", required=True)
    receive = commands.add_parser("rex-receive", help="Fetch queued messages before acknowledging")
    receive.add_argument("peer_id")
    receive.add_argument("--wait", type=int, default=0)
    ack = commands.add_parser("rex-ack", help="Acknowledge one exact received task")
    ack.add_argument("peer_id")
    ack.add_argument("task_id")
    complete = commands.add_parser("rex-complete", help="Record a correlated Rex result")
    complete.add_argument("peer_id")
    complete.add_argument("task_id")
    result_body = complete.add_mutually_exclusive_group(required=True)
    result_body.add_argument("--result")
    result_body.add_argument("--result-file", type=Path)
    retire = commands.add_parser("rex-retire", help="Retire enrollment before a new conversation")
    retire.add_argument("peer_id")

    async def run_configure(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "rex_configure", {"binary": binary_argument(args.binary), "server": args.server}
        )

    async def run_read(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "rex_read",
            {"session_id": args.session, "block_id": args.block, "max_bytes": args.max_bytes},
        )

    async def run_enroll(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        from .client import _full_id

        if args.here and (args.session is not None or args.block is not None):
            raise ValueError("--here conflicts with explicit --session or --block")
        session_id = os.environ.get("REX_SESSION") if args.here else args.session
        block_id = os.environ.get("REX_BLOCK") if args.here else args.block
        return await bridge.call(
            "rex_enroll",
            {
                "name": args.name,
                "session_id": _full_id(session_id, "session"),
                "block_id": _full_id(block_id, "block"),
                "request_id": args.request_id,
            },
        )

    async def run_receive(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "rex_receive", {"peer_id": args.peer_id, "wait_seconds": args.wait}
        )

    async def run_ack(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call("rex_ack", {"peer_id": args.peer_id, "task_id": args.task_id})

    async def run_complete(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        result = bounded_file(args.result_file, 200_000) if args.result_file else args.result
        return await bridge.call(
            "rex_complete", {"peer_id": args.peer_id, "task_id": args.task_id, "result": result}
        )

    async def run_retire(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call("rex_retire", {"peer_id": args.peer_id})

    run(configure, run_configure)
    run(read, run_read)
    run(enroll, run_enroll)
    run(receive, run_receive)
    run(ack, run_ack)
    run(complete, run_complete)
    run(retire, run_retire)
