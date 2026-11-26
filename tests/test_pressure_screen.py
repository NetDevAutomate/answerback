"""T-227 hc screen matcher: a rate-limit line on a worker's screen is one signal, by class."""

import json
from pathlib import Path
from typing import Any

from events_helpers import Script, collector, peer, receipt

from answerback_bridge.events.alerts import alert_event
from answerback_bridge.events.board import Board
from answerback_bridge.events.collector import Collector
from answerback_bridge.pressure import PressureStore, stream_listener

IDENT = "hc:t1"
RUN = "5b0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44"
LIMITER = (
    "You're sending requests faster than your rate limit allows. Please wait a moment "
    "and try again, or run fewer sessions at the same time."
)
SECOND = "Error: You're sending requests faster than your rate limit allows (again)."


def observed(tmp_path: Path, brief: str = "Do the work. Task T-227.") -> tuple[Script, Collector]:
    script = Script()
    script.rows = [receipt("submitted", 101.0, run=RUN, peer_id=IDENT)]
    script.scan = {"peers": [peer("working", ident=IDENT, agent_kind="kiro")], "complete": True}
    observer = collector(script, full=True)
    store = PressureStore(tmp_path / "pressure.sqlite")
    store.subscribe(stream_listener(observer.hub, store))
    observer.sources.pressure = store
    briefs: list[str] = []

    async def read_brief(run: str) -> str | None:
        briefs.append(run)
        return brief if run == RUN else None

    observer.sources.brief = read_brief
    observer.briefs_read = briefs  # type: ignore[attr-defined]
    return script, observer


def throttled(observer: Collector) -> list[dict[str, Any]]:
    return [
        event
        for _, event in observer.hub.ring
        if event["type"] == "CUSTOM" and event["name"] == "answerback.provider_throttled"
    ]


def assert_no_text(observer: Collector, *lines: str) -> None:
    published = json.dumps([event for _, event in observer.hub.ring]) + json.dumps(
        observer.hub.state
    )
    for line in lines:
        for fragment in (line, "faster than your rate limit", "fewer sessions"):
            assert fragment not in published


async def test_a_limiter_line_is_one_signal_and_no_text_leaves(tmp_path: Path) -> None:
    script, observer = observed(tmp_path)
    script.tails[IDENT] = f"$ kiro-cli chat\nworking on it\n{LIMITER}\n> "
    await observer.tick()
    (event,) = throttled(observer)
    value = event["value"]
    assert {k: value[k] for k in ("provider", "account_hint", "harness", "source")} == {
        "provider": "kiro",
        "account_hint": "default",
        "harness": "kiro",
        "source": "hc_screen",
    }
    assert value["signature"] == "kiro.rate_limit" and value["level"] == 1
    assert set(value) == {
        "provider",
        "account_hint",
        "model",
        "harness",
        "source",
        "signature",
        "observed_at",
        "level",
        "until",
    }
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 1
    assert alert_event(event)
    # The same screen on the next tick is the same line: no second signal.
    await observer.tick()
    assert len(throttled(observer)) == 1
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 1
    # A changed line with the signature is a new signal while held: level 2.
    script.tails[IDENT] += f"\n{SECOND}"
    await observer.tick()
    assert [e["value"]["level"] for e in throttled(observer)] == [1, 2]
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 2
    assert_no_text(observer, LIMITER, SECOND)
    for _, event in observer.hub.ring:
        assert "digest" not in json.dumps(event)


async def test_a_brief_that_quotes_the_signature_is_not_a_signal(tmp_path: Path) -> None:
    brief = f"Routed: design-impl\nThe Kiro limiter prints:\n  {LIMITER}\nDetect it."
    script, observer = observed(tmp_path, brief=brief)
    # The worker's screen shows the assignment it was given.
    script.tails[IDENT] = f"> {brief.splitlines()[0]}\n{LIMITER}\nReading the code..."
    await observer.tick()
    await observer.tick()
    assert throttled(observer) == []
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 0
    assert observer.briefs_read == [RUN]  # type: ignore[attr-defined]  # Once, not per tick.


async def test_a_second_copy_of_the_same_message_is_a_second_signal(tmp_path: Path) -> None:
    script, observer = observed(tmp_path)
    script.tails[IDENT] = LIMITER
    await observer.tick()
    script.tails[IDENT] = f"{LIMITER}\nretrying\n{LIMITER}"
    await observer.tick()
    assert [e["value"]["level"] for e in throttled(observer)] == [1, 2]


async def test_only_terminal_screens_are_matched(tmp_path: Path) -> None:
    script, observer = observed(tmp_path)
    crew = "crew:chat-1"
    script.rows = [receipt("submitted", 101.0, run=RUN, peer_id=crew)]
    script.scan = {"peers": [peer("working", ident=crew, agent_kind="kirocrew")]}
    script.tails[crew] = LIMITER
    await observer.tick()
    assert throttled(observer) == []


async def test_claude_screen_bedrock_throttling_is_the_bedrock_account(tmp_path: Path) -> None:
    script, observer = observed(tmp_path)
    script.scan = {"peers": [peer("working", ident=IDENT, agent_kind="claude")]}
    script.tails[IDENT] = "  ⎿  API Error (ThrottlingException): Too many tokens, please wait"
    await observer.tick()
    (event,) = throttled(observer)
    assert (event["value"]["provider"], event["value"]["harness"]) == ("bedrock", "claude")
    assert observer.hub.state["pressure"]["bedrock:default"]["level"] == 1


async def test_remembered_lines_go_with_their_run_and_the_board_shows_pressure(
    tmp_path: Path,
) -> None:
    script, observer = observed(tmp_path)
    script.tails[IDENT] = LIMITER
    await observer.tick()
    assert observer.pressure_seen
    script.rows = [receipt("completed", 102.0, run=RUN, peer_id=IDENT, result_chars=1)]
    script.scan = {"peers": [peer("idle", ident=IDENT, agent_kind="kiro")]}
    observer.settings = observer.settings.__class__(
        tracing=observer.settings.tracing.__class__(enabled=False)
    )
    await observer.tick()
    assert not observer.pressure_seen
    board = Board()
    for item in observer.hub.read()["events"]:
        board.apply(item["id"], item["event"])
    text = board.render()
    assert "PRESSURE" in text and "kiro:default" in text
    assert "kiro:default throttled (kiro.rate_limit on kiro hc_screen); level 1" in text
    assert "faster than" not in text


async def test_the_slow_tick_decays_and_restates_the_section(tmp_path: Path) -> None:
    script, observer = observed(tmp_path)
    clock = {"mono": 0.0, "wall": 1_800_000_000.0}
    store = PressureStore(
        tmp_path / "decay.sqlite", clock=lambda: clock["mono"], wall=lambda: clock["wall"]
    )
    store.subscribe(stream_listener(observer.hub, store))
    observer.sources.pressure = store
    script.tails[IDENT] = LIMITER
    await observer.tick()
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 1
    clock["mono"] += 600
    clock["wall"] += 600
    await observer.tick_slow()
    assert observer.hub.state["pressure"]["kiro:default"]["level"] == 0
    assert observer.hub.state["pressure"]["kiro:default"]["until"] is None
    assert len(throttled(observer)) == 1


async def test_every_pressure_event_and_snapshot_is_schema_valid(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator
    from test_events_schema import SCHEMA_DIR

    validator = Draft202012Validator(json.loads((SCHEMA_DIR / "ag-ui-1.0.schema.json").read_text()))
    script, observer = observed(tmp_path)
    script.tails[IDENT] = f"{LIMITER}\n{SECOND}"
    await observer.tick()
    events = [item["event"] for item in observer.hub.read()["events"]]
    events += [event for _, event in observer.hub.ring]
    assert {"CUSTOM", "STATE_DELTA", "STATE_SNAPSHOT"} <= {event["type"] for event in events}
    for event in events:
        assert not list(validator.iter_errors(event)), event["type"]
