"""CLI-only maintenance entry points. No service or MCP dispatch is required."""

import argparse
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .maintenance import Maintenance, MaintenanceError, RetentionSelection


def add_parser(commands: argparse._SubParsersAction) -> None:
    maintenance = commands.add_parser(
        "maintenance", help="Preview or explicitly maintain local SQLite history"
    )
    actions = maintenance.add_subparsers(dest="maintenance_action")
    for name in ("stats", "plan"):
        action = actions.add_parser(name, help="Read-only " + name)
        action.add_argument("--older-than-days", type=int, default=30)
        action.add_argument("--provider")
        action.add_argument("--project")
        action.add_argument("--max-actions", type=int, default=500)
        action.add_argument("--max-roots", type=int, default=500)
        if name == "plan":
            action.add_argument("--output", type=Path, required=True)
            action.add_argument("--cursor", type=Path, help="Continue from a previous private plan")
    apply = actions.add_parser("apply", help="Apply exactly a reviewed private plan")
    apply.add_argument("--plan", type=Path, required=True)
    compact = actions.add_parser(
        "compact", help="VACUUM one known store with owned services stopped"
    )
    compact.add_argument("--store", choices=("tasks", "bus", "clients"), required=True)
    maintenance.set_defaults(maintenance_help=maintenance.format_help())


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.maintenance_action is None:
        return {"status": "help", "help": args.maintenance_help}
    maintenance = Maintenance(args.config.expanduser().absolute())
    try:
        if args.maintenance_action == "apply":
            return maintenance.apply(maintenance.read_plan(args.plan))
        if args.maintenance_action == "compact":
            return maintenance.compact(args.store)
        selection = RetentionSelection(
            args.older_than_days, args.provider, args.project, args.max_actions, args.max_roots
        )
        if args.maintenance_action == "stats":
            return maintenance.stats(selection)
        cursor = None
        if args.cursor:
            previous = maintenance.read_plan(args.cursor)
            if previous["selection"] != asdict(selection) or not previous["truncated"]:
                raise ValueError(
                    "Continuation requires the same filters/limits and a truncated plan"
                )
            cursor = previous["continuation"]
        plan = maintenance.plan(selection, cursor=cursor)
        maintenance.write_plan(plan, args.output)
        return {
            "status": "planned",
            "output": str(args.output.absolute()),
            "digest": plan["digest"],
            "actions": len(plan["actions"]),
            "logical_bytes": sum(a["bytes"] for a in plan["actions"]),
            "protected": plan["protected"],
            "truncated": plan["truncated"],
            "continuation": plan["continuation"],
        }
    except sqlite3.Error as error:
        raise MaintenanceError(
            "storage_error", "SQLite refused maintenance; transaction rolled back"
        ) from error
