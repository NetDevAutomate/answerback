"""Read-only lifecycle monitoring shared by the terminal and web frontends."""

import asyncio
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from .. import __version__
from ..process import BridgeError
from ..transport import RemoteBridge
from .config import BusConfig

EVENT_FIELDS = frozenset(
    {
        "delivery",
        "task_id",
        "status",
        "result_ref",
        "turn_id",
        "revision",
        "reason_code",
        "blocking_task_id",
    }
)


def cursor_value(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise ValueError("Event cursor must be an integer between 0 and 9223372036854775807")


def safe_text(value: object, limit: int = 1024) -> str:
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[:limit] + "…"


class BusNotConfigured(FileNotFoundError):
    """`agent-bus configure` never ran here: the bus is opt-in, so this is not a ledger failure."""

    def __init__(self) -> None:
        super().__init__(
            "Agent bus is not configured, so there are no lifecycle events; "
            "run agent-bus configure to enable it"
        )


def absent_or_empty(folder: Path) -> bool:
    """Whether `folder` does not exist, or is an ordinary folder without any entry.

    One open that follows no link, and one listing from that descriptor: the answer comes
    from a single object, not from two looks at a path. A symbolic link (dangling or not),
    anything that is not a folder and a folder that cannot be listed are neither.
    """
    try:
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        with os.scandir(descriptor) as entries:
            return next(entries, None) is None
    except OSError:
        return False
    finally:
        os.close(descriptor)


class Monitor:
    """What "not configured" means. A read by this monitor reports the bus as not configured when,
    and only when, all three hold: this monitor has not yet read a bus that had its `config.json`;
    the operating system says at this read that `config.json` does not exist; and the bus folder is
    absent, or this read opened it as an ordinary folder and its listing showed no entry. For such a
    read `snapshot()` returns `available: False, configured: False, error: None`, and `follow()`
    raises `BusNotConfigured`. Every other state in which this read does not find `config.json` is
    reported as a failure, with `available: False, configured: True` and the class of the error, and
    `follow()` raises `BridgeError`: a `config.json` that is absent after this monitor has read one;
    a bus folder that is a symbolic link or is not a folder; a bus folder that holds any entry
    without `config.json`; a bus folder that this read cannot search or cannot list, also when it
    holds no entry. A bus whose `config.json` is found is read as it was before this change, also
    when the bus folder is a symbolic link, so every failure of such a bus is the failure it was. So
    `configured: False` means that nothing here shows a bus was ever set up, and `configured: True`
    does not promise that `config.json` exists now.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        # True from the first read that found bus/config.json, and never cleared: what this
        # monitor has seen cannot be asked of the disk again once the file is gone.
        self._has_read_config = False

    def _read(self, after: int | None, limit: int) -> dict[str, Any]:
        bus = self.config_path.parent / "bus"
        try:
            # lstat, not exists(): a `bus` directory that cannot be entered and a dangling
            # config.json link are failures. A configured bus that lost its ledger or token
            # fails below.
            (bus / "config.json").lstat()
        except FileNotFoundError:
            # An absent config.json alone does not say "never configured" (BusAPI.status()
            # asks only that, with exists(); this is stricter on purpose). It is a failure
            # when an earlier read by this monitor found bus/config.json, and when the bus
            # folder is a link, is not a folder, cannot be listed or holds anything: bus files
            # without their configuration.
            # `agent-bus configure` creates the folder and its lock before config.json, so a
            # read at that moment can report a failure once; the next read is right. No retry.
            if self._has_read_config or not absent_or_empty(bus):
                raise
            raise BusNotConfigured from None
        self._has_read_config = True
        config = BusConfig.load(self.config_path)
        path = config.root / "ledger.sqlite"
        # Validate ownership/permissions without loading database bytes.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Monitor ledger must be a private regular file")
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            latest = db.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
            ).fetchone()[0]
            floor = db.execute("SELECT COALESCE(min(seq),?) FROM events", (latest + 1,)).fetchone()[
                0
            ]
            has_retention = (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='retention_records'").fetchone()
                is not None
            )
            retained_commands = (
                db.execute(
                    "SELECT count(*) FROM retention_records WHERE entity='commands' "
                    "AND record IS NOT NULL"
                ).fetchone()[0]
                if has_retention
                else 0
            )
            retention_join = (
                " LEFT JOIN retention_records r ON r.entity='commands' AND r.row_id=c.id "
                if has_retention
                else ""
            )
            envelope = (
                "CASE WHEN json_valid(c.payload) THEN c.payload "
                "ELSE json_extract(r.record,'$.envelope') END"
                if has_retention
                else "c.payload"
            )
            start = max(0, latest - limit) if after is None else after
            rows = db.execute(
                f"SELECT e.*, json_extract({envelope},'$.peer_id') AS peer_id, "
                f"json_extract({envelope},'$.project') AS project "
                "FROM events e LEFT JOIN commands c ON c.id=e.command_id "
                + retention_join
                + "WHERE e.seq>? ORDER BY e.seq LIMIT ?",
                (start, limit + 1),
            ).fetchall()
            states = dict(db.execute("SELECT delivery,count(*) FROM commands GROUP BY delivery"))
            tasks = dict(
                db.execute(
                    "SELECT json_extract(payload,'$.status'),count(*) FROM events "
                    "WHERE seq IN (SELECT max(seq) FROM events WHERE type LIKE 'task.%' "
                    "GROUP BY command_id) GROUP BY json_extract(payload,'$.status')"
                )
            )
            total = db.execute("SELECT count(*) FROM events").fetchone()[0]
            has_history = (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='retention_history'").fetchone()
                is not None
            )
            deleted = (
                db.execute(
                    "SELECT deleted_event_ceiling FROM retention_history WHERE singleton=1"
                ).fetchone()[0]
                if has_history
                else 0
            )
            retention_gap = start < deleted
            pending = db.execute(
                "SELECT count(*) FROM outbox WHERE published_at IS NULL"
            ).fetchone()[0]
            activity = [
                row[0]
                for row in db.execute(
                    "SELECT created FROM events WHERE created>=? ORDER BY seq DESC LIMIT 1000",
                    (time.time() - 60,),
                )
            ]
        events = []
        for row in rows[:limit]:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("Invalid stored event payload")
            if type(row["created"]) not in (int, float) or not math.isfinite(row["created"]):
                raise ValueError("Invalid stored event timestamp")
            events.append(
                {
                    "seq": row["seq"],
                    "event_id": safe_text(row["id"], 128),
                    "command_id": safe_text(row["command_id"], 128),
                    "type": safe_text(row["type"], 128),
                    "created_at": row["created"],
                    "peer_id": safe_text(row["peer_id"], 256),
                    "project": safe_text(row["project"]),
                    "payload": {
                        key: safe_text(value) if isinstance(value, str) else value
                        for key, value in payload.items()
                        if key in EVENT_FIELDS and (value is None or isinstance(value, str))
                    },
                }
            )
        return {
            "available": True,
            "configured": True,
            "events": events,
            "cursor": events[-1]["seq"] if events else max(start, latest),
            "retention_gap": retention_gap,
            "retained_event_floor": floor,
            "latest_cursor": latest,
            "reset_required": start > latest,
            "has_more": len(rows) > limit,
            "namespace": config.namespace,
            "stats": {
                "commands": sum(states.values()),
                "retained_commands": retained_commands,
                "task_counts_cover_retained_events_only": True,
                "delivery": states,
                "tasks": tasks,
                "events": total,
                "outbox_pending": pending,
                "activity": activity,
                "activity_limited": len(activity) == 1000,
            },
        }

    async def snapshot(self, *, after: int | None = None, limit: int = 100) -> dict[str, Any]:
        cursor_value(after)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event page size must be 1–1000")
        started = time.monotonic()
        try:
            frame = await asyncio.to_thread(self._read, after, limit)
        except BusNotConfigured:
            # Nothing failed: `available` stays False for readers that know only that key.
            frame = {
                "available": False,
                "configured": False,
                "stats": None,
                "events": [],
                "cursor": after or 0,
                "latest_cursor": None,
                "has_more": False,
                "reset_required": False,
                "error": None,
            }
        except (OSError, sqlite3.Error, ValueError, BridgeError) as error:
            frame = {
                "available": False,
                "configured": True,
                "stats": None,
                "events": [],
                "cursor": after or 0,
                "latest_cursor": None,
                "has_more": False,
                "reset_required": False,
                "error": type(error).__name__,
            }
        connection: dict[str, Any] = {"service_running": None, "broker_connected": None}
        if frame["available"]:
            try:
                async with asyncio.timeout(1):
                    status = await RemoteBridge(self.config_path).call("status", {})
                state = status["bus"]
                connection = {
                    "service_running": True,
                    "broker_connected": state.get("connected") is True,
                    "last_error": safe_text(state.get("last_error"), 128),
                    "outbox_errors": {
                        key: safe_text(value, 128)
                        for key, value in state.get("outbox_errors", {}).items()
                        if key in ("command", "event")
                    },
                }
            except (OSError, ValueError, BridgeError, TimeoutError, KeyError) as error:
                connection["observation_error"] = type(error).__name__
        return {
            "schema_version": 1,
            "version": __version__,
            "generated_at": time.time(),
            **frame,
            "connection": connection,
            "read_ms": round((time.monotonic() - started) * 1000, 1),
        }

    async def follow(
        self,
        *,
        after: int | None = None,
        count: int | None = None,
        interval: float = 1,
        on_configured: Callable[[], None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events as they appear; `on_configured` is called once, by the first read,
        when that read found a configured bus (readable or not), and before it raises."""
        cursor_value(after)
        if not math.isfinite(interval) or not 0.01 <= interval <= 60:
            raise ValueError("Monitor interval must be 0.01–60 seconds")
        if count is not None and (type(count) is not int or count < 1):
            raise ValueError("Monitor count must be positive")
        emitted = 0
        while True:
            frame = await self.snapshot(after=after)
            if not frame["configured"]:
                raise BusNotConfigured
            if on_configured is not None:
                on_configured()
                on_configured = None
            if not frame["available"]:
                raise BridgeError("Local event ledger is unavailable; inspect bus configuration")
            if frame.get("retention_gap"):
                raise ValueError(
                    "Local event history contains retention gaps; "
                    "inspect the snapshot before resuming"
                )
            if frame["reset_required"]:
                raise ValueError(
                    "Cursor is ahead of this history; inspect it before restarting at 0"
                )
            for event in frame["events"]:
                yield event
                after = event["seq"]
                emitted += 1
                if count is not None and emitted >= count:
                    return
            after = frame["cursor"]
            if not frame["has_more"]:
                await asyncio.sleep(interval)
