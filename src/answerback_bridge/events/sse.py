"""Loopback Server-Sent Events over the hub. Read-only: every verb but GET is refused.

The posture is the bus dashboard's, which now shares this code: bound to 127.0.0.1, Host and
Origin must be this listener, and a per-process bearer token travels in the Authorization
header, never in a URL. A stalled reader hits the socket timeout and is dropped; it resumes
with Last-Event-ID.
"""

import asyncio
import hmac
import io
import json
import secrets
import select
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import SplitResult, parse_qs, urlsplit

from ..process import BridgeError
from ..transport import RemoteBridge
from .hub import EVENT_ID, EventHub

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
PAGE = 200
MAX_STREAMS = 4
REQUEST_HEADER_BYTES = 16 * 1024
REQUEST_HEADER_SECONDS = 3.0
SLICE = 1.0  # Longest wait on the source before the reader's socket is looked at again.
INCOMPLETE = (
    "The start of this run is no longer in the retained event history; "
    "its current state is in the full stream"
)


class EventSource(Protocol):
    def read(self, after: str | None, limit: int, wait: float, run: str | None) -> dict[str, Any]:
        """One page after `after`, waiting up to `wait` seconds for the first event."""
        ...

    def runs(self, offset: int, limit: int) -> dict[str, Any]:
        """One bounded page of retained receipt metadata."""
        ...


class LocalSource:
    """A hub in this process: tests and embedders."""

    def __init__(self, hub: EventHub) -> None:
        self.hub = hub

    def read(self, after: str | None, limit: int, wait: float, run: str | None) -> dict[str, Any]:
        deadline = time.monotonic() + wait
        while True:
            page = self.hub.read(after, limit, run)
            if page["events"] or page.get("incomplete") or time.monotonic() >= deadline:
                return page
            time.sleep(0.02)

    def runs(self, offset: int, limit: int) -> dict[str, Any]:
        from .history import read_runs, validate_page

        validate_page(offset, limit)
        if self.hub.receipt_path is not None:
            return read_runs(self.hub.receipt_path, offset, limit)
        # Embedders without a receipt store can expose only their in-memory inventory.
        with self.hub.lock:
            items = sorted(
                self.hub.state["runs"].items(),
                key=lambda item: (item[1].get("createdMs", 0), item[0]),
                reverse=True,
            )
            return {
                "runs": dict(items[offset : offset + limit]),
                "total": len(items),
                "offset": offset,
                "nextOffset": offset + limit if offset + limit < len(items) else None,
                "source": "cache",
            }


class RemoteSource:
    """The running bridge's hub, through its private unix socket."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    async def aread(
        self, after: str | None, limit: int, wait: float, run: str | None
    ) -> dict[str, Any]:
        params = {"after": after, "limit": limit, "wait": wait, "run": run}
        return await RemoteBridge(self.config_path).call("events_read", params)

    def read(self, after: str | None, limit: int, wait: float, run: str | None) -> dict[str, Any]:
        return asyncio.run(self.aread(after, limit, wait, run))

    def runs(self, offset: int, limit: int) -> dict[str, Any]:
        return asyncio.run(
            RemoteBridge(self.config_path).call("events_runs", {"offset": offset, "limit": limit})
        )


def frame(ident: str, event: dict[str, Any]) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"id: {ident}\nevent: {event['type']}\ndata: {data}\n\n".encode()


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Port must be 0–65535")
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(8)
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", port), handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Busy\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "AnswerbackMonitor"
    unauthorised = "A bearer token from this process is required"

    @property
    def local(self) -> LocalHTTPServer:
        return cast(LocalHTTPServer, self.server)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_HEADER_SECONDS)

    def _incomplete_request(self, code: bytes) -> None:
        """Refuse a pre-auth request without invoking the standard HTTP parser."""
        self.close_connection = True
        try:
            self.connection.sendall(b"HTTP/1.0 " + code + b"\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass

    def _read_request_headers(self) -> bytes | None:
        """Read one HTTP header block within fixed byte and wall-clock budgets."""
        deadline = time.monotonic() + REQUEST_HEADER_SECONDS
        received = bytearray()
        while len(received) < REQUEST_HEADER_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._incomplete_request(b"408 Request Timeout")
                return None
            self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(min(4096, REQUEST_HEADER_BYTES - len(received)))
            except TimeoutError:
                self._incomplete_request(b"408 Request Timeout")
                return None
            if not chunk:
                self.close_connection = True
                return None
            received.extend(chunk)
            if b"\r\n\r\n" in received or b"\n\n" in received:
                self.connection.settimeout(3)
                return bytes(received)
        self._incomplete_request(b"431 Request Header Fields Too Large")
        return None

    def handle_one_request(self) -> None:
        """Parse a complete bounded header after enforcing the total pre-auth deadline."""
        headers = self._read_request_headers()
        if headers is None:
            return
        socket_rfile = self.rfile
        self.rfile = io.BytesIO(headers)
        socket_rfile.close()
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self.send_error(501, f"Unsupported method ({self.command!r})")
                return
            method()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        # URLs, headers and event data never enter an access log.
        pass

    def security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")

    def respond(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if code == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.security_headers()
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def error(self, code: int, message: str) -> None:
        self.respond(code, json.dumps({"error": message}).encode())

    def do_POST(self) -> None:
        self.error(405, "The monitor is read-only")

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_HEAD = do_POST
    do_OPTIONS = do_POST

    def local_request(self) -> SplitResult | None:
        """The parsed path of a same-machine, same-origin request; otherwise an error is sent."""
        port = self.local.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self.error(403, "Local host required")
            return None
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in allowed_hosts}:
            self.error(403, "Same-origin requests required")
            return None
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self.error(400, "Invalid request path")
            return None
        return parsed

    def authorised(self) -> bool:
        supplied = self.headers.get("Authorization", "").encode()
        expected = ("Bearer " + self.local.token).encode()
        if hmac.compare_digest(supplied, expected):
            return True
        self.error(401, self.unauthorised)
        return False

    def reader_gone(self) -> bool:
        """A stream reader sends nothing after its request, so a readable socket that
        yields no bytes is one the reader has closed."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def run_page(self, parsed: SplitResult, source: EventSource) -> None:
        """Authenticated GET handler for receipt history, sharing the listener's limits."""
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
            if set(query) - {"offset", "limit"} or any(len(v) != 1 for v in query.values()):
                raise ValueError("Invalid run page query")
            page = source.runs(
                int(query.get("offset", ["0"])[0]), int(query.get("limit", ["100"])[0])
            )
            self.respond(200, json.dumps(page, ensure_ascii=False, allow_nan=False).encode())
        except (ValueError, TypeError):
            self.error(400, "Invalid run page offset or limit")
        except (BridgeError, OSError, sqlite3.Error):
            self.error(503, "Receipt history unavailable")

    def stream_events(self, parsed: SplitResult, source: EventSource, *, wait: float = 10) -> None:
        """Serve the stream until the reader leaves, the source fails or the server stops.

        The source is asked in short slices and the socket is checked between them, so a
        reader that has gone gives its stream slot back within a slice, not a whole wait."""
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
            if set(query) - {"after", "run"} or any(len(value) != 1 for value in query.values()):
                raise ValueError("Invalid event stream query")
            after = self.headers.get("Last-Event-ID") or query.get("after", [None])[0]
            run = query.get("run", [None])[0]
            if after is not None and EVENT_ID.fullmatch(after) is None:
                raise ValueError("Invalid event cursor")
        except ValueError:
            self.error(400, "Invalid event cursor or run filter")
            return
        if not self.local.streams.acquire(blocking=False):
            self.error(503, "Too many event streams; close one and retry")
            return
        try:
            try:
                page = source.read(after, PAGE, 0, run)
            except ValueError:
                self.error(400, "Invalid event cursor or run filter")
                return
            except (BridgeError, OSError):
                self.error(503, "Event stream unavailable; is the bridge running this version?")
                return
            if page.get("incomplete"):
                self.error(410, INCOMPLETE)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.security_headers()
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 2000\n\n")
            quiet = 0.0
            while True:
                for item in page["events"]:
                    self.wfile.write(frame(item["id"], item["event"]))
                if page["events"]:
                    quiet = 0.0
                elif quiet >= wait:
                    self.wfile.write(b": keep-alive\n\n")
                    quiet = 0.0
                self.wfile.flush()
                if self.local.stopping.is_set() or self.reader_gone():
                    return
                pause = min(wait, SLICE)
                page = source.read(page["cursor"], PAGE, pause, run)
                quiet += pause
                if page.get("incomplete"):
                    return  # The reader fell out of the history mid-run; it reconnects.
        except (BridgeError, OSError, ValueError):
            return  # Reader gone or source lost: the client reconnects with its last ID.
        finally:
            self.local.streams.release()


class EventsHandler(LocalHandler):
    unauthorised = "Use the bearer token printed by answerback events --serve"

    def do_GET(self) -> None:
        parsed = self.local_request()
        if parsed is None:
            return
        if parsed.path != "/events":
            self.error(404, "Unknown resource; the stream is at /events")
            return
        if self.authorised():
            server = cast("_EventHTTPServer", self.server)
            self.stream_events(parsed, server.source, wait=server.wait)


class _EventHTTPServer(LocalHTTPServer):
    def __init__(self, source: EventSource, port: int, wait: float) -> None:
        self.source = source
        self.wait = wait
        super().__init__(port, EventsHandler)


class EventServer:
    """`answerback events --serve`: the stream alone, for clients that are not the dashboard."""

    def __init__(self, source: EventSource, *, port: int = 0, wait: float = 10) -> None:
        self.server = _EventHTTPServer(source, port, wait)
        self.token = self.server.token
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/events"

    def __enter__(self) -> "EventServer":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
