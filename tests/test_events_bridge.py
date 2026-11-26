"""The stream end to end: bridge socket, CLI, terminal board and the web dashboard."""

import asyncio
import json
import re
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from events_helpers import PEER, Script, acceptance_run, collector, in_order, peer, receipt
from test_events_stream import ACCEPTANCE, read_frames, sse

from answerback_bridge.bus.cli import monitor_board
from answerback_bridge.bus.cli import parser as bus_parser
from answerback_bridge.bus.web import Dashboard
from answerback_bridge.cli import parser
from answerback_bridge.config import Config
from answerback_bridge.events import cli as events_cli
from answerback_bridge.events.collector import Collector, bridge_sources
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.sse import LocalSource, RemoteSource
from answerback_bridge.process import BridgeError
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.transport import READ_METHODS, RemoteBridge


@asynccontextmanager
async def running_bridge() -> AsyncIterator[tuple[Service, Path]]:
    # Unix socket paths are short; pytest's tmp_path can exceed the limit on macOS.
    with tempfile.TemporaryDirectory(prefix="ab-events-", dir="/tmp") as folder:
        path = (await asyncio.to_thread(Path(folder).resolve)) / "config.json"
        config = Config(provider_selection=[])
        config.save(path)
        service = Service(path)
        service.path.parent.mkdir(mode=0o700)
        service.bridge = Bridge(config, path)
        server = await asyncio.start_unix_server(service.handle, path=str(service.path))
        try:
            yield service, path
        finally:
            server.close()
            await server.wait_closed()
            await service.bridge.close()


def observer_for(bridge: Bridge) -> Collector:
    """The live sources, with a scripted worker: the test has no agent terminals."""
    observer = Collector(bridge.events, bridge_sources(bridge), bridge.event_settings)

    async def scan() -> dict[str, Any]:
        return {"peers": [peer("working")], "complete": True, "errors": {}}

    observer.sources.peers = scan
    return observer


async def test_receipt_store_to_sse_through_the_bridge_socket() -> None:
    async with running_bridge() as (service, path):
        bridge = service.bridge
        observer = observer_for(bridge)
        remote = RemoteBridge(path)
        first = await remote.call("events_read", {})
        assert first["resync"] and first["events"][0]["event"]["type"] == "STATE_SNAPSHOT"

        task, _ = bridge.store.reserve(
            "hc:default:t61",
            "Task T-16. PROMPT_PRIVATE build the event stream",
            "ab-20261117-T16-design-t61",
            {"kind": "hc", "peer": {"peer_id": PEER}, "completion": "worker_report"},
        )
        await observer.tick()
        bridge.store.update(task["id"], "submitted")
        await observer.tick()
        bridge.store.update(task["id"], "completed", result="RESULT_PRIVATE all done")
        await observer.tick()

        page = await remote.call("events_read", {"after": first["cursor"]})
        kinds = [item["event"]["type"] for item in page["events"]]
        assert in_order(kinds, ACCEPTANCE)
        started = page["events"][kinds.index("RUN_STARTED")]["event"]
        assert (started["threadId"], started["runId"]) == ("T-16", task["id"])
        body = json.dumps(page)
        assert "PROMPT_PRIVATE" not in body and "RESULT_PRIVATE" not in body

        # The dashboard relays the same stream: socket -> RemoteSource -> SSE.
        def over_http() -> list[str]:
            with Dashboard(path, wait=0.2) as dashboard:
                url = dashboard.base_url + "/api/events"
                with pytest.raises(HTTPError) as denied:
                    sse(url, None)
                assert denied.value.code == 401
                with sse(url, dashboard.token, last=first["cursor"]) as response:
                    frames = read_frames(response, len(page["events"]))
                assert [ident for ident, _ in frames] == [item["id"] for item in page["events"]]
                with sse(url, dashboard.token) as response:
                    assert read_frames(response, 1)[0][1]["type"] == "STATE_SNAPSHOT"
                request = Request(
                    url, headers={"Authorization": f"Bearer {dashboard.token}"}, method="POST"
                )
                with pytest.raises(HTTPError) as refused:
                    urlopen(request, timeout=5)
                assert refused.value.code == 405
                return [event["type"] for _, event in frames]

        assert in_order(await asyncio.to_thread(over_http), ACCEPTANCE)


async def test_the_service_observer_loop_follows_the_live_receipt_store() -> None:
    async with running_bridge() as (service, path):
        (path.parent / "events.json").write_text('{"receipt_seconds": 0.2}')
        service.bridge = Bridge(Config.load(path), path)
        loop = asyncio.create_task(service.run_events())
        remote = RemoteBridge(path)
        try:
            cursor = (await remote.call("events_read", {}))["cursor"]
            task, _ = service.bridge.store.reserve(
                "hc:default:t61", "Task T-16. go", "ab-T16", {"kind": "hc"}
            )
            seen: list[str] = []
            async with asyncio.timeout(10):
                while "RUN_FINISHED" not in seen:
                    page = await remote.call("events_read", {"after": cursor, "wait": 5})
                    cursor = page["cursor"]
                    seen += [item["event"]["type"] for item in page["events"]]
                    if "STEP_STARTED" in seen and "RUN_FINISHED" not in seen:
                        service.bridge.store.update(task["id"], "completed", result="ok")
            assert in_order(seen, ["RUN_STARTED", "STEP_STARTED", "STEP_FINISHED", "RUN_FINISHED"])
            state = service.bridge.events.state
            assert state["runs"][task["id"]]["status"] == "completed"
            assert state["bridge"]["instanceId"] == service.instance_id
            # The receipt itself is exactly what dispatch wrote: observing changed nothing.
            assert service.bridge.store.get(task["id"])["result"] == "ok"
        finally:
            service.stopping.set()
            await asyncio.wait_for(loop, 5)


async def test_stream_readers_never_take_a_dispatch_slot_and_long_poll_wakes() -> None:
    async with running_bridge() as (service, path):
        service.slots = asyncio.Semaphore(0)  # Every dispatch slot is busy.
        remote = RemoteBridge(path)
        async with asyncio.timeout(5):
            cursor = (await remote.call("events_read", {}))["cursor"]
            waiting = asyncio.create_task(remote.call("events_read", {"after": cursor, "wait": 10}))
            await asyncio.sleep(0.3)
            assert not waiting.done()
            service.bridge.events.set_section("runs", {"r": {"status": "pending"}})
            page = await waiting
        assert [item["event"]["type"] for item in page["events"]] == ["STATE_DELTA"]
        empty = await remote.call("events_read", {"after": page["cursor"], "wait": 0})
        assert empty["events"] == []
        for params in ({"wait": 21}, {"wait": -1}, {"after": "nope"}, {"limit": 0}, {"submit": 1}):
            with pytest.raises(BridgeError):
                await remote.call("events_read", params)


async def test_reader_capacity_is_bounded_separately() -> None:
    async with running_bridge() as (service, path):
        remote = RemoteBridge(path)
        cursor = (await remote.call("events_read", {}))["cursor"]
        held = [
            asyncio.create_task(remote.call("events_read", {"after": cursor, "wait": 3}))
            for _ in range(8)
        ]
        await asyncio.sleep(0.3)
        with pytest.raises(BridgeError):
            await remote.call("events_read", {"after": cursor})
        # Dispatch-side reads are unaffected by a full reader pool.
        assert (await remote.call("status", {}))["running"]
        service.bridge.events.set_section("runs", {"r": {}})
        await asyncio.gather(*held)


def test_the_stream_adds_only_read_methods_and_no_way_to_command() -> None:
    added = {name for name in Bridge.METHODS if name.startswith("events")}
    assert added == {"events_read", "events_runs"} and added <= READ_METHODS
    hub = EventHub()
    public = {name for name in dir(hub) if not name.startswith("_")}
    assert not {name for name in public if "submit" in name or "send" in name or "answer" in name}


def test_a_bad_events_file_falls_back_to_defaults_without_stopping_the_bridge(
    tmp_path: Path,
) -> None:
    (tmp_path / "events.json").write_text('{"history": 1}')
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    assert bridge.event_settings.history == 2000 and bridge.event_settings_error == "ValueError"
    observer = Collector(
        bridge.events, Script().sources(), settings_error=bridge.event_settings_error
    )
    assert observer.errors == {"settings": "ValueError"}


async def test_answerback_events_cli_prints_ndjson_and_sse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with running_bridge() as (service, path):
        script = Script()
        observer = Collector(service.bridge.events, script.sources())
        await acceptance_run(script, observer)
        args = parser().parse_args(["--config", str(path), "events", "--count", "1"])
        assert args.command == "events"
        await asyncio.to_thread(events_cli.run_cli, args)
        (line,) = capsys.readouterr().out.splitlines()
        assert json.loads(line)["type"] == "STATE_SNAPSHOT"

        run = next(iter(service.bridge.events.state["runs"]))
        args = parser().parse_args(
            ["--config", str(path), "events", "--run", run, "--sse", "--timeout", "0.3"]
        )
        await asyncio.to_thread(events_cli.run_cli, args)
        out = capsys.readouterr().out
        kinds = [line[7:] for line in out.splitlines() if line.startswith("event: ")]
        assert kinds[0] == "RUN_STARTED" and kinds[-1] == "RUN_FINISHED"
        assert out.count("id: " + service.bridge.events.epoch) == len(kinds)


def test_answerback_events_cli_reports_a_stopped_bridge_and_bad_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    for options in (["--count", "1"], ["--serve"], ["--port", "9"], ["--serve", "--sse"]):
        with pytest.raises(SystemExit) as stopped:
            events_cli.run_cli(parser().parse_args(["--config", str(path), "events", *options]))
        assert stopped.value.code == 1
        assert "error" in json.loads(capsys.readouterr().err)
    source = LocalSource(EventHub())
    for bad in ({"after": "x"}, {"count": 0}, {"timeout": -1.0}):
        with pytest.raises(ValueError):
            list(events_cli.follow(source, **bad))  # type: ignore[arg-type]


async def test_terminal_board_renders_runs_peers_and_decisions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = Script()
    observer = collector(script, full=True)
    script.rows = [receipt("submitted", 101.0)]
    script.scan = {"peers": [peer("working", name="evil\x1b[2Jname")], "errors": {}}
    script.tails = {PEER: "running 2068 tests\n"}
    script.board = {
        "decisions": {
            "D-20261117-05": {
                "task": "T-16",
                "class": "reserved",
                "status": "open",
                "title": "Is a multiplexed stream acceptable?",
                "neededBy": None,
                "answered": False,
                "overdue": False,
                "project": "/work/answerback",
            }
        },
        "applyQueue": {"/work/answerback": {"pending": ["T-16-events"], "done": []}},
        "heads": {
            "/work/answerback": {"/tmp/awb-t16": {"branch": "task-T16-events", "sha": "abc"}}
        },
    }
    await observer.tick()
    args = bus_parser().parse_args(["monitor", "--board", "--once"])
    await asyncio.to_thread(monitor_board, args, LocalSource(observer.hub))
    out = capsys.readouterr().out
    for expected in (
        "RUNS · 1",
        "T-16",
        "submitted",
        "screen changed",
        "PEERS · 1",
        PEER,
        "DECISIONS · 1",
        "D-20261117-05",
        "APPLY QUEUE",
        "T-16-events",
        "task-T16-events",
        "SUBAGENT_STARTED",
    ):
        assert expected in out, expected
    assert "\x1b" not in out and "running 2068 tests" not in out
    # A long path keeps its distinguishing end.
    from answerback_bridge.events.board import _table

    long_path = "/Users/someone/code/personal/tools/herdr_bridge/.claude/worktrees/wf-1"
    cell = _table("T", ("w",), [(long_path,)], 5).splitlines()[-1]
    assert cell.startswith("…") and cell.endswith("/.claude/worktrees/wf-1") and len(cell) == 44


def test_board_options_are_validated(capsys: pytest.CaptureFixture[str]) -> None:
    from answerback_bridge.bus.cli import run_cli

    for options in (
        ["--board", "--json"],
        ["--board", "--after", "3"],
        ["--once"],
        ["--web", "--board"],
    ):
        with pytest.raises(SystemExit) as refused:
            run_cli(bus_parser().parse_args(["monitor", *options]))
        assert refused.value.code == 1
        assert "error" in json.loads(capsys.readouterr().err)


def tick_in_thread(observer: Collector) -> None:
    """Playwright's sync API owns this thread's event loop, so the pass runs beside it."""
    worker = threading.Thread(target=lambda: asyncio.run(observer.tick()))
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()


def test_web_dashboard_renders_the_board_from_the_stream(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright
    from test_bus_api import configured

    script = Script()
    observer = collector(script, full=True)
    script.rows = [receipt("submitted", 101.0)]
    hostile = '<img src=x onerror="document.title=1">'
    script.scan = {"peers": [peer("working", name=hostile)], "errors": {}}
    script.tails = {PEER: "running 2068 tests\n"}
    tick_in_thread(observer)
    source = LocalSource(observer.hub)
    with Dashboard(configured(tmp_path), source=source, wait=0.2) as dashboard:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(dashboard.url)
                expect(page.get_by_test_id("board-status")).to_have_text("LIVE")
                run_row = page.get_by_test_id("board-runs-row")
                expect(run_row).to_have_count(1)
                expect(run_row).to_contain_text("T-16")
                expect(run_row).to_contain_text("submitted")
                # The screen text is digested in the bridge; only its change time is shown.
                expect(run_row).not_to_contain_text("running 2068 tests")
                expect(page.locator("#board-runs tr td:nth-child(6)")).to_have_text(
                    re.compile(r"\d\d:\d\d:\d\d")
                )
                expect(page.get_by_test_id("board-peers-row")).to_contain_text(PEER)
                expect(page.get_by_test_id("board-peers-row")).to_contain_text(hostile)
                assert page.locator("#board img").count() == 0 and page.title() != "1"
                stream = page.get_by_test_id("board-events")
                expect(stream).to_contain_text("RUN_STARTED")
                expect(stream).to_contain_text("SUBAGENT_STARTED")

                # The receipt completes while the page is open: the delta updates the row.
                # It is dated 1970, so once finished it is history: the All view keeps it.
                page.get_by_test_id("board-all").click()
                script.rows = [receipt("completed", 102.0)]
                script.scan = {"peers": [peer("idle", name=hostile)], "errors": {}}
                tick_in_thread(observer)
                expect(run_row).to_contain_text("completed")
                # The run ended, so its activity was withdrawn and the page let it go.
                expect(page.locator("#board-runs tr td:nth-child(6)")).to_have_text("—")
                expect(stream).to_contain_text("RUN_FINISHED")
                expect(page.get_by_test_id("board-peers-row")).to_contain_text("idle")
                expect(page.locator("#board-cursor")).to_contain_text(observer.hub.epoch)
                assert not errors
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()


def test_remote_source_reports_a_stopped_bridge(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    # The stream handlers treat both as "unavailable" (503), never as an empty stream.
    with pytest.raises((BridgeError, OSError)):
        RemoteSource(path).read(None, 1, 0, None)
