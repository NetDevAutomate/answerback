"""Every event type the bridge emits validates against the vendored AG-UI 1.0 JSON Schema."""

import hashlib
import json
import re
from importlib import resources
from typing import Any

import pytest
from events_helpers import Script, acceptance_run, collector, peer, receipt
from jsonschema import Draft202012Validator

from answerback_bridge.events import model

SCHEMA_DIR = resources.files("answerback_bridge.events") / "schema"
EMITTED = {
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "STEP_STARTED",
    "STEP_FINISHED",
    "SUBAGENT_STARTED",
    "SUBAGENT_FINISHED",
    "SUBAGENT_ERROR",
    "STATE_SNAPSHOT",
    "STATE_DELTA",
    "ACTIVITY_SNAPSHOT",
    "ACTIVITY_DELTA",
    "RAW",
    "CUSTOM",
}


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / "ag-ui-1.0.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


async def every_event() -> list[dict[str, Any]]:
    """Drive each producer path once: success, failure, cancellation, interrupt, tails, bus."""
    script = Script()
    observer = collector(script, full=True)
    script.tails = {"hc:t61": "building\nrunning tests\n"}
    await acceptance_run(script, observer)
    # A failed run on a second worker, and one abandoned before any worker picked it up.
    script.rows = [
        receipt("submitted", 110.0, run="run-failed", peer_id="hc:t62"),
        receipt("pending", 110.5, run="run-abandoned", peer_id="hc:t63"),
    ]
    script.scan = {"peers": [peer("working", ident="hc:t62")], "complete": True, "errors": {}}
    script.tails = {"hc:t62": "step one\n"}
    await observer.tick()
    script.tails = {"hc:t62": "step one\nstep two\n"}
    script.bus_rows = [{"seq": 7, "type": "task.completed", "payload": {"status": "completed"}}]
    script.board = {
        "decisions": {
            "D-20261117-09": {
                "task": "T-16",
                "class": "reserved",
                "status": "open",
                "title": "Approve the public push?",
                "neededBy": "2026-11-17T10:00:00+00:00",
                "answered": False,
                "overdue": True,
                "project": "/work/answerback",
            }
        },
        "applyQueue": {"/work/answerback": {"pending": ["T-16-events"], "done": []}},
        "heads": {"/work/answerback": {"/work/answerback": {"branch": "main", "sha": "81fc384"}}},
    }
    await observer.tick()
    script.rows = [
        receipt("failed", 111.0, run="run-failed", peer_id="hc:t62", detail="Worker exited"),
        receipt("abandoned", 111.5, run="run-abandoned", peer_id="hc:t63", detail="superseded"),
    ]
    await observer.tick()
    observer.hub.publish(model.custom("answerback.anomaly", {"reason": "stuck", "peerId": "x"}))
    connect = observer.hub.read()["events"]
    history = [event for _, event in observer.hub.ring]
    return [item["event"] for item in connect] + history


async def test_every_emitted_event_type_is_schema_valid(validator: Draft202012Validator) -> None:
    events = await every_event()
    assert {event["type"] for event in events} == EMITTED
    for event in events:
        errors = [error.message for error in validator.iter_errors(event)]
        assert not errors, (event["type"], errors)
    outcomes = {
        event["outcome"]["type"]
        for event in events
        if event["type"] == "RUN_FINISHED" and "outcome" in event
    }
    assert outcomes == {"cancelled", "interrupt"}


def test_the_validator_rejects_what_the_schema_forbids(validator: Draft202012Validator) -> None:
    valid = model.run_started("T-16", "run-1")
    assert validator.is_valid(valid)
    # The schema closes every object and reads null as a value, so these must all fail.
    assert not validator.is_valid({**valid, "unknownField": 1})
    assert not validator.is_valid({"type": "RUN_STARTED", "threadId": "T-16"})
    assert not validator.is_valid(
        {"type": "RUN_FINISHED", "threadId": "a", "runId": "b", "result": None}
    )
    assert not validator.is_valid(
        model.state_delta([{"op": "replace", "path": "no-slash", "value": 1}])
    )
    assert not validator.is_valid(
        model.run_finished("a", "b", outcome={"type": "interrupt", "interrupts": []})
    )


def test_vendored_schema_is_the_published_file_with_its_licence() -> None:
    data = (SCHEMA_DIR / "ag-ui-1.0.schema.json").read_bytes()
    recorded = re.search(r"`([0-9a-f]{64})`", (SCHEMA_DIR / "README.md").read_text())
    assert recorded and hashlib.sha256(data).hexdigest() == recorded.group(1)
    assert json.loads(data)["$id"] == "https://ag-ui.com/spec/1.0/schema.json"
    assert "MIT License" in (SCHEMA_DIR / "LICENSE.ag-ui").read_text()


def test_optional_members_are_omitted_never_null() -> None:
    event = model.subagent_started(
        "s", "worker", description=None, thread="T-1", run="r", peer_id="hc:t1"
    )
    assert "description" not in event and None not in event.values()
    with pytest.raises(ValueError, match="namespaced"):
        model.custom("anomaly", {})
