"""Which live terminals count as active agents (spec section 5). Pure.

The rule is the one the shared capacity probe of the lanes uses, plus the labelled lanes:
an unattended lane is a plain shell that reports no status, so only its label shows it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_LABEL = "ab.lane=1"
NOT_ACTIVE = frozenset({"done", "idle", "blocked"})
SHELLS = frozenset({"zsh", "bash", "sh", "fish", ""})
# Key and value: 1 to 64 letters, digits, ".", "_" and "-". The key starts with a letter or
# a digit, so the argument can never read as an option of ``hc``.
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}=[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Terminal:
    """One live terminal of a listing: its machine (None: the coordinator), status, program."""

    machine: str | None
    status: str | None
    agent: str | None


def label_ok(value: object) -> bool:
    """True for a lane label ``K=V`` that may be passed to ``hc ls --label``."""
    return isinstance(value, str) and _LABEL.fullmatch(value) is not None


def _works(terminal: Terminal) -> bool:
    if terminal.status in NOT_ACTIVE:
        return False
    return terminal.status is not None or (terminal.agent or "") not in SHELLS


def count(
    first: Mapping[str, Terminal], labelled: Mapping[str, Terminal] | None
) -> tuple[dict[str | None, int], dict[str | None, int] | None]:
    """Active agents and labelled lanes for each machine; each address counts once.

    ``labelled`` None means the labelled listing failed: lanes are then not known.
    """
    active: dict[str | None, int] = {}
    lanes: dict[str | None, int] | None = None if labelled is None else {}
    for address in {*first, *(labelled or {})}:
        seen = first.get(address)
        tagged = None if labelled is None else labelled.get(address)
        either = [t for t in (seen, tagged) if t is not None]
        machine = either[0].machine
        status = next((t.status for t in either if t.status is not None), None)
        agent = next((t.agent for t in either if t.agent), None)
        lane = tagged is not None and status is None
        if lane or _works(Terminal(machine, status, agent)):
            active[machine] = active.get(machine, 0) + 1
        if lane and lanes is not None:
            lanes[machine] = lanes.get(machine, 0) + 1
    return active, lanes
