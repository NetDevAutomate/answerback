"""Advice for new work and test suites, and the launches it waits for (spec section 6).

The advice reserves nothing. Two launchers that read the same document may pick the same
machine.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..placement.assess import Assessment, rank
from ..placement.probe import LOCAL
from . import bounds

LAUNCH_MEMORY_S = 60.0
_ADDRESS = re.compile(r"(?:([A-Za-z0-9][A-Za-z0-9._-]{0,62})/)?t[A-Za-z0-9]{1,63}")


@dataclass(frozen=True)
class Launch:
    """A terminal address seen for the first time."""

    machine: str | None
    address: str
    seen_at: float
    seen_mono: float


@dataclass(frozen=True)
class Candidate:
    """A worker as the advice sees it."""

    name: str
    link: str
    current: bool
    state: str | None
    suites: int | None
    assessment: Assessment | None


def machine_of(address: Any) -> str | None:
    """The machine of a terminal address: None for the coordinator, "" when invalid."""
    if not isinstance(address, str):
        return ""
    match = _ADDRESS.fullmatch(address)
    if match is None:
        return ""
    machine = match.group(1)
    if machine is None:
        return None
    return machine if bounds.machine_name_ok(machine) else ""


def launches(
    previous: frozenset[str] | None,
    current: frozenset[str],
    remembered: tuple[Launch, ...],
    *,
    now_wall: float,
    now_mono: float,
) -> tuple[Launch, ...]:
    """Add the addresses that are new since the last listing; forget launches after 60 s.

    The first listing (``previous`` None) is the baseline and counts no launch.
    """
    kept = tuple(item for item in remembered if now_mono - item.seen_mono <= LAUNCH_MEMORY_S)
    if previous is None:
        return kept
    new = []
    for address in sorted(current - previous):
        machine = machine_of(address)
        if machine != "":
            new.append(Launch(machine, address, now_wall, now_mono))
    return kept + tuple(new)


def launched_on(remembered: Iterable[Launch], now_mono: float) -> set[str]:
    """The workers with a launch in the last 60 s."""
    return {
        item.machine
        for item in remembered
        if item.machine is not None and now_mono - item.seen_mono <= LAUNCH_MEMORY_S
    }


def recent(remembered: Iterable[Launch], now_mono: float) -> list[dict[str, Any]]:
    """``recent_launches``: one entry per machine with its newest time and its count."""
    groups: dict[str | None, dict[str, Any]] = {}
    for item in remembered:
        if now_mono - item.seen_mono > LAUNCH_MEMORY_S:
            continue
        group = groups.setdefault(
            item.machine, {"machine": item.machine, "seen_at": item.seen_at, "count": 0}
        )
        group["count"] += 1
        group["seen_at"] = max(group["seen_at"], item.seen_at)
    return sorted(groups.values(), key=lambda g: (g["machine"] is not None, g["machine"] or ""))[
        : bounds.MACHINES
    ]


def advise(
    candidates: Iterable[Candidate],
    *,
    order: list[str],
    launched: set[str],
    paused: bool,
) -> dict[str, Any]:
    """``ranked_for_heavy_work``, ``new_work`` and ``new_suite`` of the capacity section."""
    workers = [c for c in candidates if c.name != LOCAL]

    def none(why: str) -> dict[str, Any]:
        return {
            "ranked_for_heavy_work": [],
            "new_work": {"machine": None, "why": why},
            "new_suite": {"machine": None, "why": why},
        }

    if paused:
        return none("worker probes are paused")
    current = [c for c in workers if c.link == "connected" and c.current]
    if not current:
        if any(c.link == "connected" for c in workers):
            return none("no current reading of a worker")
        return none("no ok worker")
    ok = [c for c in current if c.state == "ok" and c.assessment is not None]
    if not ok:
        return none("no ok worker")
    free = [c for c in ok if c.name not in launched]
    if not free:
        return none("a launch in the last minute on every ok worker")
    by_name = {c.name: c for c in free}
    ranked = rank(
        [c.assessment for c in free if c.assessment is not None],
        coordinator=LOCAL,
        heavy=True,
        order=order,
    )
    ranked = [name for name in ranked if name in by_name]
    if not ranked:
        return none("no ok worker")
    suite = next((name for name in ranked if by_name[name].suites == 0), None)
    return {
        "ranked_for_heavy_work": ranked,
        "new_work": {"machine": ranked[0], "why": "first ok worker of the ranking"},
        "new_suite": {
            "machine": suite,
            "why": "first ok worker of the ranking with no suite running"
            if suite is not None
            else "every ok machine runs a suite",
        },
    }
