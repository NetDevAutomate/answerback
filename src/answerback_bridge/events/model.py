"""AG-UI 1.0 events as plain dictionaries: one constructor per type this bridge emits.

Wire names are camelCase because that is the protocol's format. Optional members are omitted,
never null: the schema closes every object and reads null as a value.
"""

import re
import time
from typing import Any

Event = dict[str, Any]
PROTOCOL_VERSION = "1.0"
NAMESPACE = "answerback"
LIFECYCLE = frozenset(
    {
        "RUN_STARTED",
        "RUN_FINISHED",
        "RUN_ERROR",
        "STEP_STARTED",
        "STEP_FINISHED",
        "SUBAGENT_STARTED",
        "SUBAGENT_FINISHED",
        "SUBAGENT_ERROR",
    }
)
_BRIEF_TASK = re.compile(r"\bTask (T-\d{1,4})\b")
_REQUEST_TASK = re.compile(r"(?:^|[-_.:])T-?(\d{1,4})(?=$|[-_.:])")


def now_ms() -> int:
    return int(time.time() * 1000)


def bounded(value: object, limit: int = 240) -> str:
    """Display text only; control characters never reach a terminal or a page."""
    text = value if isinstance(value, str) else ""
    text = "".join(char if char.isprintable() else " " for char in text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def thread_id(message: str | None, request_key: str) -> str:
    """The ledger task a receipt serves: named in the brief, else in the request id."""
    found = _BRIEF_TASK.search(message or "")
    if found:
        return found.group(1)
    found = _REQUEST_TASK.search(request_key)
    return f"T-{int(found.group(1)):02d}" if found else request_key


def _event(kind: str, fields: dict[str, Any], **ids: str | None) -> Event:
    event: Event = {"type": kind, "timestamp": now_ms()}
    event.update({key: value for key, value in fields.items() if value is not None})
    tags = {key: value for key, value in ids.items() if value is not None}
    if tags:
        # RUN_ERROR, STEP_* and SUBAGENT_* carry no run identity of their own, and this
        # stream interleaves runs, so every event names the run it belongs to here.
        event["metadata"] = {NAMESPACE: tags}
    return event


def run_id_of(event: Event) -> str | None:
    tags = event.get("metadata", {}).get(NAMESPACE, {})
    return tags.get("runId") if isinstance(tags, dict) else None


def replayed(event: Event) -> Event:
    """A copy of a past event, marked so a consumer that alerts on events can skip it."""
    tags = dict(event.get("metadata", {}).get(NAMESPACE, {}))
    return {
        **event,
        "metadata": {**event.get("metadata", {}), NAMESPACE: {**tags, "replayed": True}},
    }


def run_started(thread: str, run: str, *, peer_id: str | None = None) -> Event:
    return _event(
        "RUN_STARTED",
        {"threadId": thread, "runId": run, "protocolVersion": PROTOCOL_VERSION},
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def run_finished(
    thread: str,
    run: str,
    *,
    result: Any = None,
    outcome: dict[str, Any] | None = None,
    peer_id: str | None = None,
) -> Event:
    return _event(
        "RUN_FINISHED",
        {"threadId": thread, "runId": run, "result": result, "outcome": outcome},
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def run_error(
    message: str, code: str, *, thread: str, run: str, peer_id: str | None = None
) -> Event:
    return _event(
        "RUN_ERROR",
        {"message": bounded(message) or "Run failed", "code": code},
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def step_started(name: str, *, thread: str, run: str) -> Event:
    return _event("STEP_STARTED", {"stepName": name}, runId=run, threadId=thread)


def step_finished(name: str, *, thread: str, run: str) -> Event:
    return _event("STEP_FINISHED", {"stepName": name}, runId=run, threadId=thread)


def subagent_started(
    subagent_run: str, name: str, *, description: str | None, thread: str, run: str, peer_id: str
) -> Event:
    return _event(
        "SUBAGENT_STARTED",
        {
            "subagentRunId": subagent_run,
            "name": bounded(name, 120) or peer_id,
            "description": bounded(description, 200) or None,
        },
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def subagent_finished(subagent_run: str, *, thread: str, run: str, peer_id: str) -> Event:
    return _event(
        "SUBAGENT_FINISHED",
        {"subagentRunId": subagent_run},
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def subagent_error(
    subagent_run: str, message: str, code: str, *, thread: str, run: str, peer_id: str
) -> Event:
    return _event(
        "SUBAGENT_ERROR",
        {"subagentRunId": subagent_run, "message": bounded(message), "code": code},
        runId=run,
        threadId=thread,
        peerId=peer_id,
    )


def interrupt_outcome(
    ident: str, reason: str, message: str, *, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    interrupt: dict[str, Any] = {"id": ident, "reason": reason, "message": bounded(message)}
    if details:
        interrupt["metadata"] = {NAMESPACE: details}
    return {"type": "interrupt", "interrupts": [interrupt]}


CANCELLED = {"type": "cancelled"}


def state_snapshot(snapshot: Any) -> Event:
    return _event("STATE_SNAPSHOT", {"snapshot": snapshot})


def state_delta(operations: list[dict[str, Any]]) -> Event:
    return _event("STATE_DELTA", {"delta": operations})


def activity_snapshot(message_id: str, activity_type: str, content: dict[str, Any]) -> Event:
    return _event(
        "ACTIVITY_SNAPSHOT",
        {"messageId": message_id, "activityType": activity_type, "content": content},
    )


def activity_delta(message_id: str, activity_type: str, patch: list[dict[str, Any]]) -> Event:
    return _event(
        "ACTIVITY_DELTA",
        {"messageId": message_id, "activityType": activity_type, "patch": patch},
    )


def raw(event: Any, source: str) -> Event:
    return _event("RAW", {"event": event, "source": source})


def custom(name: str, value: Any) -> Event:
    """Extension point for application events; T-17 emits `answerback.anomaly` here."""
    if not name.startswith(NAMESPACE + "."):
        raise ValueError("Custom event names are namespaced: answerback.<name>")
    return _event("CUSTOM", {"name": name, "value": value})
