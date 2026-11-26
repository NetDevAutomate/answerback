"""`answerback route`: classify a brief, pick a harness and peer, dispatch through peer_submit.

The order of work is the policy's: an idle peer of the right harness in the project is
reused; otherwise a bare worker is created and awaited; the brief is then submitted once
with the routing line on top. Nothing here retries a submission.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..addresses import address_machine, sendable_address, validate_machine_names
from .policy import Harness, Policy, classify
from .pool import ACTIVE_RECEIPTS, AtCapacity, Pool, Worker
from .ports import BridgeCalls

# Class owners that are people or lanes, not harnesses this tool may start or message.
_NOT_AUTHOR = "not-author"


def _stale_read_only(peers: list[dict[str, Any]], machines: Sequence[str]) -> list[str]:
    """Warnings for listed-machine peers the running bridge still reports read-only."""
    warnings: list[str] = []
    for peer in peers:
        target = peer.get("target")
        if peer.get("send_readiness_reason") != "remote_read_only" or not isinstance(target, str):
            continue
        try:
            machine = address_machine(target)
        except ValueError:
            continue
        if machine is not None and machine in machines:
            warnings.append(
                f"hc:{target}: the bridge reports remote_read_only although {machine} is in "
                "hc_send_machines; restart the bridge after changing hc_send_machines"
            )
    return warnings


def _local_only(idle: dict[str, list[dict[str, Any]]], task_class: str) -> list[str]:
    """Drop remote peers in place for a gated class; return why each was passed over.

    The gate check can only confirm that a local terminal is still live, so a gate slot
    held by a remote terminal that died would stay held until its receipt is settled.
    """
    passed: list[str] = []
    for name in list(idle):
        kept = [peer for peer in idle[name] if "/" not in peer["target"]]
        passed += [
            f"hc:{peer['target']}: remote terminal not used for gated class {task_class} "
            "(a gate slot held by a remote terminal cannot be released if it dies)"
            for peer in idle[name]
            if "/" in peer["target"]
        ]
        if kept:
            idle[name] = kept
        else:
            del idle[name]
    return passed


def refused(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "refused", "success": False, "reason": reason, **extra}


def candidates(
    policy: Policy, task_class: str, author: str | None
) -> tuple[list[list[Harness]], list[str]]:
    """Ranked groups of creatable harnesses for a class, plus why others were skipped.

    Each group is a set of equals; tie-breaks apply inside a group, rank between groups.
    """
    entry = policy.classes[task_class]
    skipped: list[str] = []
    if entry.primary == _NOT_AUTHOR:
        if author is None:
            return [], []
        group = [
            harness
            for harness in policy.harnesses.values()
            if harness.creatable and harness.name != author
        ]
        skipped += [f"{name}: not a harness this tool can dispatch to" for name in entry.fallback]
        return ([group] if group else []), skipped
    groups: list[list[Harness]] = []
    for name in (entry.primary, *entry.fallback):
        harness = policy.harnesses.get(name)
        if harness is None:
            skipped.append(f"{name}: not a harness (owner or lane); not dispatchable here")
        elif not harness.creatable:
            skipped.append(f"{name}: provider {harness.provider} is not created or messaged here")
        else:
            groups.append([harness])
    return groups, skipped


def _order(
    policy: Policy, group: list[Harness], idle: dict[str, list[dict[str, Any]]], author: str | None
) -> list[Harness]:
    def key(harness: Harness) -> tuple[Any, ...]:
        parts: list[Any] = []
        for rule in policy.tie_breaks:
            if rule == "existing_idle_peer_same_project":
                parts.append(0 if idle.get(harness.name) else 1)
            elif rule == "not_author_of_touched_files":
                parts.append(1 if harness.name == author else 0)
            elif rule == "cheaper_seat":
                parts.append(harness.seat_cost if harness.seat_cost is not None else float("inf"))
        return (*parts, harness.order)

    return sorted(group, key=key)


def _idle_peers(
    policy: Policy,
    peers: list[dict[str, Any]],
    busy: set[str],
    managed: dict[str, Worker],
    *,
    send_machines: Sequence[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Peers the bridge would deliver to now, per harness: managed, other local, remote.

    A remote `machine/tNN` peer is kept only when its machine is exactly in
    `send_machines` (the bridge's `hc_send_machines`); the bridge's own `send_ready`
    is still required on top of that.
    """
    machines = frozenset(validate_machine_names(send_machines))
    by_kind = {h.agent_kind: h.name for h in policy.harnesses.values() if h.creatable}
    found: dict[str, list[dict[str, Any]]] = {}
    for peer in peers:
        harness = by_kind.get(peer.get("agent_kind"))
        target = peer.get("target")
        if (
            harness is None
            or not isinstance(target, str)
            or ("/" in target and not sendable_address(target, machines))
            or peer.get("send_ready") is not True
            or peer.get("agent_identity_source") != "foreground_argv"
            or peer.get("project_known") is not True
            or peer.get("project") != policy.project
            or target in busy
        ):
            continue
        found.setdefault(harness, []).append(peer)
    for group in found.values():
        group.sort(
            key=lambda peer: (peer["target"] not in managed, "/" in peer["target"], peer["target"])
        )
    return found


async def route(
    pool: Pool,
    bridge: BridgeCalls,
    brief: str,
    *,
    task: str,
    lane: str,
    day: str,
    class_override: str | None = None,
    author: str | None = None,
    force_new: bool = False,
    dry_run: bool = False,
    send_machines: Sequence[str] = (),
) -> dict[str, Any]:
    """Route one brief. `day` is yyyymmdd; the worker name uses its last six digits.

    `send_machines` is the bridge config's `hc_send_machines`: idle terminals on those
    machines may be reused. The default, no machines, keeps routing local-only.
    """
    policy = pool.policy
    machines = validate_machine_names(send_machines)
    if not brief.strip():
        raise ValueError("The brief is empty")
    if author is not None and author not in policy.harnesses:
        raise ValueError(f"--author must be one of: {', '.join(policy.harnesses)}")
    found = classify(policy, brief, class_override)
    if found.name is None:
        return {
            "status": "needs_selection",
            "reason": "ambiguous class" if found.candidates else "no class keyword matched",
            "candidates": list(found.candidates) or list(policy.classes),
            "scores": dict(found.scores),
            "hint": "pass --class",
        }
    task_class = found.name
    classification = {
        "class": task_class,
        "source": found.source,
        "matched": list(found.matched),
        "scores": dict(found.scores),
    }
    groups, skipped = candidates(policy, task_class, author)
    owner = policy.classes[task_class].primary
    if owner == _NOT_AUTHOR and author is None:
        return refused(
            f"class {task_class} needs --author: it goes to a harness that did not write the work",
            classification=classification,
        )
    if not groups:
        return refused(
            f"class {task_class} is not dispatchable to a worker (primary: {owner})",
            classification=classification,
            skipped=skipped,
        )

    recent = (await bridge.call("task_list", {"query": "", "limit": 100})).get("tasks", [])
    active = [row for row in recent if row.get("status") in ACTIVE_RECEIPTS]
    same_lane = policy.request_ids(task, lane)
    for row in active:
        if same_lane.fullmatch(str(row.get("request_key"))):
            return refused(
                f"task {task} lane {lane} already has an active receipt ({row['status']})",
                receipt_id=row.get("id"),
                request_id=row.get("request_key"),
                classification=classification,
            )
    gated = pool.gated(task_class)
    if gated:
        holders = await pool.gate_holders(task, lane)
        if len(holders) >= (policy.max_concurrent_gates or 0):
            return refused(pool.gate_busy(holders), holders=holders, classification=classification)
    any_lane = policy.request_ids(task)
    warnings = [
        f"task {task} also has an active receipt {row.get('request_key')} on another lane"
        for row in active
        if any_lane.fullmatch(str(row.get("request_key")))
    ]

    search = await bridge.call("peer_search", {"query": "", "provider": "hc"})
    if search.get("complete") is False:
        warnings.append("peer scan incomplete; an idle peer may have been missed")
    workers = await pool.workers()
    managed = {worker.address: worker for worker in workers}
    # hc receipts are keyed hc:<session>:<address>; the address is what the scanner reports.
    busy = {
        str(row.get("target")).rsplit(":", 1)[-1]
        for row in active
        if str(row.get("target")).startswith("hc:")
    }
    idle = _idle_peers(policy, search.get("peers", []), busy, managed, send_machines=machines)
    if machines:
        warnings += _stale_read_only(search.get("peers", []), machines)

    choice: Harness | None = None
    peer: dict[str, Any] | None = None
    passed: list[str] = list(skipped)
    if gated:
        passed += _local_only(idle, task_class)
    primary = policy.classes[task_class].primary
    for group in groups:
        for harness in _order(policy, group, idle, author):
            if idle.get(harness.name) and not force_new:
                choice, peer = harness, idle[harness.name][0]
                break
            full = pool.capacity(workers, harness)
            if full is None:
                choice = harness
                break
            passed.append(f"{harness.name}: no idle peer and at {full}")
        if choice is not None:
            break
    if choice is None:
        return refused(
            "at_capacity: no idle peer and no room for a new worker; queue the brief",
            classification=classification,
            skipped=passed,
        )
    rank = "primary" if choice.name == primary else "fallback"
    if primary == _NOT_AUTHOR:
        rank = f"not the author ({author})"
    plan = {
        "classification": classification,
        "harness": choice.name,
        "model": choice.model,
        "action": "reuse" if peer is not None else "create",
        "peer": f"hc:{peer['target']}" if peer is not None else None,
        "skipped": passed,
        "warnings": warnings,
    }
    if dry_run:
        return {"status": "planned", **plan}

    created: dict[str, Any] | None = None
    if peer is None:
        try:
            created = await pool.create(task_class, choice.name, day[2:], gate_checked=True)
        except AtCapacity as error:
            return refused(str(error), **plan)
        if not created["visible"]:
            # A worker outside the project's App space ran unseen for tens of minutes on
            # 2026-11-17. Nothing is sent to it; its registry row stays, so `workers gc`
            # lists it and removes it after keep_idle_min.
            return {
                "status": "failed",
                "success": False,
                "stage": "placement",
                "reason": "the worker is not a tab in the project's App space; nothing was sent",
                "worker": created,
                **plan,
            }
        ready = await pool.wait_ready(created["address"], choice, created_ms=created["created_ms"])
        if ready["status"] != "ready":
            return {
                "status": "failed",
                "stage": "ready_wait",
                "worker": created,
                **{key: ready.get(key) for key in ("reason", "screen")},
                **plan,
                "ready": ready["status"],
            }
        peer = ready["peer"]
        reason = f"{rank} for {task_class}; no idle peer, created {created['name']}"
    else:
        reason = f"{rank} for {task_class}; reused idle peer hc:{peer['target']}"
    if passed:
        reason += "; passed over " + "; ".join(passed)
    address = str(peer["target"])
    line = f"Routed: {task_class} -> {choice.name}/{choice.model}, {reason}"
    request_id = policy.request_id(day, task, lane, address)
    held: int | None = None
    if gated:
        # Taken last, so a worker that never became ready holds nothing; and before the
        # submit, so two routes cannot both pass the check above.
        held, holders = await pool.reserve_gate(
            request_id, address=address, task=task, lane=lane, task_class=task_class
        )
        if held is None:
            return refused(pool.gate_busy(holders), holders=holders, worker=created, **plan)
    receipt = await bridge.call(
        "peer_submit",
        {
            "selector": f"hc:{address}",
            "message": f"{line}\n\n{brief}",
            "request_id": request_id,
            "provider": "hc",
            "binding": peer["binding"],
        },
    )
    if address in managed or created is not None:
        pool.registry.update(
            address,
            task=task,
            lane=lane,
            request_id=request_id,
            receipt_id=receipt.get("id"),
            dispatched_at=pool.now(),
        )
    if gated and receipt.get("status") == "provider_backoff":
        # Nothing was reserved or sent and no receipt will come for this request id: the
        # gate slot goes back now, not after the reservation's grace period.
        pool.release_gate(request_id)
        plan["gate"] = {"held": None, "cap": policy.max_concurrent_gates}
    elif gated:
        pool.gate_receipt(request_id, receipt.get("id"))
        plan["gate"] = {"held": held, "cap": policy.max_concurrent_gates}
    return {
        **plan,
        "status": receipt.get("status", "uncertain"),
        "action": "create" if created is not None else "reuse",
        "peer": f"hc:{address}",
        "routed": line,
        "request_id": request_id,
        "receipt_id": receipt.get("id"),
        "worker": created,
        "receipt": receipt,
    }
