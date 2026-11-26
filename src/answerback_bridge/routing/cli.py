"""`answerback route` and `answerback workers`: parser and wiring to the real ports."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..transport import RemoteBridge
from .policy import load_policy
from .pool import REGISTRY_NAME, AtCapacity, Pool, Registry
from .ports import HcTerminals, HerdrApp, QueueHandoffs, TasksLedger
from .route import refused, route

MAX_BRIEF_BYTES = 96 * 1024  # peer_submit takes 100 000 characters; the routing line needs room


def _common(action: argparse.ArgumentParser) -> None:
    action.add_argument(
        "--routing",
        type=Path,
        default=Path("status/routing.json"),
        help="Routing policy file (default: ./status/routing.json)",
    )
    action.add_argument("--herdr-app", default="herdr-app", help="Herdr App executable")


def add_parser(commands: argparse._SubParsersAction) -> None:
    routed = commands.add_parser(
        "route", help="Classify a brief, pick or create a worker, and submit it"
    )
    routed.add_argument("brief", type=Path, help="UTF-8 brief file")
    routed.add_argument("--task", required=True, help="Ledger task id, for example T-18")
    routed.add_argument("--lane", default="a", help="Lane within the task (default: a)")
    routed.add_argument("--class", dest="task_class", help="Skip keyword classification")
    routed.add_argument("--author", help="Harness that wrote the work under review")
    routed.add_argument("--new", action="store_true", help="Create a worker even if one is idle")
    routed.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing")
    _common(routed)

    workers = commands.add_parser("workers", help="Create, list and remove managed workers")
    actions = workers.add_subparsers(dest="workers_action", required=True)
    create = actions.add_parser("create", help="Start one bare worker and wait until it is ready")
    create.add_argument("--class", dest="task_class", required=True)
    create.add_argument("--harness", help="Default: the class's primary harness")
    _common(create)
    _common(actions.add_parser("list", help="Managed workers with their receipt and gc verdict"))
    collect = actions.add_parser("gc", help="Preview teardown; --apply removes finished workers")
    collect.add_argument("--apply", action="store_true", help="Tear down what the preview lists")
    _common(collect)


def _pool(args: argparse.Namespace) -> Pool:
    policy = load_policy(args.routing.expanduser())
    config_path = args.config.expanduser().absolute()
    config = Config.load(config_path)
    project = Path(policy.project)
    return Pool(
        policy,
        HcTerminals(config.hc_binary, config.hc_session),
        HerdrApp(args.herdr_app),
        RemoteBridge(config_path),
        Registry(config_path.parent / REGISTRY_NAME),
        QueueHandoffs(project),
        TasksLedger(project),
    )


def _brief(path: Path) -> str:
    with path.expanduser().open("rb") as handle:
        raw = handle.read(MAX_BRIEF_BYTES + 1)
    if len(raw) > MAX_BRIEF_BYTES:
        raise ValueError(f"Brief exceeds {MAX_BRIEF_BYTES} bytes")
    return raw.decode("utf-8")


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    pool = _pool(args)
    day = time.strftime("%Y%m%d")
    if args.command == "route":
        result = await route(
            pool,
            pool.bridge,
            _brief(args.brief),
            task=args.task,
            lane=args.lane,
            day=day,
            class_override=args.task_class,
            author=args.author,
            force_new=args.new,
            dry_run=args.dry_run,
            send_machines=Config.load(args.config.expanduser().absolute()).hc_send_machines,
        )
        if args.dry_run and result.get("status") == "planned":
            # What a real run would wait for (create) or be told (peer_submit's
            # provider_backoff); a dry run never waits.
            planned = pool.policy.harnesses.get(str(result.get("harness")))
            result["hold"] = None if planned is None else await pool.pressure_hold(planned)
        return result
    if args.workers_action == "list":
        return await pool.listing()
    if args.workers_action == "gc":
        return await pool.gc(apply=args.apply)
    policy = pool.policy
    if args.task_class not in policy.classes:
        raise ValueError(f"--class must be one of: {', '.join(policy.classes)}")
    name = args.harness or policy.classes[args.task_class].primary
    harness = policy.harnesses.get(name)
    if harness is None or not harness.creatable:
        return refused(f"{name} is not a harness this tool can create; pass --harness")
    try:
        created = await pool.create(args.task_class, name, day[2:])
    except AtCapacity as error:
        return refused(str(error))
    ready = await pool.wait_ready(created["address"], harness, created_ms=created["created_ms"])
    ready.pop("peer", None)
    return {
        **created,
        **ready,
        "status": "ready" if ready["status"] == "ready" else "failed",
        "ready": ready["status"],
    }
