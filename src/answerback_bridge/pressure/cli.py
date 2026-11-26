"""`answerback pressure`: read provider rate pressure, or clear one account's hold."""

import argparse
from datetime import datetime
from typing import Any

from ..events.model import bounded


def add_parser(commands: Any) -> None:
    pressure = commands.add_parser(
        "pressure",
        help="Show provider rate pressure, or clear one account's hold",
        description=(
            "Per provider account: the pressure level, until when dispatch waits, and the "
            "last signal. --clear ends a hold now (an operator override)."
        ),
    )
    pressure.add_argument("--json", action="store_true", help="Print the bridge's JSON answer")
    pressure.add_argument("--clear", metavar="PROVIDER", help="Clear this provider (kiro, bedrock)")
    pressure.add_argument(
        "--account",
        metavar="HINT",
        default="default",
        help="With --clear: the account hint (default: default)",
    )


async def dispatch(args: argparse.Namespace, bridge: Any) -> dict[str, Any]:
    if args.clear is None:
        if args.account != "default":
            raise ValueError("--account requires --clear")
        return await bridge.call("pressure_status", {})
    return await bridge.call(
        "pressure_clear", {"provider": args.clear, "account_hint": args.account}
    )


def _clock(seconds: Any) -> str:
    if type(seconds) not in (int, float):
        return "—"
    try:
        return datetime.fromtimestamp(seconds).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "—"


def render(result: dict[str, Any]) -> str:
    rows = sorted((result.get("pressure") or {}).items())
    lines = ["account                   level  held  until     last signal"]
    for key, row in rows:
        lines.append(
            f"{bounded(key, 24):<25} {row.get('level', 0):>5}  "
            f"{'yes' if row.get('held') else 'no':<4}  {_clock(row.get('until')):<8}  "
            f"{_clock(row.get('last_signal'))}"
        )
    settings = result.get("settings") or {}
    if settings:
        lines.append(
            f"enabled {settings.get('enabled')}; base {settings.get('base_seconds')} s, "
            f"cap {settings.get('cap_seconds')} s, clean window {settings.get('clean_seconds')} s "
            "(events.json pressure)"
        )
    if result.get("error"):
        lines.append(f"store error: {bounded(result['error'], 64)}")
    return "\n".join(lines)
