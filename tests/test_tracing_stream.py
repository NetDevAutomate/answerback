"""Trace anomalies survive SSE transport and render in both existing boards."""

from pathlib import Path
from typing import Any

from events_helpers import PEER, Script, peer, receipt
from test_events_stream import read_frames, sse
from test_tracing import Clock

from answerback_bridge.events.board import Board
from answerback_bridge.events.collector import Collector
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.sse import EventServer, LocalSource


async def test_stuck_on_sse_and_board_shows_resources_and_the_guard_message() -> None:
    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer()]}
    script.tails = {PEER: "unchanged"}
    sources = script.sources(full=True)

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {PEER: {"cpuPercent": 12, "rssBytes": 1024**3, "children": 3}}

    sources.resources = resources
    observer = Collector(EventHub(), sources, clock=clock)
    board = Board()
    with EventServer(LocalSource(observer.hub), wait=0.05) as server:
        with sse(server.url, server.token) as response:
            ((cursor, initial),) = read_frames(response, 1)
            board.apply(cursor, initial)
            await observer.tick()
            clock.value += 15
            await observer.tick()
            total = observer.hub.seq - int(cursor.split("-")[1])
            frames = read_frames(response, total)
    for ident, event in frames:
        board.apply(ident, event)
    anomaly = next(e for _, e in frames if e.get("name") == "answerback.anomaly")
    assert anomaly["value"]["reason"] == "stuck"
    assert board.state == observer.hub.state
    rendered = board.render()
    assert "stuck" in rendered and "tracing.stuck_seconds" in rendered
    for value in ("CPU %", "RSS MiB", "children", "1024.0"):
        assert value in rendered


def test_alert_filter_only_admits_new_anomalies_and_interrupts() -> None:
    from answerback_bridge.events.alerts import alert_event

    assert alert_event({"type": "CUSTOM", "name": "answerback.anomaly", "value": {}})
    assert alert_event({"type": "RUN_FINISHED", "outcome": {"type": "interrupt"}})
    assert not alert_event({"type": "RUN_FINISHED"})
    assert not alert_event({"type": "CUSTOM", "name": "answerback.other"})
    assert not alert_event(
        {
            "type": "CUSTOM",
            "name": "answerback.anomaly",
            "metadata": {"answerback": {"replayed": True}},
        }
    )


def test_alert_follower_counts_only_alerts_and_advances_past_filtered_events() -> None:
    from answerback_bridge.cli import parser
    from answerback_bridge.events import model
    from answerback_bridge.events.cli import follow

    hub = EventHub()
    cursor = hub.read()["cursor"]
    hub.publish(model.raw({"ignored": True}, "test"))
    hub.publish(model.custom("answerback.anomaly", {"reason": "hung"}))
    assert parser().parse_args(["events", "--alerts-only"]).alerts_only
    events = list(follow(LocalSource(hub), after=cursor, alerts_only=True, count=1))
    assert len(events) == 1 and events[0][1]["value"]["reason"] == "hung"


def test_web_board_renders_tracing_from_sse(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright
    from test_bus_api import configured
    from test_events_bridge import tick_in_thread

    from answerback_bridge.bus.web import Dashboard

    script, clock = Script(), Clock()
    script.rows = [receipt("submitted", 101)]
    script.scan = {"peers": [peer()]}
    script.tails = {PEER: "unchanged"}
    sources = script.sources(full=True)

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {PEER: {"cpuPercent": 12, "rssBytes": 1024**3, "children": 3}}

    sources.resources = resources
    observer = Collector(EventHub(), sources, clock=clock)
    tick_in_thread(observer)
    with Dashboard(configured(tmp_path), source=LocalSource(observer.hub), wait=0.05) as dashboard:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(dashboard.url)
                row = page.get_by_test_id("board-peers-row")
                expect(row).to_contain_text("1024.0")
                assert row.locator("td").nth(6).inner_text() == "—"  # No token source.
                clock.value += 15
                tick_in_thread(observer)
                expect(row).to_contain_text("stuck")
                expect(page.get_by_test_id("board-events")).to_contain_text("tracing.stuck_seconds")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors
            finally:
                browser.close()
