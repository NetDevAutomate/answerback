import json
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

import pytest
from test_bus_api import configured
from test_bus_monitor import populate


def request(url: str, *, token: str | None = None, headers: dict | None = None, method="GET"):
    values = dict(headers or {})
    if token is not None:
        values["Authorization"] = f"Bearer {token}"
    return urlopen(Request(url, headers=values, method=method), timeout=5)


@contextmanager
def served(
    monkeypatch: pytest.MonkeyPatch, path: Path, *, open_browser: bool = True
) -> Iterator[tuple[Any, list[str]]]:
    """`serve_dashboard` on a thread: the live dashboard, and what the browser launcher got.

    The launcher is a recorder, so no browser starts."""
    from answerback_bridge.bus import web

    created: list[Any] = []
    opened: list[str] = []
    serving = threading.Event()
    launched = threading.Event()

    class Recording(web.Dashboard):
        def __enter__(self) -> "Recording":
            super().__enter__()
            created.append(self)
            serving.set()
            return self

    def launcher(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        launched.set()
        return True

    monkeypatch.setattr(web, "Dashboard", Recording)
    monkeypatch.setattr(web.webbrowser, "open", launcher)
    thread = threading.Thread(
        target=web.serve_dashboard, args=(path,), kwargs={"open_browser": open_browser}, daemon=True
    )
    thread.start()
    try:
        assert serving.wait(10)
        if open_browser:
            assert launched.wait(10)
        yield created[0], opened
    finally:
        if created:
            created[0].server.shutdown()
        thread.join(10)
        assert not thread.is_alive()


def launch_ticket(opened: str) -> tuple[Path, str]:
    """The page the launcher was pointed at, and the one ticket it carries."""
    address = urlsplit(opened)
    assert address.scheme == "file" and not address.query and not address.fragment
    page = Path(unquote(address.path))
    (ticket,) = set(re.findall(r"#ticket=([A-Za-z0-9_-]+)", page.read_text()))
    return page, ticket


def session(base_url: str, ticket: str | None, **kwargs: Any):
    headers = {} if ticket is None else {"Authorization": f"Ticket {ticket}"}
    return request(
        base_url + "/api/session", headers={**headers, **kwargs.pop("headers", {})}, **kwargs
    )


def test_dashboard_requires_local_authority_and_session_token(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.web import Dashboard

    path = configured(tmp_path)
    populate(path)
    with Dashboard(path) as dashboard:
        assert dashboard.base_url.startswith("http://127.0.0.1:")
        assert dashboard.token in dashboard.url.split("#")[1]
        with request(dashboard.base_url + "/") as response:
            html = response.read().decode()
            assert "Answerback" in html
            assert dashboard.token not in html
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        with pytest.raises(HTTPError) as unauthorised:
            request(dashboard.base_url + "/api/frame")
        assert unauthorised.value.code == 401
        with request(dashboard.base_url + "/api/frame?after=0", token=dashboard.token) as response:
            body = response.read().decode()
            frame = json.loads(body)
            assert frame["stats"]["commands"] == 1
            assert "PROMPT_PRIVATE" not in body and "RESULT_PRIVATE" not in body
            assert response.headers["Cache-Control"] == "no-store"
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}):
            with pytest.raises(HTTPError) as denied:
                request(dashboard.base_url + "/api/frame", token=dashboard.token, headers=headers)
            assert denied.value.code == 403


def test_dashboard_rejects_writes_traversal_and_invalid_cursors(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.web import Dashboard

    with Dashboard(configured(tmp_path)) as dashboard:
        for method, path, code in (
            ("POST", "/api/frame", 405),
            ("GET", "/../config.json", 404),
            ("GET", "/api/frame?after=-1", 400),
            ("GET", "/api/frame?after=1&after=2", 400),
            ("GET", "/api/frame?after=9223372036854775808", 400),
        ):
            with pytest.raises(HTTPError) as failure:
                request(dashboard.base_url + path, token=dashboard.token, method=method)
            assert failure.value.code == code


def test_dashboard_browser_live_events_controls_and_mobile_layout(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    from codex_herdr_bridge.bus.web import Dashboard

    path = configured(tmp_path)
    api = populate(path)
    with Dashboard(path) as dashboard, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(dashboard.url)
            expect(page.get_by_role("heading", name="Answerback", exact=True)).to_be_visible()
            expect(page.get_by_test_id("commands-count")).to_have_text("1")
            expect(page.get_by_test_id("event-row")).to_have_count(3)
            assert page.url == dashboard.base_url + "/"
            page.get_by_role("button", name="Debug").click()
            expect(page.locator("#diagnostics")).to_contain_text("ledger_available")
            expect(page.get_by_test_id("debug-pane")).to_be_visible()
            page.get_by_role("button", name="Debug").click()
            expect(page.get_by_test_id("debug-pane")).to_be_hidden()
            page.get_by_label("Event filter").select_option("task")
            expect(page.get_by_test_id("event-row")).to_have_count(1)
            page.get_by_label("Event filter").select_option("all")
            page.get_by_role("button", name="Pause").click()
            live = api.submit("kiro:worker", str(tmp_path), "PRIVATE_LIVE_PROMPT", "live")
            page.wait_for_timeout(1200)
            expect(page.get_by_test_id("commands-count")).to_have_text("1")
            page.get_by_role("button", name="Resume").click()
            expect(page.get_by_test_id("commands-count")).to_have_text("2")
            expect(page.locator(f'[data-command-id="{live["command_id"]}"]')).to_be_visible()
            page.get_by_label("Search events").fill("no-such-event")
            expect(page.get_by_text("No events match this filter.")).to_be_visible()
            page.get_by_label("Search events").fill("")
            page.get_by_role("button", name="Debug").click()
            assert "PROMPT_PRIVATE" not in page.locator("body").inner_text()
            assert "RESULT_PRIVATE" not in page.locator("body").inner_text()
            assert not errors
            import os

            if folder := os.environ.get("AGENT_BUS_SCREENSHOT_DIR"):
                output = Path(folder)
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / "monitor-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#diagnostics").scroll_into_view_if_needed()
            expect(page.locator("#diagnostics")).to_contain_text("ledger_available")
            if folder:
                page.screenshot(path=str(output / "monitor-mobile.png"), full_page=True)
        finally:
            browser.close()


def test_browser_empty_reconnect_blocked_and_untrusted_metadata(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    from codex_herdr_bridge.bus.api import BusAPI
    from codex_herdr_bridge.bus.web import Dashboard

    path = configured(tmp_path)
    api = BusAPI(path)
    store = api.store()
    with Dashboard(path) as dashboard, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(dashboard.url)
            expect(page.get_by_text("No events yet", exact=True)).to_be_visible()
            disconnected = [True]

            def transport(route):
                if disconnected[0]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/api/frame*", transport)
            expect(page.get_by_text("Reconnecting", exact=True)).to_be_visible(timeout=7000)
            hostile = '<img src=x onerror="window.INJECTED=true">'
            row = api.submit("kiro:worker", str(tmp_path / hostile), "SECRET", "one")
            store.claim(row["command_id"])
            task = {"id": "task-one", "status": "blocked", "updated": 1, "turn_id": "turn-one"}
            store.bind(row["command_id"], task)
            disconnected[0] = False
            expect(page.locator("#attention-count")).to_have_text("1")
            page.get_by_role("button", name="Inspect event 1", exact=True).click()
            expect(page.locator("#event-detail")).to_contain_text("INJECTED")
            assert json.loads(page.locator("#event-detail").inner_text())["project"] == str(
                tmp_path / hostile
            )
            assert page.evaluate("window.INJECTED") is None
            store.project_task(row["command_id"], {**task, "status": "running", "updated": 2})
            expect(page.locator("#attention-count")).to_have_text("0")
            store.project_task(row["command_id"], {**task, "status": "completed", "updated": 3})
            expect(page.locator("#completed-count")).to_have_text("1")
            # Evict the selected event from the visible ring; inspection must stay exact.
            for i in range(201):
                api.submit("kiro:worker", str(tmp_path), "SECRET", f"extra-{i}")
            expect(page.get_by_test_id("event-row")).to_have_count(200, timeout=10000)
            expect(page.locator("#selected-title")).to_have_text("Event #1")
            expect(page.locator("#event-detail")).to_contain_text("INJECTED")
            assert json.loads(page.locator("#event-detail").inner_text())["project"] == str(
                tmp_path / hostile
            )
        finally:
            browser.close()


def test_serve_dashboard_gives_the_browser_launcher_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T-83, red_at_base. A launcher's arguments are readable by every local account.
    with served(monkeypatch, configured(tmp_path)) as (dashboard, opened):
        assert len(opened) == 1
        assert dashboard.token not in opened[0]
        # The token still reaches the page: through an owner-only file, once.
        page, ticket = launch_ticket(opened[0])
        assert ticket not in opened[0]
        assert stat.S_IMODE(page.stat().st_mode) == 0o600
        assert stat.S_IMODE(page.parent.stat().st_mode) == 0o700
        assert dashboard.token not in page.read_text()
        with session(dashboard.base_url, ticket) as response:
            assert json.loads(response.read()) == {"token": dashboard.token}
            assert response.headers["Cache-Control"] == "no-store"
        with pytest.raises(HTTPError) as spent:
            session(dashboard.base_url, ticket)
        assert spent.value.code == 401
    assert not page.exists() and not page.parent.exists()


def test_serve_dashboard_without_opening_prints_the_url_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # T-83, pins_existing. --no-open: the printed private URL is the way in.
    with served(monkeypatch, configured(tmp_path), open_browser=False) as (dashboard, opened):
        assert opened == []
        assert f"Answerback monitor: {dashboard.url}\n" in capsys.readouterr().out


def test_session_ticket_is_single_use_short_lived_and_behind_the_local_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T-83, red_at_base: the route does not exist at the base.
    from answerback_bridge.bus import web

    def refused(code: int, ticket: str | None, **kwargs: Any) -> None:
        with pytest.raises(HTTPError) as failure:
            session(dashboard.base_url, ticket, **kwargs)
        assert failure.value.code == code
        assert dashboard.token not in failure.value.read().decode()

    with web.Dashboard(configured(tmp_path)) as dashboard:
        # No ticket is issued until a browser is opened: nothing can be redeemed.
        for ticket in (None, "", "guess", dashboard.token):
            refused(401, ticket)
        with pytest.raises(HTTPError) as bearer:
            request(dashboard.base_url + "/api/session", token=dashboard.token)
        assert bearer.value.code == 401

        ticket = dashboard.issue_ticket()
        assert ticket != dashboard.token
        # None of these spends the ticket.
        refused(401, "guess")
        refused(401, ticket + "x")
        refused(403, ticket, headers={"Host": "attacker.example"})
        refused(403, ticket, headers={"Origin": "https://attacker.example"})
        refused(405, ticket, method="POST")
        with session(dashboard.base_url, ticket) as response:
            assert json.loads(response.read()) == {"token": dashboard.token}
        refused(401, ticket)

        # A new ticket replaces the earlier one.
        earlier, latest = dashboard.issue_ticket(), dashboard.issue_ticket()
        refused(401, earlier)
        with session(dashboard.base_url, latest) as response:
            assert json.loads(response.read()) == {"token": dashboard.token}

        monkeypatch.setattr(web, "TICKET_SECONDS", 0)
        refused(401, dashboard.issue_ticket())


def test_browser_opened_from_the_launch_page_never_has_the_token_in_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T-83, red_at_base. The launch page leads to the dashboard; no visited or requested
    # URL carries the token, so it is in no browser history either.
    from playwright.sync_api import expect, sync_playwright

    path = configured(tmp_path)
    populate(path)
    with served(monkeypatch, path) as (dashboard, opened), sync_playwright() as playwright:
        _, ticket = launch_ticket(opened[0])
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            urls: list[str] = []
            errors: list[str] = []
            page.on("framenavigated", lambda frame: urls.append(frame.url))
            page.on("request", lambda sent: urls.append(sent.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(opened[0], wait_until="commit")
            expect(page.get_by_test_id("commands-count")).to_have_text("1")
            expect(page.get_by_test_id("event-row")).to_have_count(3)
            assert page.url == dashboard.base_url + "/"
            # The spent ticket is no way in for another tab; this tab survives a reload.
            page.reload()
            expect(page.get_by_test_id("commands-count")).to_have_text("1")
            other = browser.new_page()
            other.goto(f"{dashboard.base_url}/#ticket={ticket}")
            expect(other.get_by_text("Access needed", exact=True)).to_be_visible()
            assert other.url == dashboard.base_url + "/"
            assert any(url.startswith(dashboard.base_url + "/api/frame") for url in urls)
            assert not [url for url in urls if dashboard.token in url]
            assert not errors
        finally:
            browser.close()
