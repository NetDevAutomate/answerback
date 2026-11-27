"""Contract-checked access to Crew's owner-authenticated dashboard chat API.

Credentials stay in memory. Delivery checks observe identity; Crew has no atomic
expected-incarnation guard, and an HTTP receipt does not establish task completion.

Kiro Crew ships far more often than this bridge, so the installed version is not the
gate. A version below the first one this client was written for is refused before a
token is minted. Every later version is served, and what protects the owner's session
is the validation of each answer: the loopback login URL, the separate session cookie,
the identity fields of every chat, the bounded history rows and the delivery receipt.
A release that changes one of those is refused at that answer. The version is kept as
evidence: `version` and `version_status` say what was observed and whether anyone has
exercised that release series against this client.

The version is read leniently for the same reason: a build suffix (`0.14.0rc1`,
`0.13.1.dev20261121`), a fifth number or a reworded `--version` line must not take the
provider away. Output that does not state exactly one version is refused as unreadable.

The checks cannot undo a write. When one fails after a POST, the message may have been
delivered or the chat created. For a send, this client raises an error that says the
delivery is uncertain and never posts again; what the receipt then records is the task
store's decision, and the worker's own report can still complete it. A creation is
reported as failed with the chat possibly present.
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urljoin, urlsplit
from uuid import UUID

import httpx2

from ...discovery import SelectionRequired
from ...errors import CrewVersionUnsupported
from ...process import BridgeError, CommandError, CommandTimeout, ProviderOffline, run

MAX_RESPONSE = 4 * 1024 * 1024
REQUEST_SECONDS = 15
SESSION_SECONDS = 55 * 60
HISTORY_LIMIT = 200
# How many times one response is redacted when the credential set grows while a pass runs
# in the worker thread; a set that moves on every pass is an error, not a loop.
REDACTION_PASSES = 3
# How many URL-decoding passes a string gets before it is treated as a credential echo.
DECODING_PASSES = 3
# The first release this client was written for; an older one is refused.
MINIMUM_VERSION = (0, 12, 2)
# Release series exercised end to end against this client (login, discovery, history,
# delivery and chat creation). A later series is served and reported as "untested".
TESTED_SERIES = frozenset({(0, 12, 2)})
# One line of `kirocrew --version`: an optional product name, an optional "version" or
# "v", two to eight dotted numbers, and an optional build suffix such as rc1, .dev3,
# -nightly.5 or +g1a2b3c. Seen so far: "kirocrew 0.12.2.4" and "kirocrew 0.13.0.4".
_VERSION_LINE = re.compile(
    r"(?:kiro[ _-]?crew\b)?[\s,:]*(?:version\b)?[\s:]*v?"
    r"(\d{1,12}(?:\.\d{1,12}){1,7})"
    r"((?:[-+_][a-z0-9]|\.?[a-z])[a-z0-9.+_-]{0,31})?",
    re.IGNORECASE,
)
# The characters a JSON encoder can spell only literally or as `\uXXXX`: everything but
# the quote, the backslash, the solidus (`\/` is a legal escape) and the control
# characters. A credential made only of these appears verbatim in the raw text wherever a
# decoded string holds it, unless the text spells a character with `\u` or percent-encodes
# the credential; `_redact_payload` checks both before it skips the walk. The space is
# left out too: form encoding spells it `+`, which neither check would see.
_PLAIN = re.compile(r'[^"\\/\x00-\x20]+')
_REDACTED = "[redacted]"
_STATE_FLAGS = (
    "running",
    "admission_queued",
    "stopping",
    "pending_approval",
    "waiting_for_input",
    "needs_input",
    "interrupted",
    "compacting",
    "preparing",
)


def _version(text: str) -> tuple[tuple[int, ...], str, str] | None:
    """Find the release in `kirocrew --version` output.

    Returns its numbers, its build suffix and the statement as printed:
    `kirocrew 0.13.0.4` gives ((0, 13, 0, 4), "", "0.13.0.4"). Numbers are compared as
    numbers (`2026.11.05` is 2026.11.5) and reported as printed.

    Only the first 20 lines are read, and a line longer than 200 characters is not read
    at all: cutting it could cut a suffix off and change what it says. A notice before
    or after the statement is ignored. Output with no statement, or with two statements
    that differ, is not a version.
    """
    found: dict[tuple[tuple[int, ...], str], tuple[tuple[int, ...], str, str]] = {}
    for line in text.splitlines()[:20]:
        line = line.strip()
        match = _VERSION_LINE.fullmatch(line) if len(line) <= 200 else None
        if match:
            numbers = tuple(int(part) for part in match.group(1).split("."))
            suffix = match.group(2) or ""
            found.setdefault((numbers, suffix.lower()), (numbers, suffix, match.group(1) + suffix))
    return next(iter(found.values())) if len(found) == 1 else None


def _shown(text: str) -> str:
    """The first line the executable printed, bounded and printable, for an error."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    kept = "".join(
        char for char in first.strip() if char.isascii() and (char.isalnum() or char in " ._+-")
    )
    return kept[:40] or "(unreadable)"


def _origin(url: str) -> str:
    """Admit explicit HTTP loopback authorities without credentials or aliases."""
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "http"
            or not host
            or "%" in host
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        return f"http://{authority}:{port}"
    except ValueError:
        raise BridgeError("Crew login requires an explicit HTTP loopback URL") from None


class CrewIdentityChanged(BridgeError):
    """Observed chat identity contradicts the identity selected by the caller."""


_PRIVATE_HTTP: ContextVar[bool] = ContextVar("crew_private_http", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # These libraries log URLs and response headers, including login tokens
        # and Set-Cookie. Keep unrelated tasks' HTTP diagnostics available.
        return not _PRIVATE_HTTP.get()


_PRIVATE_FILTER = _PrivateRequestFilter()
_PRIVATE_LOGGERS = (
    "httpx2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
)


def _created_stamp(value: Any) -> bool:
    if type(value) in (int, float):
        try:
            return math.isfinite(value) and value >= 0
        except OverflowError:
            return False
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", value
    ):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise BridgeError("Crew returned an invalid chat key")
    return value


def _flags(value: dict[str, Any]) -> None:
    if type(value.get("running")) is not bool or any(
        name in value and type(value[name]) is not bool for name in _STATE_FLAGS
    ):
        raise BridgeError("Crew returned unsupported chat state")


def _state(row: dict[str, Any]) -> str:
    for flag, state in (
        ("stopping", "stopping"),
        ("pending_approval", "blocked"),
        ("needs_input", "blocked"),
        ("waiting_for_input", "blocked"),
        ("admission_queued", "queued"),
        ("running", "working"),
        ("compacting", "working"),
        ("preparing", "working"),
        ("interrupted", "interrupted"),
    ):
        if row.get(flag):
            return state
    return "idle"


def _peer(row: Any, origin: str | None = None) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise BridgeError("Crew returned an unsupported chat summary")
    key = _key(row.get("key"))
    if not all(
        isinstance(row.get(name), str) for name in ("title", "project", "agent")
    ) or not _created_stamp(row.get("created")):
        raise BridgeError("Crew returned incomplete chat identity")
    _flags(row)
    # Lets a caller that selected this chat through peer_search carry its
    # discovery-time identity into peer_submit; a later resolve with a
    # different binding (recreated slot, rebound project, replaced agent)
    # is then refused instead of silently delivering to the replacement.
    binding = hashlib.sha256(
        json.dumps([origin, key, row["created"], row["project"], row["agent"]]).encode()
    ).hexdigest()
    return {
        "peer_id": f"crew:{key}",
        "provider": "crew",
        "target": key,
        "name": row["title"] or key,
        "project": row["project"],
        "kind": "interactive",
        "state": _state(row),
        "capabilities": ["send", "read", "worker_report"],
        "created": row["created"],
        "agent": row["agent"],
        "binding": binding,
        **({"origin": origin} if origin is not None else {}),
    }


_CREATION_RECEIPT_COLUMNS = {"request_key", "title", "project", "identity"}


class _CreationReceipts:
    """Creation identities retained in the task-receipt SQLite store."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _identity(value: Any) -> dict[str, Any]:
        try:
            identity = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise CrewIdentityChanged("Stored Crew creation identity is invalid") from None
        if (
            not isinstance(identity, dict)
            or set(identity)
            not in (
                {"target", "created", "project", "agent"},
                {"target", "created", "project", "agent", "origin"},
            )
            or not isinstance(identity.get("target"), str)
            or not _created_stamp(identity.get("created"))
            or not isinstance(identity.get("project"), str)
            or not isinstance(identity.get("agent"), str)
            or ("origin" in identity and not isinstance(identity["origin"], str))
        ):
            raise CrewIdentityChanged("Stored Crew creation identity is invalid")
        return identity

    def _prepare(self, db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(crew_creation_receipts)")}
        if columns != _CREATION_RECEIPT_COLUMNS:
            raise BridgeError("Stored Crew creation identity schema is invalid")

    def get(self, request_key: str, title: str, project: str) -> dict[str, Any] | None:
        try:
            with self._connection() as db:
                self._prepare(db)
                row = db.execute(
                    "SELECT title,project,identity FROM crew_creation_receipts WHERE request_key=?",
                    (request_key,),
                ).fetchone()
        except sqlite3.Error:
            raise BridgeError("Crew creation receipt store is unavailable") from None
        if row is None:
            return None
        if row["title"] != title or row["project"] != project:
            raise BridgeError("Crew creation request already names a different title or project")
        return self._identity(row["identity"])

    def put(self, request_key: str, title: str, project: str, identity: dict[str, Any]) -> None:
        stored = {
            key: identity[key]
            for key in ("target", "created", "project", "agent", "origin")
            if key in identity
        }
        encoded = json.dumps(stored, allow_nan=False, separators=(",", ":"), sort_keys=True)
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._prepare(db)
                row = db.execute(
                    "SELECT title,project,identity FROM crew_creation_receipts WHERE request_key=?",
                    (request_key,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO crew_creation_receipts VALUES (?,?,?,?)",
                        (request_key, title, project, encoded),
                    )
                    return
                if (
                    row["title"] != title
                    or row["project"] != project
                    or self._identity(row["identity"]) != stored
                ):
                    raise CrewIdentityChanged("Stored Crew creation identity changed")
        except CrewIdentityChanged:
            raise
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            raise BridgeError("Crew creation receipt store is unavailable") from None


class Crew:
    def __init__(
        self,
        binary: str = "kirocrew",
        port: int | None = None,
        *,
        receipt_path: Path | None = None,
    ) -> None:
        if not isinstance(binary, str) or not binary:
            raise ValueError("Crew executable is required")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("Crew port must be between 1 and 65535")
        self.binary = binary
        self.port = port
        self.origin: str | None = None
        # What the latest login attempt read from `kirocrew --version`: the release as
        # printed (None when the command failed or stated no single version), whether it
        # is "tested", "untested" or "unsupported" (None when the command failed), and
        # when that attempt started its version check (set even when the command
        # failed). A login lasts up to 55 minutes, so after a Crew update these describe
        # the previous release until the next login.
        self.version: str | None = None
        self.version_status: str | None = None
        self.version_checked_at: float | None = None
        # Called after every reading, so the owner of this client can publish it. A
        # login is triggered by discovery, a read or a send, not only by a setup command.
        self.on_version: Callable[[], None] | None = None
        self._cookies = CookieJar()
        for name in _PRIVATE_LOGGERS:
            logging.getLogger(name).addFilter(_PRIVATE_FILTER)
        self._auth_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()
        self._create_lock = asyncio.Lock()
        self._authenticated_until = 0.0
        # Every credential ever held, and the count of times the set grew. Both are read
        # and written on the event loop only; `_learn` is the one writer.
        self._secrets: set[str] = set()
        self._secrets_generation = 0
        self._created_requests: dict[str, dict[str, Any]] = {}
        self._creation_receipts = (
            _CreationReceipts(receipt_path) if receipt_path is not None else None
        )

    def _learn(self, values: Iterable[str | None]) -> None:
        """Hold more credentials (an empty or missing value is not one). The generation
        moves only when the set grows, so a request that re-learns the cookies it already
        holds does not make a concurrent redaction repeat."""
        added = {value for value in values if value} - self._secrets
        if added:
            self._secrets |= added
            self._secrets_generation += 1

    def _credentials(self) -> tuple[str, ...]:
        """An immutable snapshot of the held credentials. Read on the event loop, where
        `_secrets` is written, and handed to the worker thread as a tuple so the set is
        never read from two threads."""
        return tuple(self._secrets)

    @staticmethod
    def _redact_payload(payload: bytes, credentials: tuple[str, ...]) -> Any:
        """Decode one response and remove every credential echo. Runs in a worker thread:
        a 200-message history is megabytes, and walking it on the loop for every held
        credential stalled the service for seconds once the set had grown (T-281).

        The walk is skipped only when it cannot find anything: every credential is made of
        characters a JSON encoder writes literally (`_PLAIN`), the text holds no `\\u`
        escape (the one JSON escape that can spell such a character), no `%` (a URL-encoded
        echo, in any case of hex and of any character, decodes to the credential only when
        the text holds a percent sign), and no credential is in the text. Then no string of
        the decoded tree holds a credential raw or URL-encoded. Anything else takes the
        walk over the decoded tree.
        """
        # What `json.loads(bytes)` does before parsing, so the text is what it would parse.
        text = payload.decode(json.detect_encoding(payload), "surrogatepass")
        value = json.loads(text)
        if (
            all(_PLAIN.fullmatch(credential) for credential in credentials)
            and "\\u" not in text
            and "%" not in text
            and not any(credential in text for credential in credentials)
        ):
            return value
        return Crew._redact(value, credentials)

    @staticmethod
    def _echoes(value: str, credentials: tuple[str, ...]) -> bool:
        """Whether a string holds a credential, raw or URL-encoded. Each decoding pass is
        tested, until the string stops changing or DECODING_PASSES passes are done, in both
        conventions (`+` is a plus in a path and a space in a form or query; `parse_qsl`,
        which learns the link token, reads it so). Only a hit redacts: a string nested
        deeper than the bound and never matching is kept, because no gateway encodes that
        deep and a string that holds no credential is not the bridge's to replace."""
        if any(credential in value for credential in credentials):
            return True
        if "%" not in value and "+" not in value:
            return False
        for decode in (unquote, unquote_plus):
            text = value
            for _ in range(DECODING_PASSES):
                decoded = decode(text, errors="surrogateescape")
                if decoded == text:
                    break
                if any(credential in decoded for credential in credentials):
                    return True
                text = decoded
        return False

    @staticmethod
    def _redact(value: Any, credentials: tuple[str, ...]) -> Any:
        """Replace every string (and key) that holds a credential, raw or URL-encoded, with
        the redaction marker. The whole string goes: a Crew payload string that embeds a
        credential is not something the bridge needs to keep, and replacing only the
        spelling that matched would leave every other spelling (`%2f` for `%2F`, `%65` for
        `e`, mixed, nested) for a reader to decode."""
        if isinstance(value, str):
            return _REDACTED if Crew._echoes(value, credentials) else value
        if isinstance(value, list):
            return [Crew._redact(item, credentials) for item in value]
        if isinstance(value, dict):
            return {
                Crew._redact(key, credentials): Crew._redact(item, credentials)
                for key, item in value.items()
            }
        return value

    async def _check_gateway(self) -> None:
        # A configured endpoint (or an authenticated origin) is authoritative.
        # Legacy auto-discovery has no endpoint to probe until its first login.
        port = self.port or (urlsplit(self.origin).port if self.origin else None)
        if port is None:
            return
        host = urlsplit(self.origin).hostname if self.origin else "127.0.0.1"
        try:
            async with asyncio.timeout(0.5):
                _, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError):
            # Refused, timed out, or unreachable: the loopback endpoint is known, so any
            # failure to connect means no gateway is listening there. Container network
            # stacks report a closed loopback port as unreachable rather than refused.
            self._authenticated_until = 0.0
            raise ProviderOffline(
                f"Crew gateway is offline or unavailable on port {port}; "
                "excluded until it is running"
            ) from None

    async def _run_http(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            **(headers or {}),
        }
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            request_headers.update(
                {"Content-Type": "application/json", "Origin": self.origin or ""}
            )
        private_logging = _PRIVATE_HTTP.set(True)
        try:
            # One wall-clock deadline covers connection, every redirect, headers,
            # and the streamed body. Async transport cancellation closes the socket.
            async with asyncio.timeout(REQUEST_SECONDS):
                async with httpx2.AsyncClient(
                    cookies=self._cookies,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=REQUEST_SECONDS,
                ) as client:
                    for _ in range(6):
                        async with client.stream(
                            method, url, content=data, headers=request_headers
                        ) as response:
                            declared = response.headers.get("Content-Length")
                            if declared is not None:
                                if int(declared) > MAX_RESPONSE:
                                    raise BridgeError("Crew response exceeded the 4 MiB bound")
                                if int(declared) < 0:
                                    raise BridgeError("Crew returned an invalid response length")
                            if response.status_code in (301, 302, 303, 307, 308):
                                if method != "GET":
                                    raise BridgeError(
                                        "Crew write redirect refused; delivery is uncertain"
                                    )
                                location = response.headers.get("Location")
                                if not location:
                                    raise BridgeError(
                                        "Crew returned a redirect without a destination"
                                    )
                                destination = urljoin(url, location)
                                if _origin(destination) != self.origin:
                                    raise BridgeError("Crew cross-origin redirect refused")
                                url = destination
                                continue
                            if not 200 <= response.status_code < 300:
                                raise BridgeError(
                                    "Crew request failed; check the running gateway and owner login"
                                )
                            if (
                                response.headers.get("Content-Encoding", "identity").lower()
                                != "identity"
                            ):
                                raise BridgeError("Crew returned an unsupported response encoding")
                            payload = bytearray()
                            async for chunk in response.aiter_raw():
                                payload.extend(chunk)
                                if len(payload) > MAX_RESPONSE:
                                    raise BridgeError("Crew response exceeded the 4 MiB bound")
                            return bytes(payload)
                    raise BridgeError("Crew exceeded the same-origin redirect limit")
        except (TimeoutError, httpx2.TimeoutException):
            raise BridgeError("Crew request timed out") from None
        except BridgeError:
            raise
        except Exception:
            # Never surface transport exceptions: their text can contain URLs,
            # header values or other credentials belonging to the owner session.
            raise BridgeError(
                "Crew request failed; check the running gateway and owner login"
            ) from None
        finally:
            self._learn(cookie.value for cookie in self._cookies)
            _PRIVATE_HTTP.reset(private_logging)

    async def _authenticate(self) -> None:
        async with self._auth_lock:
            if time.monotonic() < self._authenticated_until and any(
                not cookie.is_expired() for cookie in self._cookies
            ):
                return
            # Nothing from an earlier login may stand for this attempt.
            self.version = self.version_status = None
            self.version_checked_at = time.time()
            try:
                try:
                    version = await run([self.binary, "--version"], max_seconds=30)
                except CommandTimeout:
                    raise BridgeError(
                        "Crew version check timed out after 30 seconds; run kirocrew --version "
                        "in your shell and check its launcher before retrying setup"
                    ) from None
                except CommandError as error:
                    raise BridgeError(
                        f"Crew version check failed (exit {error.returncode}); "
                        "run kirocrew --version in your shell to inspect its launcher"
                    ) from None
                except Exception:
                    raise BridgeError(
                        "Crew version check failed; check the installed executable"
                    ) from None
                numbers, suffix, printed = _version(version.stdout) or ((), "", "")
                self.version = printed or None
                released = numbers
                while released and released[-1] == 0:
                    released = released[:-1]
                # A suffixed build of exactly the minimum (0.12.2rc1, 0.12.2.0-dev) may
                # predate that release, so it is refused with the older ones.
                if numbers < MINIMUM_VERSION or (suffix and released == MINIMUM_VERSION):
                    # Refused before a token is minted: nothing is sent to a gateway
                    # whose release predates this client or cannot be identified.
                    self.version_status = "unsupported"
                    raise CrewVersionUnsupported(
                        f"Unsupported Crew version {self.version or _shown(version.stdout)}; "
                        "this adapter requires kirocrew 0.12.2 or later"
                    )
                # A build suffix marks something other than the release that was exercised.
                self.version_status = (
                    "tested" if not suffix and numbers[:3] in TESTED_SERIES else "untested"
                )
            finally:
                if self.on_version is not None:
                    self.on_version()
            args = [self.binary, "token", "--ttl", "1h"]
            if self.port is not None:
                args.extend(["--port", str(self.port)])
            try:
                issued = await run(args, max_seconds=30)
            except Exception:
                raise BridgeError(
                    "Crew login failed; complete the CLI owner login and check the gateway"
                ) from None
            urls = [
                line.strip()
                for line in issued.stdout.splitlines()
                if line.strip().startswith(("http://", "https://"))
            ]
            if not urls:
                raise BridgeError("Crew login returned no canonical loopback URL")
            login_url = urls[0]
            origin = _origin(login_url)
            parsed = urlsplit(login_url)
            values = parse_qsl(parsed.query, keep_blank_values=True)
            if (
                len(values) != 1
                or values[0][0] != "token"
                or not values[0][1]
                or parsed.path not in ("", "/")
                or parsed.fragment
            ):
                raise BridgeError("Crew login returned an unsupported token URL")
            if self.port is not None and parsed.port != self.port:
                raise BridgeError("Crew login URL does not match the selected port")
            if self.origin is not None and self.origin != origin:
                raise BridgeError("Crew login gateway changed; reconnect before sending")
            self._learn((values[0][1],))
            self.origin = origin
            async with self._io_lock:
                self._cookies.clear()
                await self._run_http("GET", login_url)
                session_cookies = [
                    cookie
                    for cookie in self._cookies
                    if cookie.name == f"mc_token_{parsed.port}" and not cookie.is_expired()
                ]
                if not session_cookies or any(
                    cookie.value == values[0][1] for cookie in session_cookies
                ):
                    self._cookies.clear()
                    raise BridgeError("Crew login did not issue a separate session cookie")
            self._authenticated_until = time.monotonic() + SESSION_SECONDS

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        await self._check_gateway()
        await self._authenticate()
        async with self._io_lock:
            payload = await self._run_http(method, f"{self.origin}{path}", body, headers)
        # `_io_lock` is released before the worker runs, so another request can learn a
        # cookie or a link token while this one is being redacted; the worker never sees
        # it, since it only holds the tuple. Redact again from the original payload with a
        # fresh snapshot until the set is stable across one whole pass (T-281 review).
        for _ in range(REDACTION_PASSES):
            generation = self._secrets_generation
            credentials = self._credentials()
            try:
                value = await asyncio.to_thread(self._redact_payload, payload, credentials)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise BridgeError("Crew returned an unsupported JSON response") from None
            if generation == self._secrets_generation:
                return value
        raise BridgeError("Crew credentials kept changing while a response was redacted")

    async def peers(self) -> list[dict[str, Any]]:
        rows = await self._request("GET", "/api/chat/slots")
        if not isinstance(rows, list):
            raise BridgeError("Crew returned an unsupported chat list")
        peers = [_peer(row, self.origin) for row in rows]
        if len({peer["target"] for peer in peers}) != len(peers):
            raise BridgeError("Crew returned duplicate chat identities")
        return peers

    async def peer(self, target: str) -> dict[str, Any]:
        _key(target)
        for peer in await self.peers():
            if peer["target"] == target:
                return peer
        raise BridgeError("Crew chat identity is unavailable")

    async def detail(self, target: str) -> dict[str, Any]:
        _key(target)
        row = await self._request(
            "GET", f"/api/chat/slots/{quote(target, safe='')}?limit=200&cursor=1"
        )
        if not isinstance(row, dict) or row.get("key") != target:
            raise BridgeError("Crew history returned an unexpected chat identity")
        _flags(row)
        messages, queue = row.get("messages"), row.get("queue")
        if (
            not isinstance(messages, list)
            or len(messages) > HISTORY_LIMIT
            or not isinstance(queue, list)
            or len(queue) > HISTORY_LIMIT
            or type(row.get("total")) is not int
            or row["total"] < 0
            or type(row.get("has_more")) is not bool
        ):
            raise BridgeError("Crew returned unsupported history or exceeded its row bound")
        for message in messages:
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)
                or ("meta" in message and not isinstance(message["meta"], dict))
            ):
                raise BridgeError("Crew returned an unsupported history row")
        for message in queue:
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("id"), str)
                or not isinstance(message.get("content"), str)
                or ("meta" in message and not isinstance(message["meta"], dict))
            ):
                raise BridgeError("Crew returned an unsupported queue row")
        permitted = (
            "key",
            "title",
            "running",
            "admission_queued",
            "turn",
            "turn_gen",
            "stopping",
            "stop_state",
            "stop_declined",
            "messages",
            "queue",
            "total",
            "has_more",
            "next_before",
            "cursor",
            "delta",
        )
        # Optional tracing metadata is projected, never forwarded wholesale: a gateway
        # process object may also carry argv, environment or provider-private fields.
        from ...events.resources import crew_tokens

        telemetry: dict[str, Any] = {}
        if "turn_stats" in row:
            tokens = crew_tokens(row["turn_stats"])
            telemetry["turn_stats"] = {"total_tokens": tokens} if tokens is not None else None
        if "process" in row:
            process = row["process"]
            pid = process.get("pid") if isinstance(process, dict) else None
            telemetry["process"] = {"pid": pid} if type(pid) is int and pid > 0 else None
        return {
            **{name: row[name] for name in permitted if name in row},
            **telemetry,
            "coverage": {
                "limit": HISTORY_LIMIT,
                "returned": len(messages),
                "total": row["total"],
                "has_more": row["has_more"],
            },
            "completion": "unknown",
        }

    @staticmethod
    def _check_identity(target: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
        expected_key = expected.get("target", expected.get("key"))
        if (
            expected_key != target
            or actual.get("target") != target
            or (
                ("origin" in expected or "origin" in actual)
                and expected.get("origin") != actual.get("origin")
            )
            or any(
                name not in expected or actual.get(name) != expected[name]
                for name in ("created", "project", "agent")
            )
        ):
            raise CrewIdentityChanged(
                "Crew chat identity changed or its identity evidence is incomplete"
            )

    async def submit(
        self, target: str, message: str, send_id: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Crew message must not be empty")
        try:
            if not isinstance(send_id, str) or str(UUID(send_id)) != send_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("Crew send ID must be a canonical UUID") from None
        self._check_identity(target, expected, await self.peer(target))
        try:
            receipt = await self._request(
                "POST",
                "/api/chat?ws=1",
                {
                    "slot": target,
                    "message": message,
                    "turn_origin": "agent",
                    "steer": False,
                    "meta": {"sendId": send_id},
                },
                {"Idempotency-Key": send_id},
            )
            if isinstance(receipt, dict) and "slot" in receipt and receipt["slot"] != target:
                raise CrewIdentityChanged("Crew delivery receipt named a different chat")
            if (
                not isinstance(receipt, dict)
                or receipt.get("ok") is not True
                or ("queued" in receipt and type(receipt["queued"]) is not bool)
                or receipt.get("steered")
                or (
                    "mid" in receipt and (not isinstance(receipt["mid"], str) or not receipt["mid"])
                )
                or (
                    receipt.get("queued")
                    and (not isinstance(receipt.get("queue_id"), str) or not receipt["queue_id"])
                )
            ):
                raise BridgeError("Crew returned an unsupported delivery receipt")
            self._check_identity(target, expected, await self.peer(target))
        except CrewIdentityChanged:
            raise
        except BridgeError as error:
            raise BridgeError(
                f"Crew delivery is uncertain: {error}. Do not resubmit blindly."
            ) from None
        result = {
            name: receipt[name]
            for name in ("ok", "slot", "mid", "queued", "queue_id")
            if name in receipt
        }
        result.update(
            {
                "status": "queued" if receipt.get("queued") else "accepted",
                "identity_guard": "observed_before_and_after",
            }
        )
        return result

    async def create(self, title: str, project: str, request_id: str) -> dict[str, Any]:
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in title)
        ):
            raise ValueError("Crew title must be 1–200 characters without control characters")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512:
            raise ValueError("Crew creation request ID must be 1–512 characters")
        if not isinstance(project, str) or not Path(project).is_absolute():
            raise ValueError("Crew project must be an absolute existing directory")
        directory = await asyncio.to_thread(lambda: Path(project).resolve(strict=True))
        if not await asyncio.to_thread(directory.is_dir):
            raise ValueError("Crew project must be an absolute existing directory")
        project = str(directory)
        request_key = hashlib.sha256(request_id.encode()).hexdigest()
        name = "bridge-" + request_key[:24]
        async with self._create_lock:
            prior = self._created_requests.get(request_key)
            if prior is None and self._creation_receipts is not None:
                prior = await asyncio.to_thread(
                    self._creation_receipts.get, request_key, title, project
                )
            key = prior["target"] if prior else name
            inventory = await self.peers()
            existing = next((peer for peer in inventory if peer["target"] == key), None)
            if existing is not None:
                if prior is None:
                    raise SelectionRequired(
                        "A Crew chat already holds this creation request's slot; "
                        "select it by peer_id",
                        [existing],
                        {},
                    )
                if existing["name"] != title or existing["project"] != project:
                    raise BridgeError(
                        "Crew creation request already names a different title or project"
                    )
                self._check_identity(key, prior, existing)
                self._created_requests[request_key] = prior.copy()
                return existing
            if prior is not None:
                raise BridgeError(
                    "Previously created Crew chat is unavailable; refusing to replace it"
                )
            created = _peer(
                await self._request(
                    "POST",
                    "/api/chat/slots",
                    {
                        "name": name,
                        "title": title,
                        "agent_kind": "template",
                        "no_session_folder": True,
                    },
                ),
                self.origin,
            )
            if created["name"] != title:
                raise BridgeError("Crew created a chat with a different title")
            key = created["target"]
            if any(peer["target"] == key for peer in inventory):
                raise BridgeError("Crew returned an existing chat; refusing to change its project")
            # A fresh named slot may inherit Crew's default directory. Rebinding
            # is limited to this verified creation; existing slots above never move.
            self._check_identity(key, created, await self.peer(key))
            response = await self._request(
                "POST", f"/api/chat/slots/{quote(key, safe='')}/project", {"project": project}
            )
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise BridgeError(
                    "Crew project assignment was not acknowledged; inspect the created chat"
                )
            result = await self.peer(key)
            if result["project"] != project or result["name"] != title:
                raise BridgeError("Crew project or title readback did not match the requested chat")
            self._check_identity(key, {**created, "project": project}, result)
            if self._creation_receipts is not None:
                await asyncio.to_thread(
                    self._creation_receipts.put, request_key, title, project, result
                )
            self._created_requests[request_key] = result.copy()
            return result


def check_identity(target: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Raise CrewIdentityChanged unless `actual` is the chat `expected` identifies.

    The public name of the one Crew identity comparison, `Crew._check_identity`, for
    callers outside the client: the Crew adapter runs it against its live lookup before
    reserving a receipt, and `Crew.submit` runs it again before and after its POST.
    """
    Crew._check_identity(target, expected, actual)
