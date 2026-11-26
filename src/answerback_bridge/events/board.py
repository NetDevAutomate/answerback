"""The board as a fold over the stream: events in, tables out. Holds nothing of its own.

`agent-bus monitor --board` renders this in a terminal; the web dashboard's `dashboard.js`
performs the same fold in the browser.
"""

from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import model
from .patch import apply

OPEN_FIRST = {"completed": 1, "failed": 1, "abandoned": 1, "cancelled": 1}
WINDOW_MS = 24 * 60 * 60 * 1000
MAX_ACTIVITIES = 64  # Whatever the stream claims, this fold holds no more than this.


def in_window(run: dict[str, Any], now_ms: int) -> bool:
    """The default view: the last 24 hours, plus anything unfinished or needing attention
    whatever its age. Everything else is history, shown with `--all` until retention
    deletes it. `dashboard.js` applies the same rule."""
    return bool(
        run["status"] not in OPEN_FIRST
        or run.get("attention")
        or run["updatedMs"] >= now_ms - WINDOW_MS
    )


def _table(
    title: str,
    header: tuple[str, ...],
    rows: list[tuple[str, ...]],
    limit: int,
    note: str = "",
) -> str:
    """`limit` 0 shows every row."""
    limit = limit or len(rows)
    shown = rows[:limit]
    widths = [
        min(44, max(len(header[i]), *(len(row[i]) for row in shown), 1)) for i in range(len(header))
    ]

    def fit(cell: str, width: int) -> str:
        if len(cell) <= width:
            return cell.ljust(width)
        # A path is told apart by its end, other text by its start.
        return "…" + cell[1 - width :] if cell.startswith("/") else cell[: width - 1] + "…"

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(
            fit(cell, width) for cell, width in zip(cells, widths, strict=True)
        ).rstrip()

    more = f" (+{len(rows) - limit} more; --rows 0 shows every row)" if len(rows) > limit else ""
    body = [line(row) for row in shown] or ["  (none)"]
    return "\n".join([f"{title} · {len(rows)}{more}{note}", line(header), *body])


def _text(value: Any) -> str:
    """Stream content is untrusted: printable characters only reach the terminal."""
    if value is None:
        return "—"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return model.bounded(str(value), 200)


def _moment(seconds: float, now: datetime) -> datetime:
    """`seconds` in the viewer's zone, at the offset in force at that moment, not today's.

    A naive `now` is this process's local time: each value goes through the system zone, so
    a summer time read in winter keeps its summer offset. An aware `now` supplies the zone;
    a named one (`ZoneInfo`) converts each value the same way.
    """
    if now.tzinfo is None:
        return datetime.fromtimestamp(seconds).astimezone()
    return datetime.fromtimestamp(seconds, now.tzinfo)


def _clock(milliseconds: Any, now: datetime | None = None) -> str:
    """A moment in the viewer's time zone: the time alone on the viewer's current calendar
    day, the date and the time on any other. `now` is the viewer's time; see `_moment`."""
    if type(milliseconds) is not int:
        return "—"
    now = now or datetime.now()
    try:
        moment = _moment(milliseconds / 1000, now)
    except (OverflowError, OSError, ValueError):
        return "—"
    same_day = moment.date() == now.date()
    return moment.strftime("%H:%M:%S" if same_day else "%Y-%m-%d %H:%M:%S")


def _seconds_ms(seconds: Any) -> int | None:
    """Pressure times are wall-clock seconds; the board's clock takes milliseconds."""
    if type(seconds) not in (int, float) or seconds != seconds or abs(seconds) > 1e14:
        return None
    return int(seconds * 1000)


def _throttled(value: dict[str, Any]) -> str:
    return model.bounded(
        f"{_text(value.get('provider'))}:{_text(value.get('account_hint'))} throttled "
        f"({_text(value.get('signature'))} on {_text(value.get('harness'))} "
        f"{_text(value.get('source'))}); level {_text(value.get('level'))}, dispatch waits "
        f"until {_clock(_seconds_ms(value.get('until')))}",
        800,
    )


class Board:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None
        self.activities: dict[str, dict[str, Any]] = {}
        # The first cell is the event's timestamp; it is formatted when the board is drawn,
        # so "today" is the day of drawing.
        self.recent: deque[tuple[Any, ...]] = deque(maxlen=12)
        self.alerts: deque[str] = deque(maxlen=12)
        self.cursor: str | None = None
        self.skipped = 0
        self.run_page: dict[str, Any] | None = None

    def apply(self, ident: str, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "STATE_SNAPSHOT":
            # A snapshot is a complete restart: the live activities follow it again.
            self.state = event["snapshot"]
            self.activities = {}
            self.recent.clear()  # The connect page restates recent events after it.
        elif kind == "STATE_DELTA":
            if self.state is None:
                self.skipped += 1  # No base yet: a delta without its snapshot is not applied.
            else:
                self.state = apply(self.state, event["delta"])
                self._prune()
        elif kind == "ACTIVITY_SNAPSHOT":
            message = event["messageId"]
            if message in self._listed() and (
                message in self.activities or len(self.activities) < MAX_ACTIVITIES
            ):
                self.activities[message] = event["content"]
            else:
                self.skipped += 1  # Not a live activity: holding it would never end.
        elif kind == "ACTIVITY_DELTA":
            if event["messageId"] in self.activities:
                self.activities[event["messageId"]] = apply(
                    self.activities[event["messageId"]], event["patch"]
                )
            else:
                self.skipped += 1
        elif kind in model.LIFECYCLE or kind == "CUSTOM":
            if event.get("name") == "answerback.anomaly":
                self.alerts.append(model.bounded((event.get("value") or {}).get("message"), 800))
            elif event.get("name") == "answerback.provider_throttled":
                self.alerts.append(_throttled(event.get("value") or {}))
            tags = event.get("metadata", {}).get(model.NAMESPACE, {})
            self.recent.append(
                (
                    event.get("timestamp"),
                    _text(kind),
                    _text(tags.get("threadId") or event.get("name")),
                    _text((tags.get("runId") or "")[:8] or None),
                    _text(
                        event.get("stepName")
                        or event.get("message")
                        or (event.get("value") or {}).get("message")
                        or (event.get("outcome") or {}).get("type")
                        or tags.get("peerId")
                    ),
                )
            )
        self.cursor = ident

    def _listed(self) -> dict[str, Any]:
        listed = (self.state or {}).get("activities")
        return listed if isinstance(listed, dict) else {}

    def _prune(self) -> None:
        """An activity the state no longer lists has ended."""
        listed = self._listed()
        self.activities = {
            ident: content for ident, content in self.activities.items() if ident in listed
        }

    def render(self, *, rows: int = 12, now: datetime | None = None, history: bool = False) -> str:
        """`history` shows every retained run; otherwise the default window. `rows` 0 shows
        every selected row of each table."""
        if self.state is None:
            return "Waiting for the first state snapshot…"
        state = self.state
        now = now or datetime.now()  # Naive: each time is converted at its own offset.
        now_ms = int(now.timestamp() * 1000)
        debugging = state["bridge"].get("debug", {}).get("peers", {})

        def badge(ident: str) -> str:
            until = debugging.get(ident)
            return " [DEBUG]" if type(until) is int and until > now_ms else ""

        current_runs = self.run_page["runs"] if history and self.run_page else state["runs"]
        retained = len(current_runs)
        runs = sorted(
            (item for item in current_runs.items() if history or in_window(item[1], now_ms)),
            key=lambda item: (OPEN_FIRST.get(item[1]["status"], 0), -item[1]["updatedMs"]),
        )
        if history:
            note = " · all retained history"
            if self.run_page:
                page = self.run_page
                note += f" · offset {page['offset']} of {page['total']}"
                if page["nextOffset"] is not None:
                    note += f"; --page {page['offset'] // 100 + 2} for more"
        else:
            note = " · last 24 h, unfinished and failed"
            if retained > len(runs):
                note += f"; {retained - len(runs)} older in --all"
        omitted = state["bridge"].get("runsOmitted")
        if omitted and not (history and self.run_page):
            note += f" · truncated, {omitted} more; --all --page N reads retained history"
        # By run, not by peer: a worker's earlier run says nothing about its next one.
        activity = {content.get("runId"): content for content in self.activities.values()}
        sections = [
            _table(
                "RUNS",
                ("task", "run", "peer", "status", "updated", "screen changed"),
                [
                    (
                        _text(run["threadId"]),
                        _text(ident[:8]),
                        _text(run["peerId"]) + badge(run["peerId"]),
                        _text(run["status"]),
                        _clock(run["updatedMs"], now),
                        _clock(activity.get(ident, {}).get("changedMs"), now),
                    )
                    for ident, run in runs
                ],
                rows,
                note,
            ),
            _table(
                "PEERS",
                (
                    "peer",
                    "name",
                    "agent",
                    "state",
                    "ready",
                    "run",
                    "tokens",
                    "CPU %",
                    "RSS MiB",
                    "children",
                    "since",
                    "health",
                ),
                [
                    (
                        _text(ident) + badge(ident),
                        _text(peer["name"]),
                        _text(peer["agentKind"] or None),
                        _text(peer["state"]),
                        _text(peer["sendReady"]),
                        _text((peer["runId"] or "")[:8] or None),
                        _text(peer["metrics"]["tokens"]),
                        _text(peer["metrics"]["cpuPercent"]),
                        _text(
                            round(peer["metrics"]["rssBytes"] / 1024**2, 1)
                            if peer["metrics"]["rssBytes"] is not None
                            else None
                        ),
                        _text(peer["metrics"]["children"]),
                        _clock(peer.get("trace", {}).get("sinceMs"), now),
                        _text(", ".join(peer.get("trace", {}).get("anomalies", [])) or None),
                    )
                    for ident, peer in sorted(state["peers"].items())
                ],
                rows,
            ),
            _table(
                "DECISIONS",
                ("id", "task", "class", "status", "needed by", "overdue", "question"),
                [
                    (
                        _text(ident),
                        _text(decision["task"]),
                        _text(decision["class"]),
                        _text(decision["status"]),
                        _text(decision["neededBy"]),
                        _text(decision["overdue"]),
                        _text(decision["title"]),
                    )
                    for ident, decision in sorted(state["decisions"].items(), reverse=True)
                ],
                rows,
            ),
            _table(
                "APPLY QUEUE",
                ("project", "lane", "items"),
                [
                    (_text(project), _text(lane), _text(", ".join(names) or None))
                    for project, queue in sorted(state["applyQueue"].items())
                    for lane, names in queue.items()
                ],
                rows,
            ),
            _table(
                "HEADS",
                ("worktree", "branch", "sha"),
                [
                    (_text(path), _text(head["branch"]), _text(head["sha"]))
                    for heads in state["heads"].values()
                    for path, head in sorted(heads.items())
                ],
                rows,
            ),
            *(
                [
                    _table(
                        "PRESSURE",
                        ("account", "level", "until", "last signal"),
                        [
                            (
                                _text(ident),
                                _text(row.get("level")),
                                _clock(_seconds_ms(row.get("until")), now),
                                _clock(_seconds_ms(row.get("last_signal")), now),
                            )
                            for ident, row in sorted(state["pressure"].items())
                            if isinstance(row, dict)
                        ],
                        rows,
                    )
                ]
                if isinstance(state.get("pressure"), dict)
                else []
            ),
            _table(
                "RECENT",
                ("time", "event", "task", "run", "detail"),
                [(_clock(stamp, now), *cells) for stamp, *cells in self.recent],
                rows,
            ),
        ]
        errors = state["bridge"].get("sourceErrors") or {}
        if self.alerts:
            sections.append("ALERTS\n" + "\n".join(self.alerts))
        if errors:
            sections.append(
                "SOURCES WITH ERRORS · "
                + ", ".join(f"{_text(k)}: {_text(v)}" for k, v in errors.items())
            )
        offset = now.astimezone().strftime("%z") if now.tzinfo is None else now.strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC+00:00"
        if isinstance(now.tzinfo, timezone):
            zone = offset  # One fixed offset, the same for every time shown.
        else:
            zone = f"today {offset}; each time is shown at the offset in force on its own date"
        sections.append(f"Times are local ({zone}); a date is shown for any other day.")
        return "\n\n".join(sections)
