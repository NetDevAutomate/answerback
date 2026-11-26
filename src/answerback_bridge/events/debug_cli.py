"""Local RPC controls for timed debug sampling."""

from typing import Any

from .debug import DEFAULT_SECONDS, duration


def add_parser(commands: Any) -> None:
    debug = commands.add_parser("debug", help="Temporarily trace a local task or peer")
    actions = debug.add_subparsers(dest="debug_action", required=True)
    on = actions.add_parser("on", help="Enable debug for at most 60 minutes")
    on.add_argument("scope", help="Receipt ID, local provider:peer, or all")
    on.add_argument("--for", dest="seconds", type=duration, default=DEFAULT_SECONDS)
    off = actions.add_parser("off", help="Disable one scope, or all scopes")
    off.add_argument("scope", nargs="?")
    actions.add_parser("status", help="Show active scopes, deadlines and debug drawer")
