"""CLI surface owned by the Kiro provider."""

import argparse
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...cli_kit import binary_argument
from ...plugin import CliRunner

if TYPE_CHECKING:
    from ...transport import Operations


def kiro(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    register = commands.add_parser(
        "kiro-register", help="Alias for workspace add; Crew is configured separately"
    )
    register.add_argument("alias")
    register.add_argument("--cwd", required=True)
    register.add_argument("--binary", default="kiro-cli")
    sessions = commands.add_parser("kiro-sessions", help="Search saved Kiro session metadata")
    sessions.add_argument("query", nargs="?", default="")
    sessions.add_argument("--cwd")
    sessions.add_argument("--cursor")

    async def run_register(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        cwd = await asyncio.to_thread(lambda: str(Path(args.cwd).expanduser().resolve()))
        return await bridge.call(
            "kiro_register",
            {
                "alias": args.alias,
                "cwd": cwd,
                "binary": binary_argument(args.binary),
            },
        )

    async def run_sessions(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "kiro_sessions", {"query": args.query, "cwd": args.cwd, "cursor": args.cursor}
        )

    run(register, run_register)
    run(sessions, run_sessions)
