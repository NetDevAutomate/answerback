"""Pure, bounded receipt projections for the fleet document reader."""

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any
from uuid import UUID

from .tasks import STATES

SCHEMA = "answerback.fleet_snapshot/1"
# Reserve room for runtime health and the ordinary RPC envelope.
MAX_BYTES = 256 * 1024 - 2048
_EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_DATE = r"20[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])"
_PREFIX = re.compile(rf"^[A-Za-z0-9_]+-{_DATE}-")
_DATE_TOKEN = re.compile(rf"-{_DATE}(?=-|$)")


@dataclass(frozen=True)
class ReasonRule:
    """An exact detail, a bounded-name template, or one approved fixed beginning."""

    code: str
    parts: tuple[str, ...]
    sources: Mapping[str, tuple[int, ...]]
    name_at: int | None = 1
    begins: bool = False

    def __post_init__(self) -> None:
        """Keep beginning matches restricted to one fixed part."""
        if self.begins and len(self.parts) != 1:
            raise ValueError("A beginning rule requires exactly one part")

    def matches(self, detail: str) -> bool:
        """Match the declared fixed text without returning any stored detail."""
        if self.begins:
            return detail.startswith(self.parts[0])
        if len(self.parts) == 1 or self.name_at is None:
            return detail == "".join(self.parts)
        prefix = "".join(self.parts[: self.name_at])
        suffix = "".join(self.parts[self.name_at :])
        return (
            detail.startswith(prefix)
            and detail.endswith(suffix)
            and _EXCEPTION_NAME.fullmatch(detail[len(prefix) : -len(suffix)]) is not None
        )


# Each source records a minimum count of each fragment among its AST constants.
# Rules shared by multiple providers stay in the kernel; provider-specific rules arrive
# through ``ProviderPlugin.receipt_reasons``. ``REASON_ORDER`` restores the historical
# pre-split sequence when those independently owned tables are composed.
REASONS = (
    ReasonRule(
        "bridge_stopped_during_submission",
        ("Bridge stopped during submission; inspect before abandoning",),
        {"tasks.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_interrupted",
        ("Submission was interrupted",),
        {
            "providers/codex/adapter.py": (2,),
            "providers/herdr/adapters/herdr.py": (1,),
        },
        name_at=1,
    ),
    ReasonRule(
        "turn_start_unobserved",
        ("Core accepted text but did not observe the turn start",),
        {"tasks.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "permission_request_too_large",
        (": ", "Permission request exceeds the supported bounds"),
        {
            "providers/kiro/workers.py": (1, 0),
            "worker_runtime.py": (0, 1),
            "providers/native/workers.py": (1, 0),
        },
        name_at=0,
    ),
)
REASON_ORDER = (0, 3, 14, 57)
HISTORICAL_REASON_COUNT = 61


def clean(value: str, limit: int) -> str:
    """Remove Unicode controls and bound the remaining characters."""
    return "".join(islice((c for c in value if not unicodedata.category(c).startswith("C")), limit))


def title(request_key: str, target: str, bus_keys: Mapping[str, str]) -> str | None:
    """Name work only from its sender key, never from receipt bodies or identifiers."""
    target = target[:512]
    if request_key.startswith("bus:"):
        key = bus_keys.get(request_key[4:])
        if key is None:
            return None
    else:
        key = request_key
    key = key[:512]
    key, removed = _PREFIX.subn("", key, count=1)
    if not removed:
        key = _DATE_TOKEN.sub("", key, count=1)
    terminal = re.split(r"[:/]", target)[-1] if target.startswith("hc:") else ""
    if re.fullmatch(r"t[A-Za-z0-9]+", terminal):
        key = key.removesuffix("-" + terminal)
    # Test identifiers before the display cut; a long title is not an identifier.
    key = clean(key, len(key))
    if not key:
        return None
    try:
        UUID(key.removeprefix("bus:"))
    except ValueError:
        return key[:80]
    return None


def reason(
    status: str, detail: str | None, rules: Sequence[ReasonRule] = REASONS
) -> tuple[str | None, str | None]:
    """Classify only fixed writer sentences; free details never leave the process.

    ``rules`` is the kernel table by default; the service passes it extended with every
    loaded plugin's rules (``plugin.receipt_reasons``)."""
    if status == "abandoned":
        return "abandoned_by_person", None
    if status not in {"failed", "uncertain", "blocked", "cancelled"}:
        return None, None
    if detail is not None:
        for rule in rules:
            if rule.matches(detail):
                return (
                    rule.code,
                    "recipient_not_ready" if rule.code == "recipient_not_ready" else None,
                )
    return "unclassified", None


def _timestamp(value: Any) -> int | float | None:
    """Retain finite numeric times, excluding booleans and unrepresentable integers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def row(
    task: Mapping[str, Any], bus_keys: Mapping[str, str], rules: Sequence[ReasonRule] = REASONS
) -> dict[str, Any]:
    """Project exactly nine fields, excluding all receipt content and metadata."""
    ident = task["id"][:512]
    request_key = task["request_key"][:512]
    target = task["target"][:512]
    status = task["status"][:512]
    code, cause = reason(task["status"], task.get("detail"), rules)
    return {
        "id": clean(ident, 64),
        "request_key": clean(request_key, 160),
        "target": clean(target, 160),
        "status": clean(status, 64),
        "created": _timestamp(task["created"]),
        "updated": _timestamp(task["updated"]),
        "title": title(request_key, target, bus_keys),
        "reason": code,
        "cause": cause,
    }


def build(
    *,
    read_at: float,
    counts: Mapping[str, int] | None,
    needs_person: tuple[list[dict[str, Any]], int] | None,
    recent: list[dict[str, Any]] | None,
    bus_keys: Mapping[str, str],
    errors: list[dict[str, str]],
    rules: Sequence[ReasonRule] = REASONS,
) -> dict[str, Any]:
    """Build the receipt sections; the runtime supplies its own health separately."""
    if needs_person is None and recent is None:
        raise ValueError("store read failed")
    needed = (
        None if needs_person is None else [row(t, bus_keys, rules) for t in needs_person[0][:200]]
    )
    newest = None if recent is None else [row(t, bus_keys, rules) for t in recent[:20]]
    tasks = []
    seen: set[str] = set()
    for item in (newest or []) + (needed or []):
        if item["id"] not in seen:
            tasks.append(item)
            seen.add(item["id"])
    result = {
        "schema": SCHEMA,
        "read_at": read_at,
        "counts": None
        if counts is None
        else {
            **{state: counts.get(state, 0) for state in STATES},
            "total": sum(counts.values()),
        },
        "needs_person": needed,
        "needs_person_total": None if needs_person is None else needs_person[1],
        "needs_person_omitted": None
        if needed is None or needs_person is None
        else needs_person[1] - len(needed),
        "recent": newest,
        "tasks": tasks,
        "errors": errors,
    }
    # The three lists repeat rows on the wire. Unicode and JSON escaping can exceed
    # a byte bound even within character limits. Keep every row; shorten display text.
    for attempt in range(9):
        if len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_BYTES:
            return result
        if attempt == 8:
            break
        shortened = False
        for item in (newest or []) + (needed or []):
            for field in ("request_key", "target", "title"):
                value = item[field]
                if value:
                    item[field] = value[: len(value) // 2] or (None if field == "title" else "")
                    shortened = True
        if not shortened:
            break
    raise ValueError("store read failed")
