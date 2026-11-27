"""Crew's private login exchange and chat API, exercised against a local server."""

import asyncio
import hashlib
import json
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.crew import Crew, CrewIdentityChanged
from codex_herdr_bridge.discovery import SelectionRequired
from codex_herdr_bridge.errors import CrewVersionUnsupported
from codex_herdr_bridge.process import BridgeError
from codex_herdr_bridge.runtime import Service
from codex_herdr_bridge.service import Bridge
from codex_herdr_bridge.tasks import TaskStore
from codex_herdr_bridge.transport import FRAME_LIMIT, RemoteBridge

LINK_TOKEN = "fixture-link-credential"
SESSION_TOKEN = "fixture-session-credential"


async def test_offline_gateway_skips_cli_and_rejoins_when_available(gateway: Any) -> None:
    state, cli, origin = gateway
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        closed_port = reserved.getsockname()[1]
        crew = Crew(str(cli), closed_port)
        with pytest.raises(BridgeError, match="gateway is offline"):
            await crew.peers()
        assert not (cli.parent / "calls.jsonl").exists()
    crew.port = int(urlsplit(origin).port)
    assert (await crew.peers())[0]["peer_id"] == "crew:design"
    assert state.auth_count == 1


def slot(key: str = "design", project: str = "/tmp/design", **changes: Any) -> dict[str, Any]:
    return {
        "key": key,
        "title": "Design review",
        "project": project,
        "agent": "kiro",
        "agent_kind": "template",
        "created": 123.0,
        "row_identity": key,
        "running": False,
        "admission_queued": False,
        "stopping": False,
        "compacting": False,
        "preparing": False,
        "pending_approval": False,
        "waiting_for_input": False,
        "needs_input": False,
        "interrupted": False,
        **changes,
    }


class Gateway:
    def __init__(self) -> None:
        self.slots: Any = [slot()]
        self.requests: list[dict[str, Any]] = []
        self.exchange_redirect: str | None = None
        self.send_redirect: str | None = None
        self.issue_cookie = True
        self.auth_count = 0
        self.request_started = threading.Event()
        self.send_response: Any = {"ok": True, "slot": "design", "mid": "row-1"}
        self.send_status = 200
        self.after_send: dict[str, Any] | None = None
        self.detail: Any = {
            "key": "design",
            "title": "Design review",
            "running": False,
            "admission_queued": False,
            "stopping": False,
            "messages": [],
            "queue": [],
            "total": 0,
            "has_more": False,
            "next_before": None,
        }
        self.project_override: str | None = None
        self.raw_slots: bytes | None = None
        self.raw_detail: bytes | None = None
        self.create_key: str | None = None
        self.slow_slots = False
        self.slow_headers = False
        self.headers_started = threading.Event()
        self.release_headers = threading.Event()
        self.old_slot_on_create = False


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[tuple[Gateway, Path, str]]:
    state = Gateway()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def respond(
            self,
            value: Any,
            status: int = 200,
            headers: dict[str, str] | None = None,
            raw: bytes | None = None,
        ) -> None:
            data = raw if raw is not None else json.dumps(value).encode()
            if state.slow_headers and self.path == "/api/chat/slots":
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    self.wfile.flush()
                    state.headers_started.set()
                    while not state.release_headers.wait(0.02):
                        self.wfile.write(b".")
                        self.wfile.flush()
                    self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, val in (headers or {}).items():
                self.send_header(key, val)
            try:
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                return
            try:
                if state.slow_slots and self.path == "/api/chat/slots":
                    for byte in data[:10]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            state.requests.append(
                {"method": "GET", "path": self.path, "headers": dict(self.headers)}
            )
            state.request_started.set()
            if parsed.path in ("", "/") and parse_qs(parsed.query).get("token") == [LINK_TOKEN]:
                state.auth_count += 1
                headers = {}
                if state.issue_cookie:
                    headers["Set-Cookie"] = (
                        f"mc_token_{self.server.server_port}={SESSION_TOKEN}; HttpOnly; Path=/"
                    )
                if state.exchange_redirect:
                    headers["Location"] = state.exchange_redirect
                    self.respond({}, 302, headers)
                else:
                    self.respond({}, headers=headers)
                return
            if self.headers.get("Cookie") != f"mc_token_{self.server.server_port}={SESSION_TOKEN}":
                self.respond({"error": f"bad cookie {LINK_TOKEN}"}, 401)
                return
            if parsed.path == "/api/chat/slots":
                self.respond(state.slots, raw=state.raw_slots)
            elif parsed.path.startswith("/api/chat/slots/"):
                self.respond(state.detail, raw=state.raw_detail)
            elif parsed.path == "/dashboard":
                self.respond({})
            else:
                self.respond({}, 404)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.requests.append(
                {"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body}
            )
            if self.headers.get("Cookie") != f"mc_token_{self.server.server_port}={SESSION_TOKEN}":
                self.respond({"error": "unauthenticated"}, 401)
                return
            if self.path == "/api/chat?ws=1":
                if state.after_send:
                    state.slots[0].update(state.after_send)
                if state.send_redirect:
                    self.respond({}, 307, {"Location": state.send_redirect})
                else:
                    self.respond(state.send_response, state.send_status)
            elif self.path == "/api/chat/slots":
                key = state.create_key or body["name"]
                if state.old_slot_on_create:
                    self.respond(state.slots[0])
                    return
                created = slot(key, project="", title=body["title"])
                state.slots.append(created)
                self.respond(created)
            elif self.path.endswith("/project"):
                key = self.path.split("/")[-2]
                next(row for row in state.slots if row["key"] == key)["project"] = (
                    state.project_override
                    if state.project_override is not None
                    else body["project"]
                )
                self.respond({"ok": True})
            else:
                self.respond({}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    settings = tmp_path / "issuer.json"
    settings.write_text(json.dumps({"url": f"{origin}?token={LINK_TOKEN}", "version": "0.12.2.4"}))
    cli = tmp_path / "kirocrew"
    cli.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
settings = json.loads(Path({str(settings)!r}).read_text())
with Path({str(tmp_path / "calls.jsonl")!r}).open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if settings.get("failure"):
    print(settings["url"], file=sys.stderr)
    sys.exit(1)
if sys.argv[1:] == ["--version"]:
    print(settings.get("version_output") or "kirocrew " + settings["version"])
elif sys.argv[1:4] == ["token", "--ttl", "1h"]:
    print(settings["url"])
else:
    sys.exit(2)
""")
    cli.chmod(0o700)
    try:
        yield state, cli, origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def settings(cli: Path, **changes: Any) -> None:
    path = cli.parent / "issuer.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | changes))


def posts(gateway: Gateway) -> list[dict[str, Any]]:
    return [request for request in gateway.requests if request["method"] == "POST"]


async def test_exchanges_link_token_for_cookie_and_caches_auth(gateway: Any) -> None:
    state, cli, origin = gateway
    crew = Crew(str(cli), int(urlsplit(origin).port))
    peers, repeated = await asyncio.gather(crew.peers(), crew.peers())
    assert peers == repeated
    expected_binding = hashlib.sha256(
        json.dumps([origin, "design", 123.0, "/tmp/design", "kiro"]).encode()
    ).hexdigest()
    assert peers[0] == {
        "peer_id": "crew:design",
        "provider": "crew",
        "target": "design",
        "name": "Design review",
        "project": "/tmp/design",
        "kind": "interactive",
        "state": "idle",
        "capabilities": ["send", "read", "worker_report"],
        "created": 123.0,
        "agent": "kiro",
        "binding": expected_binding,
        "origin": origin,
    }
    assert crew.origin == origin
    assert state.auth_count == 1
    assert "Cookie" not in state.requests[0]["headers"]
    assert all("token=" not in r["path"] for r in state.requests[1:])
    calls = [json.loads(line) for line in (cli.parent / "calls.jsonl").read_text().splitlines()]
    assert calls == [["--version"], ["token", "--ttl", "1h", "--port", str(urlsplit(origin).port)]]
    assert LINK_TOKEN not in json.dumps(peers)
    assert SESSION_TOKEN not in repr(crew)


@pytest.mark.parametrize(
    "version",
    [
        # Older than the release this client was written for.
        "0.12.1",
        "0.11.9.9",
        "0.12",
        # A suffixed build of exactly the minimum may predate that release.
        "0.12.2-dev",
        "0.12.2rc1",
        "0.12.2+local",
        # Not a version statement.
        "13",
        "0.13.0 extra",
        "1.2.3.4.5.6.7.8.9",
        "unknown",
        "",
    ],
)
async def test_rejects_old_or_unreadable_versions_without_minting_token(
    gateway: Any, version: str
) -> None:
    state, cli, _ = gateway
    settings(cli, version=version)
    crew = Crew(str(cli))
    with pytest.raises(CrewVersionUnsupported, match="Unsupported Crew version"):
        await crew.peers()
    assert not state.requests
    assert (cli.parent / "calls.jsonl").read_text().count("\n") == 1
    assert crew.version_status == "unsupported"


@pytest.mark.parametrize(
    "output",
    [
        # A line over 200 characters is not read at all. Cutting it to 200 would cut
        # "rc1" off and serve a pre-release of the minimum as the tested release.
        "kirocrew" + " " * 186 + "0.12.2rc1",
        "kirocrew" + " " * 190 + "0.13.0.4",
        # Two statements that differ, in either order.
        "kirocrew 0.13.0.4\nkirocrew 0.12.1\n",
        "kirocrew 0.12.1\nkirocrew 0.13.0.4\n",
        # The statement lies beyond the 20 lines that are read.
        "notice\n" * 20 + "kirocrew 0.13.0.4\n",
        # A pre-release of the minimum, written with a trailing zero.
        "kirocrew 0.12.2.0rc1",
    ],
)
async def test_rejects_output_that_does_not_state_one_supported_version(
    gateway: Any, output: str
) -> None:
    state, cli, _ = gateway
    settings(cli, version_output=output)
    crew = Crew(str(cli))
    with pytest.raises(CrewVersionUnsupported, match="Unsupported Crew version"):
        await crew.peers()
    assert not state.requests
    assert (cli.parent / "calls.jsonl").read_text().count("\n") == 1
    assert crew.version_status == "unsupported"


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("0.12.2", "tested"),
        ("0.12.2.4", "tested"),
        ("0.12.3", "untested"),
        ("0.12.20", "untested"),
        ("0.13.0", "untested"),
        ("0.13.0.4", "untested"),
        ("0.13.1", "untested"),
        ("0.14.0.2", "untested"),
        ("1.0.0", "untested"),
        # Forms a frequently released tool can start to use without notice: fewer or
        # more numbers, a date as a number, and build suffixes. A suffixed build is
        # never "tested", because it is not the release that was exercised.
        ("0.13", "untested"),
        ("0.13.0.4.1", "untested"),
        ("0.13.20261121", "untested"),
        ("0.13.0rc1", "untested"),
        ("0.13.1.dev20261121", "untested"),
        ("0.14.0-nightly.5", "untested"),
        ("0.13.0.4+g1a2b3c", "untested"),
        ("0.12.2.4-dev", "untested"),
    ],
)
async def test_serves_the_minimum_version_and_later_and_reports_what_it_saw(
    gateway: Any, version: str, status: str
) -> None:
    # Kiro Crew ships far more often than this bridge: a release that is new to the
    # bridge must keep working, and say that nobody has exercised it yet.
    _, cli, origin = gateway
    settings(cli, version=version)
    crew = Crew(str(cli), int(urlsplit(origin).port))
    assert (crew.version, crew.version_status) == (None, None)
    assert (await crew.peers())[0]["peer_id"] == "crew:design"
    assert (crew.version, crew.version_status) == (version, status)


@pytest.mark.parametrize(
    ("output", "version"),
    [
        ("kirocrew 0.13.0.4", "0.13.0.4"),
        ("0.13.0.4", "0.13.0.4"),
        ("kirocrew, version 0.13.0.4", "0.13.0.4"),
        ("kirocrew version: 0.14.0", "0.14.0"),
        ("Kiro Crew v0.13.0.4", "0.13.0.4"),
        ("kiro-crew 0.13.0.4\r\n", "0.13.0.4"),
        ("kirocrew 0.13.0.4\nUpdate available: run kirocrew update\n", "0.13.0.4"),
        ("A new release is available: 0.99.0\n\nkirocrew 0.13.0.4\n", "0.13.0.4"),
        # The same statement twice is one statement.
        ("kirocrew 0.13.0.4\n0.13.0.4\n", "0.13.0.4"),
        # Compared as numbers, reported as printed.
        ("kirocrew 2026.11.05", "2026.11.05"),
    ],
)
async def test_reads_the_version_from_a_reworded_version_line(
    gateway: Any, output: str, version: str
) -> None:
    # The wording of `--version` is not a contract: a release that adds the word
    # "version", a "v" or an update notice must not take the provider away.
    _, cli, origin = gateway
    settings(cli, version_output=output)
    crew = Crew(str(cli), int(urlsplit(origin).port))
    assert (await crew.peers())[0]["peer_id"] == "crew:design"
    assert (crew.version, crew.version_status) == (version, "untested")


async def test_names_the_version_it_refused(gateway: Any) -> None:
    _, cli, _ = gateway
    settings(cli, version="0.12.1")
    crew = Crew(str(cli))
    with pytest.raises(BridgeError) as caught:
        await crew.peers()
    assert str(caught.value) == (
        "Unsupported Crew version 0.12.1; this adapter requires kirocrew 0.12.2 or later"
    )
    assert (crew.version, crew.version_status) == ("0.12.1", "unsupported")


async def test_version_text_in_an_error_is_bounded_and_printable(gateway: Any) -> None:
    _, cli, _ = gateway
    settings(cli, version="9" * 500 + "\x1b[31m\u202e<script>")
    crew = Crew(str(cli))
    with pytest.raises(BridgeError) as caught:
        await crew.peers()
    text = str(caught.value)
    assert len(text) < 160
    assert text.isprintable()
    assert "<" not in text and "\u202e" not in text
    assert crew.version is None


async def test_a_later_version_with_a_changed_chat_list_still_fails_closed(gateway: Any) -> None:
    # The version is no longer the gate, so the response checks must be: a release that
    # drops the identity fields is refused on its first answer, not trusted.
    state, cli, origin = gateway
    settings(cli, version="0.14.0")
    state.slots = [{"key": "design", "running": False}]
    crew = Crew(str(cli), int(urlsplit(origin).port))
    with pytest.raises(BridgeError, match="incomplete chat identity"):
        await crew.peers()
    assert (crew.version, crew.version_status) == ("0.14.0", "untested")


async def test_a_later_version_with_a_changed_send_receipt_is_uncertain_and_posted_once(
    gateway: Any,
) -> None:
    # The check comes after the POST, so it cannot take the message back: the client
    # reports the delivery as uncertain and does not post a second time.
    state, cli, origin = gateway
    settings(cli, version="0.14.0")
    crew = Crew(str(cli), int(urlsplit(origin).port))
    peer = await crew.peer("design")
    state.send_response = {"ok": True, "slot": "design", "steered": True}
    with pytest.raises(BridgeError, match="uncertain.*Do not resubmit blindly"):
        await crew.submit("design", "Review architecture", str(uuid4()), expected=peer)
    assert [request["path"] for request in posts(state)] == ["/api/chat?ws=1"]


async def test_a_later_version_that_answers_a_creation_with_an_existing_chat_is_refused(
    gateway: Any, tmp_path: Path
) -> None:
    state, cli, origin = gateway
    settings(cli, version="0.14.0")
    state.old_slot_on_create = True
    state.slots[0]["title"] = "Review worker"
    crew = Crew(str(cli), int(urlsplit(origin).port))
    with pytest.raises(BridgeError, match="existing"):
        await crew.create("Review worker", str(tmp_path), "create-id")
    # One creation request, and no project assignment on the chat that was returned.
    assert [request["path"] for request in posts(state)] == ["/api/chat/slots"]
    assert state.slots[0]["project"] == "/tmp/design"


async def test_a_failed_version_command_leaves_no_version_from_an_earlier_login(
    gateway: Any,
) -> None:
    _, cli, origin = gateway
    settings(cli, version="0.13.0.4")
    crew = Crew(str(cli), int(urlsplit(origin).port))
    await crew.peers()
    first = crew.version_checked_at
    assert (crew.version, crew.version_status) == ("0.13.0.4", "untested")
    assert first is not None
    settings(cli, failure=True)
    crew._authenticated_until = 0.0
    with pytest.raises(BridgeError, match="Crew version check failed"):
        await crew.peers()
    assert (crew.version, crew.version_status) == (None, None)
    assert crew.version_checked_at is not None and crew.version_checked_at >= first


async def test_every_version_reading_is_announced_and_a_live_session_reads_nothing(
    gateway: Any,
) -> None:
    _, cli, origin = gateway
    crew = Crew(str(cli), int(urlsplit(origin).port))
    seen: list[tuple[str | None, str | None]] = []
    crew.on_version = lambda: seen.append((crew.version, crew.version_status))
    settings(cli, version="0.13.0.4")
    await crew.peers()
    await crew.peers()
    assert seen == [("0.13.0.4", "untested")]
    # A Crew update is noticed at the next login, whatever triggers it.
    settings(cli, version="0.13.1")
    crew._authenticated_until = 0.0
    await crew.detail("design")
    settings(cli, version="0.12.1")
    crew._authenticated_until = 0.0
    with pytest.raises(CrewVersionUnsupported):
        await crew.peers()
    settings(cli, failure=True)
    with pytest.raises(BridgeError, match="Crew version check failed"):
        await crew.peers()
    assert seen == [
        ("0.13.0.4", "untested"),
        ("0.13.1", "untested"),
        ("0.12.1", "unsupported"),
        (None, None),
    ]


async def test_a_version_below_the_minimum_is_refused_at_the_next_login(gateway: Any) -> None:
    state, cli, origin = gateway
    settings(cli, version="0.13.0.4")
    crew = Crew(str(cli), int(urlsplit(origin).port))
    await crew.peers()
    assert (crew.version, crew.version_status) == ("0.13.0.4", "untested")
    settings(cli, version="0.12.1")
    crew._authenticated_until = 0.0
    logins = state.auth_count
    with pytest.raises(BridgeError, match="Unsupported Crew version 0.12.1"):
        await crew.peers()
    assert state.auth_count == logins
    assert (crew.version, crew.version_status) == ("0.12.1", "unsupported")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234?token=hidden",
        "http://example.org:1234?token=hidden",
        # Dummy userinfo, split so secret scanners do not read it as a credential.
        "http://user:" + "password@127.0.0.1:1234?token=hidden",
        "http://127.0.0.1:1234",
        "http://127.0.0.1:1234?token=one&token=two",
        "http://127.1:1234?token=hidden",
    ],
)
async def test_rejects_unsafe_login_urls_without_connecting(gateway: Any, url: str) -> None:
    state, cli, _ = gateway
    settings(cli, url=url)
    with pytest.raises(BridgeError, match="login") as error:
        await Crew(str(cli)).peers()
    assert url not in str(error.value)
    assert not state.requests


async def test_rejects_selected_port_mismatch(gateway: Any) -> None:
    state, cli, origin = gateway
    port = int(urlsplit(origin).port)
    other_port = 1 if port != 1 else 2
    settings(cli, url=f"http://localhost:{other_port}/?token={LINK_TOKEN}")
    with pytest.raises(BridgeError, match="port"):
        await Crew(str(cli), port).peers()
    assert not state.requests


async def test_accepts_same_origin_login_redirect(gateway: Any) -> None:
    state, cli, _ = gateway
    state.exchange_redirect = "/dashboard"
    assert await Crew(str(cli)).peers()


async def test_refuses_cross_origin_redirect_and_does_not_forward_token(gateway: Any) -> None:
    state, cli, _ = gateway
    state.exchange_redirect = f"http://localhost:1/?token={LINK_TOKEN}"
    with pytest.raises(BridgeError, match="redirect") as error:
        await Crew(str(cli)).peers()
    assert LINK_TOKEN not in str(error.value)
    assert len(state.requests) == 1


async def test_requires_exchanged_session_cookie(gateway: Any) -> None:
    state, cli, _ = gateway
    state.issue_cookie = False
    with pytest.raises(BridgeError, match="session cookie"):
        await Crew(str(cli)).peers()
    assert len(state.requests) == 1


async def test_cli_failure_does_not_disclose_login_url(gateway: Any) -> None:
    _, cli, _ = gateway
    settings(cli, failure=True)
    with pytest.raises(BridgeError) as error:
        await Crew(str(cli)).peers()
    assert LINK_TOKEN not in str(error.value)
    assert error.value.__suppress_context__


async def test_sends_agent_origin_uuid_and_returns_only_acceptance(gateway: Any) -> None:
    state, cli, origin = gateway
    crew = Crew(str(cli))
    peer = await crew.peer("design")
    send_id = str(uuid4())
    receipt = await crew.submit("design", "Review architecture", send_id, expected=peer)
    sent = posts(state)
    assert len(sent) == 1
    assert sent[0]["body"] == {
        "slot": "design",
        "message": "Review architecture",
        "turn_origin": "agent",
        "steer": False,
        "meta": {"sendId": send_id},
    }
    assert sent[0]["headers"]["Origin"] == origin
    assert sent[0]["headers"]["Idempotency-Key"] == send_id
    assert receipt["ok"] is True
    assert receipt["mid"] == "row-1"
    assert receipt["status"] == "accepted"
    assert receipt["identity_guard"] == "observed_before_and_after"
    assert "completed" not in receipt.values()


async def test_preserves_queued_receipt(gateway: Any) -> None:
    state, cli, _ = gateway
    state.send_response = {"ok": True, "queued": True, "queue_id": "queued-1"}
    crew = Crew(str(cli))
    receipt = await crew.submit(
        "design", "Review", str(uuid4()), expected=await crew.peer("design")
    )
    assert receipt["queued"] is True
    assert receipt["queue_id"] == "queued-1"
    assert receipt["status"] == "queued"


@pytest.mark.parametrize(
    "change",
    [{"key": "different"}, {"created": 456.0}, {"project": "/another"}, {"agent": "another"}],
)
async def test_refuses_changed_identity_before_send(gateway: Any, change: Any) -> None:
    state, cli, _ = gateway
    crew = Crew(str(cli))
    peer = await crew.peer("design")
    state.slots[0].update(change)
    with pytest.raises(BridgeError):
        await crew.submit("design", "Review", str(uuid4()), expected=peer)
    assert not posts(state)


async def test_identity_change_after_send_is_uncertain_and_never_retried(gateway: Any) -> None:
    state, cli, _ = gateway
    state.after_send = {"created": 456.0}
    crew = Crew(str(cli))
    with pytest.raises(CrewIdentityChanged):
        await crew.submit("design", "Review", str(uuid4()), expected=await crew.peer("design"))
    assert len(posts(state)) == 1


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        [],
        {"ok": False, "error": LINK_TOKEN},
        {"ok": True, "slot": "different"},
        {"ok": True, "queued": True},
        {"ok": True, "steered": True},
    ],
)
async def test_rejects_malformed_or_wrong_slot_receipts(gateway: Any, receipt: Any) -> None:
    state, cli, _ = gateway
    state.send_response = receipt
    crew = Crew(str(cli))
    with pytest.raises(BridgeError) as error:
        await crew.submit("design", "Review", str(uuid4()), expected=await crew.peer("design"))
    assert LINK_TOKEN not in str(error.value)
    assert len(posts(state)) == 1


async def test_failed_post_is_not_replayed_or_followed(gateway: Any) -> None:
    state, cli, _ = gateway
    state.send_redirect = "/api/chat?ws=1"
    crew = Crew(str(cli))
    with pytest.raises(BridgeError, match="uncertain"):
        await crew.submit("design", "Review", str(uuid4()), expected=await crew.peer("design"))
    assert len(posts(state)) == 1


async def test_detail_is_bounded_read_only_and_redacts_credential_echoes(gateway: Any) -> None:
    state, cli, _ = gateway
    state.detail.update(
        messages=[
            {
                "role": "assistant",
                "content": f"{LINK_TOKEN} {SESSION_TOKEN}",
                "ts": 1.0,
                "meta": {"mid": "row-1"},
            }
        ],
        total=8,
        has_more=True,
        next_before=7,
    )
    result = await Crew(str(cli)).detail("design")
    assert result["coverage"] == {"limit": 200, "returned": 1, "total": 8, "has_more": True}
    assert result["completion"] == "unknown"
    assert result["messages"][0]["meta"]["mid"] == "row-1"
    assert not posts(state)
    assert state.requests[-1]["path"] == "/api/chat/slots/design?limit=200&cursor=1"
    assert LINK_TOKEN not in json.dumps(result)
    assert SESSION_TOKEN not in json.dumps(result)


def large_history(messages: int = 200, content: int = 10_000, meta: int = 120) -> list[dict]:
    """A history the size of a busy chat: ~2.7 MB, ~24k strings once decoded."""
    text = ("a line of ordinary worker output, nothing secret in it at all\n" * 200)[:content]
    return [
        {
            "role": "assistant" if index % 2 else "user",
            "content": text,
            "meta": {f"field{key}": f"value-{key}-{index}" for key in range(meta)},
        }
        for index in range(messages)
    ]


async def test_large_history_with_many_credentials_keeps_status_answering(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-281: redaction of a megabytes-long history, repeated for every held credential, ran
    on the event loop and stalled the service for seconds. The walk path is forced here: one
    credential is echoed and one is not plain, so the text shortcut cannot apply."""
    state, cli, _ = gateway
    history = large_history()
    history[0]["content"] = f"{SESSION_TOKEN} {history[0]['content']}"
    state.detail.update(messages=history, total=len(history))
    crew = Crew(str(cli))
    crew._secrets.update(secrets.token_hex(32) for _ in range(40))
    crew._secrets.add("c2Vzc2lvbj1hYmM=")
    redacting_threads: list[int] = []
    redact_payload = Crew._redact_payload

    def observed(payload: bytes, credentials: tuple[str, ...]) -> Any:
        redacting_threads.append(threading.get_ident())
        return redact_payload(payload, credentials)

    monkeypatch.setattr(Crew, "_redact_payload", staticmethod(observed))
    with tempfile.TemporaryDirectory(prefix="hb-", dir="/tmp") as directory:
        config_path = Path(directory) / "config.json"
        Config(provider_selection=[]).save(config_path)
        service = Service(config_path)
        service.bridge = Bridge(Config.load(config_path), config_path)
        service.path.parent.mkdir(mode=0o700)
        server = await asyncio.start_unix_server(
            service.handle, path=str(service.path), limit=FRAME_LIMIT
        )
        remote = RemoteBridge(config_path)
        latencies: list[float] = []
        try:
            reading = asyncio.create_task(crew.detail("design"))
            while not reading.done():
                started = time.monotonic()
                assert (await remote.call("status", {}))["running"] is True
                latencies.append(time.monotonic() - started)
            result = await reading
        finally:
            server.close()
            await server.wait_closed()
            await service.bridge.close()
    assert len(result["messages"]) == 200
    assert SESSION_TOKEN not in json.dumps(result)
    assert result["messages"][0]["content"] == "[redacted]"
    assert result["messages"][1]["content"] == history[1]["content"]
    assert max(latencies) < 0.5, f"status took {max(latencies):.2f} s during the read"
    # Each history is redacted in a worker thread, never on the loop's thread.
    assert redacting_threads and threading.get_ident() not in redacting_threads


async def test_a_string_holding_a_credential_in_any_spelling_is_replaced_whole(
    gateway: Any,
) -> None:
    """Raw, URL-quoted and in a key: the whole string goes, not only the matched spelling."""
    state, cli, _ = gateway
    credential = 'tok"en=1/2 x'
    state.detail.update(
        messages=[
            {"role": "user", "content": f"raw {credential} quoted {quote(credential, safe='')}"},
            {"role": "user", "content": "clean", "meta": {credential: "as a key"}},
        ],
        total=2,
    )
    crew = Crew(str(cli))
    crew._secrets.add(credential)
    result = await crew.detail("design")
    assert result["messages"][0]["content"] == "[redacted]"
    assert result["messages"][1]["content"] == "clean"
    assert result["messages"][1]["meta"] == {"[redacted]": "as a key"}


async def test_a_credential_echoed_only_in_json_escaped_form_is_redacted(gateway: Any) -> None:
    """The raw text holds `tok\\"en`, never `tok"en`, and no `\\u` or `%`: only the
    plain-credentials condition of the shortcut keeps the decoded tree walked."""
    state, cli, _ = gateway
    credential = 'tok"en'
    state.detail.update(messages=[{"role": "user", "content": f"saw {credential}"}], total=1)
    crew = Crew(str(cli))
    crew._secrets.add(credential)
    result = await crew.detail("design")
    assert result["messages"][0]["content"] == "[redacted]"


@pytest.mark.parametrize(
    ("credential", "echo"),
    [
        pytest.param("tok/en", "tok%2fen", id="lowercase-percent-hex"),
        pytest.param("token", "tok%65n", id="percent-encoded-unreserved"),
        pytest.param("tok/en=1", "see tok%2fen%3D1 here", id="mixed-case-in-a-sentence"),
        pytest.param("tok😀en", "tok%F0%9F%98%80en", id="percent-encoded-utf8-non-bmp"),
        pytest.param("token", "%74%6F%6b%65%6E", id="every-character-encoded"),
    ],
)
async def test_url_encoded_credential_echoes_are_redacted_in_values_and_keys(
    gateway: Any, credential: str, echo: str
) -> None:
    """`quote()` writes uppercase hex and leaves unreserved characters alone, but a gateway
    may echo a credential in any legal encoding (T-281 review). One `unquote` pass of each
    string is tested against the raw credentials, and the whole string is replaced."""
    state, cli, _ = gateway
    assert credential not in echo
    state.detail.update(
        messages=[{"role": "user", "content": echo, "meta": {echo: 1, "kept": "k%20v"}}],
        total=1,
    )
    crew = Crew(str(cli))
    crew._secrets.add(credential)
    result = await crew.detail("design")
    assert result["messages"][0] == {
        "role": "user",
        "content": "[redacted]",
        "meta": {"[redacted]": 1, "kept": "k%20v"},
    }
    assert credential not in json.dumps(result)


def test_overlapping_credentials_and_number_looking_strings() -> None:
    """Static probes from the T-281 review, run against the worker's function directly."""
    credentials = ("abc", "abcdef", "123456")
    payload = json.dumps({"outer": [{"abcdef": "abc"}, {"n": "123456", "m": 123456}]}).encode()
    assert Crew._redact_payload(payload, credentials) == {
        "outer": [{"[redacted]": "[redacted]"}, {"n": "[redacted]", "m": 123456}]
    }


def test_a_non_bmp_credential_spelled_as_a_json_surrogate_pair_is_redacted() -> None:
    """`json.loads` joins `\\\\ud83d\\\\ude00` into one character; the walk then finds it."""
    payload = b'{"value":"tok\\ud83d\\ude00en","other":"tok\\ud83den"}'
    assert Crew._redact_payload(payload, ("tok😀en",)) == {
        "value": "[redacted]",
        "other": "tok\ud83den",
    }


def test_a_response_without_an_echo_or_an_escape_takes_the_shortcut() -> None:
    """Plain credentials, no `\\u`, no `%` and no raw credential: the decoded value comes back
    as parsed. A lone `%` in the text is enough to take the walk."""
    credentials = ("c2Vzc2lvbj1hYmM=", "token")
    payload = json.dumps({"content": "a 100 per cent clean line", "n": 1}).encode()
    assert Crew._redact_payload(payload, credentials) == {
        "content": "a 100 per cent clean line",
        "n": 1,
    }
    payload = json.dumps({"content": "100% clean", "echo": "t%6fken"}).encode()
    assert Crew._redact_payload(payload, credentials) == {
        "content": "100% clean",
        "echo": "[redacted]",
    }


async def test_unicode_escaped_credential_echo_is_redacted(gateway: Any) -> None:
    """A `\\u` escape can spell a plain character, so a text that holds one never takes the
    shortcut; the decoded value is walked and the echo found, in a value and in a key."""
    state, cli, _ = gateway
    escaped = "".join(f"\\u{ord(char):04x}" for char in SESSION_TOKEN[:4]) + SESSION_TOKEN[4:]
    assert SESSION_TOKEN not in escaped
    detail = json.dumps(
        {
            **state.detail,
            "messages": [{"role": "user", "content": "<content>", "meta": {"<key>": 1}}],
            "total": 1,
        }
    )
    state.raw_detail = detail.replace("<content>", escaped).replace("<key>", escaped).encode()
    result = await Crew(str(cli)).detail("design")
    assert result["messages"][0] == {
        "role": "user",
        "content": "[redacted]",
        "meta": {"[redacted]": 1},
    }


def offline_crew(monkeypatch: pytest.MonkeyPatch) -> Crew:
    """A client whose gateway check and login are no-ops, so `_request` reaches the
    redaction step with whatever `_run_http` is set to return."""
    crew = Crew()
    crew.origin = "http://127.0.0.1:1"

    async def nothing() -> None:
        return None

    monkeypatch.setattr(crew, "_check_gateway", nothing)
    monkeypatch.setattr(crew, "_authenticate", nothing)
    return crew


async def test_a_credential_learned_while_a_response_is_redacted_is_still_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_io_lock` is released before the worker runs: request A snapshots the credentials,
    request B's response then teaches the client a new cookie, and A's worker returns a
    payload that echoes it (T-281 review, blocker 2). A must redact again before returning,
    and the worker must never read the live set."""
    crew = offline_crew(monkeypatch)
    calls: list[str] = []

    async def http(method: str, url: str, body: Any = None, headers: Any = None) -> bytes:
        calls.append(url)
        if url.endswith("/first"):
            return b'{"echo":"future-cookie"}'
        crew._learn(("future-cookie",))
        return b'{"ok":true}'

    entered = threading.Event()
    release = threading.Event()
    snapshots: list[tuple[str, ...]] = []
    redact_payload = Crew._redact_payload

    def paused(payload: bytes, credentials: tuple[str, ...]) -> Any:
        if b"future-cookie" in payload:
            snapshots.append(credentials)
            entered.set()
            assert release.wait(timeout=5)
        return redact_payload(payload, credentials)

    monkeypatch.setattr(crew, "_run_http", http)
    monkeypatch.setattr(crew, "_redact_payload", paused)
    first = asyncio.create_task(crew._request("GET", "/first"))
    assert await asyncio.to_thread(entered.wait, 5)
    assert await crew._request("GET", "/second") == {"ok": True}
    release.set()
    assert await first == {"echo": "[redacted]"}
    assert snapshots == [(), ("future-cookie",)]


async def test_a_credential_set_that_never_settles_fails_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew = offline_crew(monkeypatch)
    loop = asyncio.get_running_loop()
    passes = 0

    async def http(method: str, url: str, body: Any = None, headers: Any = None) -> bytes:
        return b'{"ok":true}'

    def moving(payload: bytes, credentials: tuple[str, ...]) -> Any:
        nonlocal passes
        passes += 1
        # Teach the client on the loop (its only writer) and wait until it has, so the set
        # has moved before this pass's result lands there.
        learned = threading.Event()
        loop.call_soon_threadsafe(crew._learn, (f"cookie-{passes}",))
        loop.call_soon_threadsafe(learned.set)
        assert learned.wait(timeout=5)
        return Crew._redact_payload(payload, credentials)

    monkeypatch.setattr(crew, "_run_http", http)
    monkeypatch.setattr(crew, "_redact_payload", moving)
    with pytest.raises(BridgeError, match="kept changing"):
        await crew._request("GET", "/moving")
    assert passes == 3


def test_learning_the_same_credentials_again_does_not_move_the_generation() -> None:
    crew = Crew()
    crew._learn(("a", "", "b"))
    crew._learn(("a", "b"))
    assert crew._secrets == {"a", "b"}
    assert crew._secrets_generation == 1
    crew._learn(("c",))
    assert crew._secrets_generation == 2


async def test_create_sets_project_before_return_and_reuses_matching_identity(
    gateway: Any, tmp_path: Path
) -> None:
    state, cli, _ = gateway
    crew = Crew(str(cli))
    result = await crew.create("Review worker", str(tmp_path), "create-id")
    expected_name = "bridge-" + hashlib.sha256(b"create-id").hexdigest()[:24]
    assert result["target"] == expected_name
    assert result["project"] == str(tmp_path)
    assert [r["path"] for r in posts(state)] == [
        "/api/chat/slots",
        f"/api/chat/slots/{expected_name}/project",
    ]
    assert posts(state)[0]["body"] == {
        "name": expected_name,
        "title": "Review worker",
        "agent_kind": "template",
        "no_session_folder": True,
    }
    assert posts(state)[1]["body"] == {"project": str(tmp_path)}
    assert await crew.create("Review worker", str(tmp_path), "create-id") == result
    assert len(posts(state)) == 2
    assert all(r["path"] != "/api/chat?ws=1" for r in state.requests)


async def test_create_without_a_durable_baseline_refuses_a_matching_existing_slot(
    gateway: Any, tmp_path: Path
) -> None:
    """red_at_76db6d7a: a fresh client adopted the predictable occupied slot."""
    state, cli, _ = gateway
    name = "bridge-" + hashlib.sha256(b"create-id").hexdigest()[:24]
    existing = slot(key=name, title="Review worker", project=str(tmp_path), agent="agent-b")
    state.slots.append(existing)

    with pytest.raises(SelectionRequired) as error:
        await Crew(str(cli)).create("Review worker", str(tmp_path), "create-id")

    assert [peer["target"] for peer in error.value.matches] == [name]
    assert not posts(state)


async def test_create_receipt_survives_restart_and_rejects_a_recreated_slot(
    gateway: Any, tmp_path: Path
) -> None:
    """red_at_76db6d7a: Crew had no durable creation-identity receipt."""
    state, cli, _ = gateway
    receipt_path = tmp_path / "tasks.sqlite"
    TaskStore(receipt_path)
    first = Crew(str(cli), receipt_path=receipt_path)
    created = await first.create("Review worker", str(tmp_path), "create-id")

    assert (
        await Crew(str(cli), receipt_path=receipt_path).create(
            "Review worker", str(tmp_path), "create-id"
        )
        == created
    )

    row = next(item for item in state.slots if item["key"] == created["target"])
    row.update(created="2030-12-31T23:59:59Z", agent="agent-b")
    with pytest.raises(CrewIdentityChanged):
        await Crew(str(cli), receipt_path=receipt_path).create(
            "Review worker", str(tmp_path), "create-id"
        )


async def test_create_receipt_schema_drift_is_a_store_error(tmp_path: Path) -> None:
    receipt_path = tmp_path / "tasks.sqlite"
    TaskStore(receipt_path)
    with sqlite3.connect(receipt_path) as db:
        db.execute("ALTER TABLE crew_creation_receipts ADD COLUMN drift TEXT")

    with pytest.raises(BridgeError, match="schema") as error:
        await Crew("kirocrew", receipt_path=receipt_path).create(
            "Review worker", str(tmp_path), "create-id"
        )

    assert type(error.value) is BridgeError


async def test_bridge_reuses_the_durable_create_receipt_after_client_reset(
    gateway: Any, tmp_path: Path
) -> None:
    """The service wires every replacement Crew client to the same receipt store."""
    state, cli, origin = gateway
    config = Config(
        "test",
        crew_binary=str(cli),
        crew_port=int(urlsplit(origin).port),
        crew_enabled=True,
    )
    config_path = tmp_path / "bridge.json"
    first = Bridge(config, config_path)
    try:
        created = await first.crew_create("Review worker", str(tmp_path), "create-id")
    finally:
        await first.close()

    restarted = Bridge(config, config_path)
    try:
        assert await restarted.crew_create("Review worker", str(tmp_path), "create-id") == created
        row = next(item for item in state.slots if item["key"] == created["target"])
        row["created"] = "2030-12-31T23:59:59Z"
        restarted._crew_client = None
        with pytest.raises(CrewIdentityChanged):
            await restarted.crew_create("Review worker", str(tmp_path), "create-id")
    finally:
        await restarted.close()


@pytest.mark.parametrize("changed", ["title", "project"])
async def test_create_retry_never_rebinds_existing_chat(
    gateway: Any, tmp_path: Path, changed: str
) -> None:
    state, cli, _ = gateway
    crew = Crew(str(cli))
    result = await crew.create("Review worker", str(tmp_path), "create-id")
    next(row for row in state.slots if row["key"] == result["target"])[changed] = "elsewhere"
    with pytest.raises(BridgeError, match="different"):
        await crew.create("Review worker", str(tmp_path), "create-id")
    assert len(posts(state)) == 2


async def test_create_verifies_directory_readback(gateway: Any, tmp_path: Path) -> None:
    state, cli, _ = gateway
    state.project_override = "/wrong"
    with pytest.raises(BridgeError, match="project"):
        await Crew(str(cli)).create("Review worker", str(tmp_path), "create-id")
    assert len(posts(state)) == 2


@pytest.mark.parametrize(
    "response",
    [
        {},
        [None],
        [slot(created=None)],
        [slot(running="false")],
        [slot(project=[])],
        [slot(), slot()],
    ],
)
async def test_rejects_malformed_discovery(gateway: Any, response: Any) -> None:
    state, cli, _ = gateway
    state.slots = response
    with pytest.raises(BridgeError):
        await Crew(str(cli)).peers()


async def test_rejects_oversized_json(gateway: Any) -> None:
    state, cli, _ = gateway
    state.raw_slots = b" " * (4 * 1024 * 1024 + 1)
    with pytest.raises(BridgeError, match="4 MiB"):
        await Crew(str(cli)).peers()


async def test_rejects_detail_wrong_slot(gateway: Any) -> None:
    state, cli, _ = gateway
    state.detail["key"] = "different"
    with pytest.raises(BridgeError):
        await Crew(str(cli)).detail("design")


async def test_supports_base_version_and_ignores_environment_proxies(
    gateway: Any, monkeypatch: Any
) -> None:
    _, cli, _ = gateway
    settings(cli, version="0.12.2")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert await Crew(str(cli)).peers()


async def test_slow_trickling_response_has_a_total_read_deadline(
    gateway: Any, monkeypatch: Any
) -> None:
    import codex_herdr_bridge.crew as crew_module

    state, cli, _ = gateway
    crew = Crew(str(cli))
    await crew.peers()
    monkeypatch.setattr(crew_module, "REQUEST_SECONDS", 0.12)
    state.slow_slots = True
    with pytest.raises(BridgeError, match="timed out"):
        await crew.peers()


async def test_http_error_is_never_replayed(gateway: Any) -> None:
    state, cli, _ = gateway
    state.send_status = 500
    state.send_response = {"error": LINK_TOKEN}
    crew = Crew(str(cli))
    with pytest.raises(BridgeError, match="uncertain") as error:
        await crew.submit("design", "Review", str(uuid4()), expected=await crew.peer("design"))
    assert LINK_TOKEN not in str(error.value)
    assert len(posts(state)) == 1


@pytest.mark.parametrize("data", [b"not json", b'{"data": NaN}', b'{"key":1,"key":2}'])
async def test_invalid_json_fails_closed(gateway: Any, data: bytes) -> None:
    state, cli, _ = gateway
    state.raw_slots = data
    with pytest.raises(BridgeError):
        await Crew(str(cli)).peers()


async def test_create_cannot_rebind_a_returned_preexisting_slot(
    gateway: Any, tmp_path: Path
) -> None:
    state, cli, _ = gateway
    state.old_slot_on_create = True
    state.slots[0]["title"] = "Review worker"
    with pytest.raises(BridgeError, match="existing"):
        await Crew(str(cli)).create("Review worker", str(tmp_path), "create-id")
    assert len(posts(state)) == 1


@pytest.mark.parametrize(
    "receipt", [{"ok": True, "mid": []}, {"ok": True, "queued": True, "queue_id": ""}]
)
async def test_rejects_malformed_receipt_identifiers(gateway: Any, receipt: Any) -> None:
    state, cli, _ = gateway
    state.send_response = receipt
    crew = Crew(str(cli))
    with pytest.raises(BridgeError):
        await crew.submit("design", "Review", str(uuid4()), expected=await crew.peer("design"))
    assert len(posts(state)) == 1


async def test_cancelled_http_call_closes_promptly_and_recovers(gateway: Any) -> None:
    state, cli, _ = gateway
    crew = Crew(str(cli))
    original = await crew.peers()
    state.slow_headers = True
    release = threading.Timer(0.75, state.release_headers.set)
    release.start()
    request = asyncio.create_task(crew.peers())
    try:
        assert await asyncio.to_thread(state.headers_started.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not state.release_headers.is_set()
        state.slow_headers = False
        assert await crew.peers() == original
    finally:
        state.release_headers.set()
        release.cancel()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_trickling_headers_cannot_outlive_total_request_deadline(
    gateway: Any, monkeypatch: Any
) -> None:
    import codex_herdr_bridge.crew as crew_module

    state, cli, _ = gateway
    crew = Crew(str(cli))
    await crew.peers()
    monkeypatch.setattr(crew_module, "REQUEST_SECONDS", 0.12)
    state.slow_headers = True
    release = threading.Timer(0.75, state.release_headers.set)
    release.start()
    try:
        with pytest.raises(BridgeError, match="timed out"):
            await crew.peers()
        assert not state.release_headers.is_set()
    finally:
        state.release_headers.set()
        release.cancel()


@pytest.mark.parametrize(
    "created",
    [
        "2026-11-13T09:32:07",
        "2026-11-13T09:32:07.123456+00:00",
        "2026-11-13T09:32:07Z",
    ],
)
async def test_live_iso_created_stamp_is_retained_and_guarded(gateway: Any, created: str) -> None:
    state, cli, _ = gateway
    state.slots[0]["created"] = created
    crew = Crew(str(cli))
    peer = await crew.peer("design")
    assert peer["created"] == created
    await crew.submit("design", "Review", str(uuid4()), expected=peer)
    state.slots[0]["created"] = "2026-11-14T09:32:07"
    with pytest.raises(CrewIdentityChanged):
        await crew.submit("design", "Review again", str(uuid4()), expected=peer)
    assert len(posts(state)) == 1


@pytest.mark.parametrize("created", ["", "not-a-timestamp", "2026-11-13", "2026-99-13T09:32:07"])
async def test_rejects_malformed_created_stamp(gateway: Any, created: str) -> None:
    state, cli, _ = gateway
    state.slots[0]["created"] = created
    with pytest.raises(BridgeError, match="identity"):
        await Crew(str(cli)).peers()


async def test_snapshot_from_another_gateway_cannot_authorize_delivery(gateway: Any) -> None:
    state, cli, origin = gateway
    crew = Crew(str(cli))
    peer = await crew.peer("design")
    assert peer["origin"] == origin
    peer["origin"] = "http://127.0.0.1:1"
    with pytest.raises(CrewIdentityChanged):
        await crew.submit("design", "Review", str(uuid4()), expected=peer)
    assert not posts(state)


async def test_http_diagnostics_do_not_disclose_login_credentials(
    gateway: Any, caplog: Any
) -> None:
    import logging

    _, cli, _ = gateway
    caplog.set_level(logging.DEBUG)
    assert await Crew(str(cli)).peers()
    assert LINK_TOKEN not in caplog.text
    assert SESSION_TOKEN not in caplog.text
    logging.getLogger("httpx2").info("unrelated HTTP diagnostics remain available")
    assert "unrelated HTTP diagnostics remain available" in caplog.text
