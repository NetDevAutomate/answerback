"""The one stream: current board state, live activities and a bounded event history.

Readers pull by cursor. A reader that falls out of the retained window, or that held a cursor
from another bridge start, is resynchronised with a snapshot, so a delta is never applied to
a state it was not computed against. Producers are never slowed by a reader.
"""

import copy
import hashlib
import json
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import model
from .debug import DebugController
from .patch import Operation, apply, diff

SCHEMA = "answerback.board/1"
# `<epoch>-<seq>`, or `<epoch>-<seq>.<part>` for an event inside a connect page that is
# not its last. A reader holding a part ID has not seen the whole page.
EVENT_ID = re.compile(r"([0-9a-f]{8})-(\d{1,18})(?:\.(\d{1,4}))?")
MAX_ACTIVITIES = 16
MAX_EVENT_BYTES = 65536
REPLAY = 50  # Recent lifecycle events restated to a reader that has just connected.
EMPTY_METRICS: dict[str, Any] = {
    "tokens": None,
    "cpuPercent": None,
    "rssBytes": None,
    "children": None,
}

# Required lifecycle fields and the bounded optional text worth preserving in a marker.
_MARKER_FIELDS = {
    "RUN_STARTED": {"threadId": 128, "runId": 128, "protocolVersion": 16},
    "RUN_FINISHED": {"threadId": 128, "runId": 128},
    "RUN_ERROR": {"message": 240, "code": 64},
    "STEP_STARTED": {"stepName": 128},
    "STEP_FINISHED": {"stepName": 128},
    "SUBAGENT_STARTED": {"subagentRunId": 1024, "name": 120, "description": 200},
    "SUBAGENT_FINISHED": {"subagentRunId": 1024},
    "SUBAGENT_ERROR": {"subagentRunId": 1024, "message": 240, "code": 64},
}


def _encode_event(event: model.Event) -> bytes | None:
    """Encode a retainable event, or refuse its size/representation before projection."""
    try:
        encoded = json.dumps(event, ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        return None
    return encoded if len(encoded) <= MAX_EVENT_BYTES else None


def _state_batches(event: model.Event) -> list[model.Event] | None:
    """Split a large atomic patch into bounded events; no individual operation is cut."""
    batches: list[model.Event] = []
    batch: list[Operation] = []
    overhead = len(json.dumps({**event, "delta": []}).encode())
    size = overhead
    for operation in event["delta"]:
        try:
            encoded = json.dumps(operation, ensure_ascii=True, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            return None
        if overhead + len(encoded) > MAX_EVENT_BYTES:
            return None
        extra = len(encoded) + (2 if batch else 0)  # JSON's comma and space.
        if size + extra > MAX_EVENT_BYTES:
            batches.append({**event, "delta": batch})
            batch, size = [], overhead
            extra = len(encoded)
        batch.append(json.loads(encoded))
        size += extra
    if batch:
        batches.append({**event, "delta": batch})
    return batches


def _patch_digest(operations: list[Operation]) -> bytes:
    """Fingerprint patch content, excluding timestamps and retaining no rejected values."""
    digest = hashlib.sha256()
    # NaN/Infinity are invalid for admission but must still have repeatable fingerprints.
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True)
    try:
        for part in encoder.iterencode(operations):
            digest.update(part.encode())
    except (TypeError, ValueError, RecursionError):
        # Non-JSON objects have no portable content representation. Remember this
        # submission by identity; the hub still retains only the fixed-size digest.
        digest.update(f"unencodable:{id(operations)}".encode())
    return digest.digest()


def _oversized_marker(event: model.Event) -> model.Event:
    """Preserve bounded lifecycle identity without retaining the oversized payload."""
    kind = model.bounded(event.get("type"), 64)
    marker = model.custom("answerback.oversized", {"originalType": kind})
    if kind in _MARKER_FIELDS:
        marker = {
            "type": kind,
            "timestamp": model.now_ms(),
            **{
                key: model.bounded(event[key], limit)
                for key, limit in _MARKER_FIELDS[kind].items()
                if key in event
            },
        }
        # Omitting a cancelled/interrupted outcome would falsely imply success.
        outcome = event.get("outcome")
        if isinstance(outcome, dict):
            try:
                encoded_outcome = json.dumps(outcome, ensure_ascii=True, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                encoded_outcome = ""
            outcome_type = outcome.get("type")
            if encoded_outcome and len(encoded_outcome) <= 4096:
                marker["outcome"] = json.loads(encoded_outcome)
            elif outcome_type in ("cancelled", "success", "suspended"):
                marker["outcome"] = {"type": outcome_type}
            elif outcome_type == "interrupt":
                marker["outcome"] = model.interrupt_outcome(
                    "oversized", "event_oversized", "Event exceeded the history size limit"
                )
    metadata = event.get("metadata")
    tags = metadata.get(model.NAMESPACE) if isinstance(metadata, dict) else None
    marker["metadata"] = {
        model.NAMESPACE: {
            "oversized": True,
            **(
                {
                    key: model.bounded(tags[key], limit)
                    for key, limit in (("runId", 128), ("threadId", 128), ("peerId", 1024))
                    if isinstance(tags.get(key), str)
                }
                if isinstance(tags, dict)
                else {}
            ),
        }
    }
    stamp = event.get("timestamp")
    if type(stamp) is int and abs(stamp) <= 9007199254740991:
        marker["timestamp"] = stamp
    return marker


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "bridge": {},
        "runs": {},
        "peers": {},
        "decisions": {},
        "applyQueue": {},
        "heads": {},
        # The live activities, by message ID. An activity that is not listed here has ended:
        # a reader drops it, so what a reader holds is bounded by what the hub holds.
        "activities": {},
    }


class EventHub:
    def __init__(
        self,
        *,
        history: int = 2000,
        receipt_path: Path | None = None,
        debug: DebugController | None = None,
    ) -> None:
        if type(history) is not int or not 16 <= history <= 100_000:
            raise ValueError("Event history must retain 16–100000 events")
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.ring: deque[tuple[int, model.Event]] = deque(maxlen=history)
        self.state = empty_state()
        self.activities: dict[str, tuple[str, dict[str, Any]]] = {}
        self.last_read = 0.0
        self.lock = threading.Lock()
        self.receipt_path = receipt_path
        self.debug = debug
        self.resync_seq = 0
        self.omitted_updates = 0
        self._last_refused_patch: bytes | None = None

    def _record(self, event: model.Event) -> str:
        """Fan out the same projected event to history and the optional debug drawer."""
        self.seq += 1
        encoded = _encode_event(event)
        if encoded is None:
            kind = event.get("type")
            if isinstance(kind, str) and kind.startswith(("STATE_", "ACTIVITY_")):
                self.resync_seq = self.seq
            event = _oversized_marker(event)
        else:
            # Detach producer-owned containers so later mutations cannot bypass admission.
            event = json.loads(encoded)
        self.ring.append((self.seq, event))
        ident = self.ident(self.seq)
        if self.debug is not None:
            self.debug.record(ident, event, self.state, self.activities)
        return ident

    def ident(self, seq: int) -> str:
        return f"{self.epoch}-{seq}"

    def publish(self, event: model.Event) -> str:
        with self.lock:
            return self._record(event)

    def set_section(self, key: str, value: Any) -> list[Operation]:
        """Replace one top-level part of the state; the difference goes out as a delta."""
        with self.lock:
            operations = diff(self.state.get(key), value, "/" + key)
            if key in ("applyQueue", "heads"):
                expanded: list[Operation] = []
                for operation in operations:
                    if operation["op"] == "add" and isinstance(operation["value"], dict):
                        # A new project may hold many buckets/worktrees. Introduce its
                        # parent first, then admit each exact child key in its own op.
                        expanded.append({**operation, "value": {}})
                        expanded.extend(diff({}, operation["value"], operation["path"]))
                    else:
                        expanded.append(operation)
                operations = expanded
            return self._patch(operations)

    def patch_state(self, operations: list[Operation]) -> list[Operation]:
        """Apply and publish a JSON Patch. T-17 writes `/peers/<id>/metrics` through here."""
        if not operations:
            return []
        with self.lock:
            return self._patch(operations)

    def _patch(self, operations: list[Operation]) -> list[Operation]:
        """With the lock held: admit the whole patch before changing reconnect state."""
        if not operations:
            return []
        event = model.state_delta(operations)
        encoded = _encode_event(event)
        batches = [json.loads(encoded)] if encoded is not None else _state_batches(event)
        if batches is None:
            fingerprint = _patch_digest(operations)
            if fingerprint != self._last_refused_patch:
                self._last_refused_patch = fingerprint
                self.omitted_updates += 1
                self._record(event)
            return []
        accepted = [operation for batch in batches for operation in batch["delta"]]
        # Validate the complete patch before either state or history changes.
        self.state = apply(self.state, accepted)
        for batch in batches:
            self._record(batch)
        return accepted

    def _list_activities(self) -> None:
        """With the lock held: restate which activities are live, as a state delta."""
        listed = {ident: {"activityType": kind} for ident, (kind, _) in self.activities.items()}
        self._patch(diff(self.state["activities"], listed, "/activities"))

    def set_activity(self, message_id: str, activity_type: str, content: dict[str, Any]) -> None:
        with self.lock:
            snapshot = model.activity_snapshot(message_id, activity_type, content)
            encoded = _encode_event(snapshot)
            if encoded is None:
                self.omitted_updates += 1
                self._record(snapshot)
                return
            snapshot = json.loads(encoded)
            content = snapshot["content"]
            current = self.activities.get(message_id)
            if current is None or current[0] != activity_type:
                if current is None and len(self.activities) >= MAX_ACTIVITIES:
                    self.activities.pop(next(iter(self.activities)))
                event = snapshot
            else:
                changes = diff(current[1], content)
                if not changes:
                    return
                event = model.activity_delta(message_id, activity_type, changes)
                if _encode_event(event) is None:
                    # The admitted complete content may be smaller than its diff.
                    event = snapshot
            self.activities[message_id] = (activity_type, copy.deepcopy(content))
            # The listing goes first: a reader keeps content only for a listed activity, and
            # the same delta removes one that was evicted to make room.
            self._list_activities()
            self._record(event)

    def drop_activity(self, message_id: str) -> None:
        with self.lock:
            if self.activities.pop(message_id, None) is not None:
                self._list_activities()

    def demand(self, window: float) -> bool:
        """Whether a reader was seen recently; costly sources idle without one."""
        return time.monotonic() - self.last_read <= window

    def read(
        self, after: str | None = None, limit: int = 200, run: str | None = None
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event page size must be 1–1000")
        if run is not None and (not isinstance(run, str) or not 1 <= len(run) <= 128):
            raise ValueError("Run filter must be a run ID")
        # Read outside the hub lock. A receipt outside the bounded working set is still a
        # known run; missing ring history must not turn it into an unknown future run.
        stored = False
        if run is not None and self.receipt_path is not None:
            from .history import receipt_exists

            stored = receipt_exists(self.receipt_path, run)
        position: int | None = None
        if after is not None:
            match = EVENT_ID.fullmatch(after) if isinstance(after, str) else None
            if match is None:
                raise ValueError("Event cursor must be an ID from this stream")
            # A part ID is from an unfinished connect page: the page is sent again, whole.
            if match.group(1) == self.epoch and match.group(3) is None:
                position = int(match.group(2))
        with self.lock:
            self.last_read = time.monotonic()
            floor = self.ring[0][0] if self.ring else self.seq + 1
            stale = position is None or position > self.seq or position < floor - 1
            if run is None and position is not None and position < self.resync_seq:
                stale = True  # A dropped state/activity delta requires its current snapshot.
            if run is not None:
                # One run's lifecycle, with no board state: this view is a single AG-UI run,
                # which must open with RUN_STARTED. When the history no longer holds that
                # event, the view is refused as incomplete; a suffix is never passed off as
                # the run.
                start = 0 if stale or position is None else position
                selected = [
                    (seq, event)
                    for seq, event in self.ring
                    if seq > start and event["type"] in model.LIFECYCLE
                    if model.run_id_of(event) == run
                ][:limit]
                if start == 0 and (
                    (selected and selected[0][1]["type"] != "RUN_STARTED")
                    or (not selected and (stored or run in self.state["runs"]))
                ):
                    return {
                        "events": [],
                        "cursor": None,
                        "resync": False,
                        "incomplete": True,
                    }
                events = [{"id": self.ident(seq), "event": event} for seq, event in selected]
                cursor = self.ident(selected[-1][0] if len(selected) == limit else self.seq)
                return {"events": events, "cursor": cursor, "resync": False}
            if stale:
                # The connect page: the state, the live activities, then recent lifecycle
                # events restated. It is one unit. Only its last event carries the stream
                # position; the others carry a part ID, which resumes nowhere. A reader that
                # drops before the last event is therefore sent the whole page again, and
                # one that saw the last event resumes after it and never re-applies a delta.
                now = self.ident(self.seq)
                recent = [
                    event
                    for _, event in self.ring
                    if event["type"] in model.LIFECYCLE or event["type"] == "CUSTOM"
                ][-REPLAY:]
                snapshot = model.state_snapshot(copy.deepcopy(self.state))
                if self.omitted_updates:
                    snapshot["metadata"] = {
                        model.NAMESPACE: {"omittedUpdates": self.omitted_updates}
                    }
                page = [
                    snapshot,
                    *(
                        model.activity_snapshot(ident, kind, copy.deepcopy(content))
                        for ident, (kind, content) in self.activities.items()
                    ),
                    *(model.replayed(event) for event in recent),
                ]
                events = [
                    {"id": now if part == len(page) - 1 else f"{now}.{part}", "event": event}
                    for part, event in enumerate(page)
                ]
                return {"events": events, "cursor": now, "resync": True}
            assert position is not None
            selected = [(seq, event) for seq, event in self.ring if seq > position][:limit]
            events = [{"id": self.ident(seq), "event": event} for seq, event in selected]
            cursor = self.ident(selected[-1][0]) if selected else self.ident(position)
            return {"events": events, "cursor": cursor, "resync": False}
