"""The fleet document: assembly, the size rule and the check before publication. Pure."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from . import advice, bounds, cadence, readings, receipts

SCHEMA = "answerback.fleet/1"
LIMIT = bounds.DOCUMENT_BYTES
MODES = tuple(cadence.INTERVALS)
PAUSE_REASONS = ("pressure_critical", "swap_exhausted")
SERVICE_STATES = ("running", "stopping", "unreachable")
SOURCES = ("fleet_snapshot", "status+task_list")
SIGNALS = ("cpu", "memory", "agents")


@dataclass(frozen=True)
class Inputs:
    """Everything one document is built from; the writer's loop owns these values."""

    writer: dict[str, Any]
    cadence: cadence.Cadence
    paused_since: float | None
    taken_at: float | None
    node_order: list[str]
    links: dict[str, str]
    memos: dict[str | None, readings.Memo]
    active: dict[str | None, int]
    lanes: dict[str | None, int] | None
    ceilings: dict[str, int]
    launches: tuple[advice.Launch, ...]
    health: dict[str, Any]
    receipts: dict[str, Any]
    errors: list[dict[str, Any]]


def _error(item: dict[str, Any]) -> dict[str, Any]:
    at = item.get("at")
    return {
        "at": at if isinstance(at, int | float) and math.isfinite(at) else None,
        "where": bounds.clean(item.get("where"), bounds.NAME),
        "text": bounds.clean(item.get("text"), bounds.TEXT),
    }


def _lanes(inputs: Inputs, name: str | None) -> int | None:
    if inputs.lanes is None or name not in inputs.active:
        return None
    return inputs.lanes.get(name, 0)


def build(inputs: Inputs, *, written_at: float, now_mono: float) -> dict[str, Any]:
    """The whole document at ``written_at``; expiry is judged at ``now_mono``."""
    order = inputs.node_order[: bounds.MACHINES - 1]
    machines = [
        readings.entry(
            None,
            inputs.memos.get(None, readings.Memo(error="not measured yet")),
            link="self",
            now_mono=now_mono,
            active=inputs.active.get(None),
            ceiling=None,
            lanes=_lanes(inputs, None),
        )
    ]
    candidates = []
    for name in order:
        link = bounds.clean(inputs.links.get(name), bounds.NAME) or "unknown"
        memo = inputs.memos.get(name, readings.Memo())
        machines.append(
            readings.entry(
                name,
                memo,
                link=link,
                now_mono=now_mono,
                active=inputs.active.get(name),
                ceiling=inputs.ceilings.get(name),
                lanes=_lanes(inputs, name),
            )
        )
        last = memo.last
        candidates.append(
            advice.Candidate(
                name,
                link,
                readings.is_current(memo, now_mono),
                None if last is None else last.assessment.state,
                None if last is None else last.reading.suites_running,
                None if last is None else last.assessment,
            )
        )
    pause = inputs.cadence.pause
    advised = advice.advise(
        candidates,
        order=order,
        launched=advice.launched_on(inputs.launches, now_mono),
        paused=pause is not None,
    )
    return {
        "schema": SCHEMA,
        "written_at": written_at,
        "writer": {
            **inputs.writer,
            "mode": inputs.cadence.mode,
            "interval_s": inputs.cadence.interval_s,
        },
        "health": inputs.health,
        "capacity": {
            "taken_at": inputs.taken_at,
            "max_age_s": int(readings.MAX_AGE_S),
            "paused": None
            if pause is None
            else {"reason": pause.reason, "since": inputs.paused_since, "text": pause.text},
            "node_order": list(order),
            **advised,
            "recent_launches": advice.recent(inputs.launches, now_mono),
        },
        "machines": machines,
        "receipts": inputs.receipts,
        "errors": [_error(item) for item in inputs.errors[-bounds.ERRORS :]],
    }


def _dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def encode(doc: dict[str, Any]) -> bytes:
    """The document's bytes, made to fit 256 KiB by the size rule.

    When too large: drop ``recent``, then the oldest entries of ``needs_person``, and
    count what was dropped in ``needs_person_omitted``. Nothing is cut silently.
    """
    data = _dump(doc)
    if len(data) <= LIMIT:
        return data
    rc = dict(doc["receipts"])
    doc = {**doc, "receipts": rc}
    rc["recent"] = []
    needs = list(rc["needs_person"])
    data = _dump(doc)
    dropped = 0
    sizes = [len(_dump(item)) + 1 for item in needs]
    while len(data) > LIMIT and needs:
        excess = len(data) - LIMIT
        count = 0
        freed = 0
        while count < len(needs) and freed < excess:
            freed += sizes[count]
            count += 1
        count = max(1, count)
        needs, sizes = needs[count:], sizes[count:]
        dropped += count
        rc["needs_person"] = needs
        rc["needs_person_omitted"] = (rc.get("needs_person_omitted") or 0) + count
        rc["needs_person_complete"] = False
        data = _dump(doc)
    return data


def content_key(doc: dict[str, Any]) -> bytes:
    """The document without ``written_at``: what "changed" compares."""
    return _dump({key: value for key, value in doc.items() if key != "written_at"})


# ---------------------------------------------------------------- the check before publication


@dataclass(frozen=True)
class Field:
    """One declared field: its kind, whether ``null`` is allowed, and what it holds.

    ``kind`` is ``text``, ``number`` (finite, not a bool), ``count`` (a whole number of
    zero or more), ``flag``, ``word`` (one of ``words``), ``object`` (``fields``),
    ``list`` (of ``items``) or ``counts`` (an object whose values are counts).
    """

    kind: str
    null: bool = False
    words: tuple[Any, ...] = ()
    fields: dict[str, Field] | None = None
    items: Field | None = None


def _f(kind: str, *, null: bool = False) -> Field:
    return Field(kind, null)


def _w(words: tuple[Any, ...], *, null: bool = False) -> Field:
    return Field("word", null, words)


def _o(fields: dict[str, Field], *, null: bool = False) -> Field:
    return Field("object", null, fields=fields)


def _l(items: Field) -> Field:
    return Field("list", items=items)


TEXT, NUMBER, COUNT, FLAG = _f("text"), _f("number"), _f("count"), _f("flag")
TEXT_N, NUMBER_N, COUNT_N, FLAG_N = (_f(k, null=True) for k in ("text", "number", "count", "flag"))
_CHOICE = _o({"machine": TEXT_N, "why": TEXT})
_RECEIPT = _o(
    {
        "id": TEXT,
        "state": _w(receipts.RECEIPT_STATES),
        "title": TEXT_N,
        "recipient": _o(
            {"provider": TEXT_N, "machine": TEXT_N, "terminal": TEXT_N, "text": TEXT_N}
        ),
        "request_key": TEXT_N,
        "reason": TEXT_N,
        "cause": _w(receipts.CAUSES, null=True),
        "identity_guard": _w((receipts.OBSERVED,), null=True),
        "identity_observer": TEXT_N,
        "created": NUMBER_N,
        "updated": NUMBER_N,
    }
)
_MACHINE = _o(
    {
        "name": TEXT_N,
        "self": FLAG,
        "role": _w(("coordinator", "worker")),
        "os": TEXT_N,
        "link": _o({"state": TEXT, "since_ms": NUMBER_N, "why": TEXT_N}),
        "reading": _o(
            {
                "taken_at": NUMBER_N,
                "error": TEXT_N,
                "state": _w(readings.STATES),
                "was": _w(readings.MEASURED, null=True),
                "decided_by": _l(
                    _o({"signal": _w(SIGNALS), "state": _w(("busy", "full")), "text": TEXT})
                ),
                "pressure": _o(
                    {"signal": _w(SIGNALS), "fraction": NUMBER, "text": TEXT}, null=True
                ),
                "load_per_core": NUMBER_N,
                "loads": Field("list", True, items=NUMBER),
                "cores": COUNT_N,
                "memory": _o({"state": _w(readings.STATES, null=True), "text": TEXT_N}),
                "agents": _o({"active": COUNT_N, "ceiling": COUNT_N, "lanes": COUNT_N}),
                "suites": COUNT_N,
                "harness_processes": COUNT_N,
            }
        ),
    }
)
# Every field that section 5 of the specification declares, with its type.
FIELDS: dict[str, Field] = {
    "schema": _w((SCHEMA,)),
    "written_at": NUMBER,
    "writer": _o(
        {
            "version": TEXT_N,
            "instance_id": TEXT,
            "pid": COUNT,
            "mode": _w(MODES),
            "interval_s": NUMBER,
        }
    ),
    "health": _o(
        {
            "source": _w(("status",)),
            "read_at": NUMBER_N,
            "stale": FLAG,
            "error": TEXT_N,
            "service": _o(
                {
                    "state": _w(SERVICE_STATES),
                    "accepting": FLAG_N,
                    "started": NUMBER_N,
                    "version": TEXT_N,
                    "instance_id": TEXT_N,
                }
            ),
            "bus": _o({"connected": FLAG_N, "last_error": TEXT_N}),
            "clients": _l(
                _o(
                    {
                        "client": TEXT_N,
                        "available": FLAG_N,
                        "live_instances": COUNT_N,
                        "last_heartbeat_age_s": NUMBER_N,
                    }
                )
            ),
        }
    ),
    "capacity": _o(
        {
            "taken_at": NUMBER_N,
            "max_age_s": NUMBER,
            "paused": _o({"reason": _w(PAUSE_REASONS), "since": NUMBER_N, "text": TEXT}, null=True),
            "node_order": _l(TEXT),
            "ranked_for_heavy_work": _l(TEXT),
            "new_work": _CHOICE,
            "new_suite": _CHOICE,
            "recent_launches": _l(_o({"machine": TEXT_N, "seen_at": NUMBER, "count": COUNT})),
        }
    ),
    "machines": _l(_MACHINE),
    "receipts": _o(
        {
            "source": _w(SOURCES),
            "read_at": NUMBER_N,
            "stale": FLAG,
            "error": TEXT_N,
            "counts": Field("counts", True),
            "needs_person": _l(_RECEIPT),
            "needs_person_complete": FLAG,
            "needs_person_omitted": COUNT_N,
            "recent": _l(_RECEIPT),
        }
    ),
    "errors": _l(_o({"at": NUMBER_N, "where": TEXT_N, "text": TEXT_N})),
}


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _walk(value: Any, field: Field, where: str, problems: list[str]) -> None:
    """Add a problem for every place where ``value`` is not of the declared ``field``."""
    if value is None:
        if not field.null:
            problems.append(f"{where} is null")
        return
    kind = field.kind
    if kind == "text":
        ok = isinstance(value, str)
    elif kind == "number":
        ok = _finite(value)
    elif kind == "count":
        ok = _is_count(value)
    elif kind == "flag":
        ok = isinstance(value, bool)
    elif kind == "word":
        ok = isinstance(value, str | int | float) and value in field.words
    elif kind == "counts":
        ok = isinstance(value, dict) and all(_is_count(v) for v in value.values())
    elif kind == "object":
        if not isinstance(value, dict):
            problems.append(f"{where} is not an object")
            return
        for name, inner in (field.fields or {}).items():
            if name not in value:
                problems.append(f"{where}.{name} is missing")
            else:
                _walk(value[name], inner, f"{where}.{name}", problems)
        return
    elif kind == "list":
        if not isinstance(value, list):
            problems.append(f"{where} is not a list")
            return
        assert field.items is not None
        for index, item in enumerate(value):
            _walk(item, field.items, f"{where}[{index}]", problems)
        return
    else:
        ok = False
    if not ok:
        problems.append(f"{where} has the wrong type or word")


def _check_machines(machines: list[Any], problems: list[str]) -> None:
    """What the table cannot say: the coordinator first, then named workers."""
    if not machines:
        problems.append("machines is empty")
        return
    for index, m in enumerate(machines):
        if not isinstance(m, dict):
            continue  # already a problem of the table
        coordinator = index == 0
        if m.get("self") is not coordinator or m.get("role") != (
            "coordinator" if coordinator else "worker"
        ):
            problems.append(f"machines[{index}] role")
        if coordinator:
            link = m.get("link")
            if m.get("name") is not None or not isinstance(link, dict):
                problems.append("machines[0] is not the coordinator")
            elif link.get("state") != "self":
                problems.append("machines[0] link is not self")
        elif not bounds.machine_name_ok(m.get("name")):
            problems.append(f"machines[{index}] name")


def _refuse(word: str) -> Any:
    raise ValueError(f"{word} is not a finite number")


def validate(data: bytes) -> list[str]:
    """Problems that stop a publication; an empty list means the document may be published.

    Every field of the table ``FIELDS`` must be there with its declared type, or ``null``
    where that is allowed; a word field must hold an allowed word. It never raises.
    """
    try:
        doc = json.loads(data, parse_constant=_refuse)
    except (ValueError, UnicodeDecodeError, RecursionError, TypeError):
        return ["not JSON"]
    if not isinstance(doc, dict):
        return ["not an object"]
    problems: list[str] = []
    try:
        if len(data) > LIMIT:
            problems.append("larger than 256 KiB")
        _walk(doc, _o(FIELDS), "document", problems)
        if isinstance(doc.get("machines"), list):
            _check_machines(doc["machines"], problems)
    except Exception as error:  # a shape no rule above foresaw is still a problem
        problems.append(f"unexpected shape ({type(error).__name__})")
    return [problem.removeprefix("document.") for problem in problems]
