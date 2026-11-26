"""Authenticated loopback dashboard. Every application endpoint is read-only."""

import asyncio
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

from ..events.sse import CSP, EventSource, LocalHandler, LocalHTTPServer, RemoteSource
from .monitor import Monitor

__all__ = ["CSP", "Dashboard", "DashboardHTTPServer", "Handler", "serve_dashboard"]
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
TICKET_SECONDS = 120.0  # How long an opened browser has to load the page and redeem its ticket.
# The page the browser launcher is pointed at. It forwards to the dashboard with a ticket.
LAUNCH_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="0;url={url}">
<title>Answerback monitor</title>
</head>
<body>
<p><a href="{url}">Open the Answerback monitor</a></p>
</body>
</html>
"""


class DashboardHTTPServer(LocalHTTPServer):
    def __init__(self, dashboard: "Dashboard", port: int) -> None:
        self.dashboard = dashboard
        super().__init__(port, Handler)


class Handler(LocalHandler):
    unauthorised = "Open the private dashboard URL printed by agent-bus monitor --web"

    @property
    def dashboard(self) -> "Dashboard":
        return cast(DashboardHTTPServer, self.server).dashboard

    def do_GET(self) -> None:
        parsed = self.local_request()
        if parsed is None:
            return
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            self.respond(
                200,
                Path(__file__).with_name("assets").joinpath(filename).read_bytes(),
                content_type,
            )
            return
        if parsed.path == "/api/session":
            # The one endpoint the ticket opens, and the only one that returns the token.
            scheme, _, ticket = self.headers.get("Authorization", "").partition(" ")
            if scheme != "Ticket" or not self.dashboard.redeem(ticket):
                self.error(401, self.unauthorised)
                return
            self.respond(200, json.dumps({"token": self.dashboard.token}).encode())
            return
        if parsed.path not in ("/api/frame", "/api/events", "/api/runs"):
            self.error(404, "Unknown monitor resource")
            return
        if not self.authorised():
            return
        if parsed.path == "/api/runs":
            self.run_page(parsed, self.dashboard.source)
            return
        if parsed.path == "/api/events":
            # The board: the bridge's AG-UI event stream, relayed as Server-Sent Events.
            self.stream_events(parsed, self.dashboard.source, wait=self.dashboard.wait)
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=3)
            if set(query) - {"after", "limit"} or any(len(value) != 1 for value in query.values()):
                raise ValueError("Invalid monitor query")
            after = int(query["after"][0]) if "after" in query else None
            limit = int(query.get("limit", ["100"])[0])
            frame = asyncio.run(self.dashboard.monitor.snapshot(after=after, limit=limit))
            frame["web"] = {
                "uptime_seconds": round(time.monotonic() - self.dashboard.started, 1),
                "transport": "local lifecycle ledger",
            }
            self.respond(200, json.dumps(frame, ensure_ascii=False, allow_nan=False).encode())
        except (ValueError, TypeError):
            self.error(400, "Invalid cursor or page size")
        except OSError:
            self.error(503, "Monitor temporarily unavailable")


class Dashboard:
    def __init__(
        self,
        config_path: Path,
        *,
        port: int = 0,
        source: EventSource | None = None,
        wait: float = 10,
    ) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Dashboard port must be 0–65535")
        self.monitor = Monitor(config_path)
        self.source = source or RemoteSource(config_path)
        self.wait = wait
        self.started = time.monotonic()
        self.server = DashboardHTTPServer(self, port)
        self.token = self.server.token
        self.ticket: tuple[str, float] | None = None
        self.ticket_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def url(self) -> str:
        # Fragments are not sent in HTTP requests or Referer headers.
        return self.base_url + "/#token=" + self.token

    def issue_ticket(self) -> str:
        """A single-use, short-lived code that `/api/session` trades for the token.

        Unlike the token it may pass through a URL: once spent or expired it opens nothing.
        A new ticket replaces the earlier one."""
        ticket = secrets.token_urlsafe(32)
        with self.ticket_lock:
            self.ticket = (ticket, time.monotonic() + TICKET_SECONDS)
        return ticket

    def redeem(self, supplied: str) -> bool:
        """Spend the ticket. A wrong guess does not spend it; an expired one is dropped."""
        with self.ticket_lock:
            if self.ticket is None:
                return False
            ticket, deadline = self.ticket
            if time.monotonic() >= deadline:
                self.ticket = None
                return False
            if not hmac.compare_digest(supplied.encode(), ticket.encode()):
                return False
            self.ticket = None
            return True

    def __enter__(self) -> "Dashboard":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def serve_dashboard(config_path: Path, *, port: int = 0, open_browser: bool = True) -> None:
    with Dashboard(config_path, port=port) as dashboard:
        print(f"Answerback monitor: {dashboard.url}", flush=True)
        print("Private local view · Ctrl+C stops the dashboard.", flush=True)
        if not open_browser:
            dashboard.thread.join()
            return
        # Every local account can read a browser launcher's arguments, so the launcher gets
        # only the path of an owner-only page. That page carries a ticket, not the token:
        # the token is in no argument list and in no URL the browser visits or remembers.
        with tempfile.TemporaryDirectory(prefix="answerback-monitor-") as folder:
            page = Path(folder) / "open.html"
            address = f"{dashboard.base_url}/#ticket={dashboard.issue_ticket()}"
            descriptor = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(LAUNCH_PAGE.format(url=address))
            webbrowser.open(page.as_uri())
            dashboard.thread.join()
