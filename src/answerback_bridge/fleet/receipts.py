"""Health and receipts from the bridge service's answers (spec section 7). Pure."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ..tasks import STATES
from . import bounds

NEEDS = ("uncertain", "blocked")
CAUSES = ("peer_not_running", "recipient_not_ready")
RECEIPT_STATES = (*STATES, "unknown")
_CORE_PEER = re.compile(
    r"hc:(?:[A-Za-z0-9_-]{1,64}:)?(?:([A-Za-z0-9][A-Za-z0-9._-]{0,62})/)?(t[A-Za-z0-9]{1,63})"
)
_CODE = re.compile(r"[a-z][a-z_]{0,63}")


def _number(value: Any) -> float | None:
    """A finite float, or None; a very large integer is None and never raises."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _recipient(target: Any) -> dict[str, Any]:
    text = bounds.clean(target, bounds.TEXT)
    provider = None
    machine = terminal = None
    if isinstance(target, str):
        head, separator, _ = target.partition(":")
        provider = bounds.clean(head, bounds.NAME) if separator else None
        match = _CORE_PEER.fullmatch(target)
        if match is not None and (match.group(1) is None or bounds.machine_name_ok(match.group(1))):
            machine, terminal = match.group(1), match.group(2)
    return {"provider": provider, "machine": machine, "terminal": terminal, "text": text}


OBSERVED = "observed_before_and_after"


def _observer(item: dict[str, Any], recipient: dict[str, Any]) -> str | None:
    """Which Core read the terminal's identity before and after the send, or None.

    Only a receipt whose `metadata.delivery.identity_guard` says the identity was
    observed before and after the send, and was not invalidated since, names one:
    `core:local` or `core:<machine>` from its Core peer target, which must agree with
    the delivery's own `identity_observer` when that is recorded. A receipt that was
    never sent (or a `fleet_snapshot` row, which carries no metadata) gives None, so
    the page never says an identity was read when it was not.
    """
    metadata = item.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("identity_invalidated") is True:
        return None
    delivery = metadata.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("identity_guard") != OBSERVED:
        return None
    if recipient["terminal"] is None:
        return None
    derived = "core:local" if recipient["machine"] is None else f"core:{recipient['machine']}"
    recorded = delivery.get("identity_observer", derived)
    return derived if recorded == derived else None


def receipt(item: Any) -> dict[str, Any] | None:
    """One stored receipt in the document's shape, or None when it has no usable id."""
    if not isinstance(item, dict):
        return None
    ident = bounds.clean(item.get("id"), bounds.NAME)
    if ident is None:
        return None
    state = item.get("status", item.get("state"))
    reason = item.get("reason")
    cause = item.get("cause")
    recipient = _recipient(item.get("target"))
    observer = _observer(item, recipient)
    return {
        "id": ident,
        "state": state if state in STATES else "unknown",
        "title": bounds.clean(item.get("title"), bounds.TITLE),
        "recipient": recipient,
        "request_key": bounds.clean(item.get("request_key"), bounds.TEXT),
        "reason": reason if isinstance(reason, str) and _CODE.fullmatch(reason) else None,
        "cause": cause if cause in CAUSES else None,
        "identity_guard": OBSERVED if observer is not None else None,
        "identity_observer": observer,
        "created": _number(item.get("created")),
        "updated": _number(item.get("updated")),
    }


def status_problem(status: Any) -> str | None:
    """Why a status answer cannot be used, or None when it can."""
    if not isinstance(status, dict) or status.get("running") is not True:
        return "status does not say that the service is running"
    problem = counts_problem(status.get("tasks"))
    return None if problem is None else f"status answer has {problem}"


def counts_problem(raw: Any) -> str | None:
    """Why counts in the shape of ``status.tasks`` cannot be used, or None when they can."""
    if not isinstance(raw, dict):
        return "no receipt counts"
    for state in STATES:
        if state in raw and _count(raw[state]) is None:
            return f"an unusable count for {state}"
    return None


def tasks_problem(tasks: Any) -> str | None:
    """Why a list of receipts cannot be used, or None when it can."""
    if not isinstance(tasks, list):
        return "list of receipts cannot be used"
    for item in tasks:
        if receipt(item) is None:
            return "list of receipts holds an entry without a usable id"
    return None


def counts(raw: Any) -> dict[str, int] | None:
    """``{<state>: n, ..., total: n}`` from the status answer's task counts."""
    if not isinstance(raw, dict):
        return None
    result = {state: 0 for state in STATES}
    for key, value in raw.items():
        n = _count(value)
        if key in result and n is not None:
            result[key] = n
    result["total"] = sum(result.values())
    return result


@dataclass(frozen=True)
class Good:
    """The last good read of the receipts."""

    source: str
    read_at: float
    counts: dict[str, int] | None
    needs_person: list[dict[str, Any]]
    recent: list[dict[str, Any]]


def good(
    source: str,
    read_at: float,
    status: dict[str, Any],
    tasks: list[Any],
    snapshot_counts: dict[str, Any] | None = None,
) -> Good:
    """Build a good read from a newest-first list of stored receipts.

    The counts come from ``snapshot_counts`` when the snapshot gave them, so that list and
    counts are from one read; else from ``status``.
    """
    raw = status.get("tasks") if snapshot_counts is None else snapshot_counts
    mapped = [r for r in (receipt(item) for item in tasks) if r is not None]
    waiting = [r for r in mapped if r["state"] in NEEDS]
    waiting.sort(key=lambda r: (r["created"] is None, r["created"] or 0.0))
    if len(waiting) > bounds.NEEDS_PERSON:
        waiting = waiting[-bounds.NEEDS_PERSON :]
    return Good(source, read_at, counts(raw), waiting, mapped[: bounds.RECENT])


def view(good: Good | None, *, stale: bool, error: str | None, source: str) -> dict[str, Any]:
    """The ``receipts`` section. Before any good read nothing claims an empty list."""
    if good is None:
        return {
            "source": source,
            "read_at": None,
            "stale": True,
            "error": bounds.clean(error, bounds.TEXT) or "no good read yet",
            "counts": None,
            "needs_person": [],
            "needs_person_complete": False,
            "needs_person_omitted": None,
            "recent": [],
        }
    found = len(good.needs_person)
    expected = None if good.counts is None else sum(good.counts[s] for s in NEEDS)
    return {
        "source": good.source,
        "read_at": good.read_at,
        "stale": stale,
        "error": bounds.clean(error, bounds.TEXT),
        "counts": good.counts,
        "needs_person": list(good.needs_person),
        # A stale read says nothing about now: it is never complete.
        "needs_person_complete": not stale and expected is not None and found == expected,
        "needs_person_omitted": None if expected is None else max(0, expected - found),
        "recent": list(good.recent),
    }


def _clients(raw: Any) -> list[dict[str, Any]]:
    rows = raw.get("clients") if isinstance(raw, dict) else raw
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "client": bounds.clean(row.get("client"), bounds.NAME),
                "available": _flag(row.get("available")),
                "live_instances": _count(row.get("live_instances")),
                "last_heartbeat_age_s": _number(row.get("last_heartbeat_age")),
            }
        )
        if len(result) == bounds.CLIENTS:
            break
    return result


def health(status: dict[str, Any], *, read_at: float, version: str | None = None) -> dict[str, Any]:
    """The ``health`` section from a status answer.

    ``version``: the bridge's version as the ``fleet_snapshot`` answer's ``health`` said it,
    when that answer was used; it is preferred to the version of ``status``, which an older
    status shape may leave out.
    """
    raw_bus = status.get("bus")
    bus: dict[str, Any] = raw_bus if isinstance(raw_bus, dict) else {}
    accepting = _flag(status.get("accepting"))
    return {
        "source": "status",
        "read_at": read_at,
        "stale": False,
        "error": None,
        "service": {
            "state": "stopping" if accepting is False else "running",
            "accepting": accepting,
            "started": _number(status.get("started")),
            "version": version or bounds.clean(status.get("version"), bounds.NAME),
            "instance_id": bounds.clean(status.get("instance_id"), bounds.NAME),
        },
        "bus": {
            "connected": _flag(bus.get("connected")),
            "last_error": bounds.clean(bus.get("last_error"), bounds.TEXT),
        },
        "clients": _clients(status.get("clients")),
    }


def unreachable(previous: dict[str, Any] | None, *, error: str) -> dict[str, Any]:
    """The ``health`` section when the service did not answer; ``read_at`` stays."""
    return {
        "source": "status",
        "read_at": None if previous is None else previous["read_at"],
        "stale": True,
        "error": bounds.clean(error, bounds.TEXT) or "service did not answer",
        "service": {
            "state": "unreachable",
            "accepting": None,
            "started": None,
            "version": None,
            "instance_id": None,
        },
        "bus": {"connected": None, "last_error": None},
        "clients": [],
    }
