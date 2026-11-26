"""A long-lived ``hc connect`` session and Herdr Core's document store (experimental).

Interface used: the ``hc connect`` CLI of hc 0.1.21 (Herdr, Apache-2.0) in its
plain line mode, with method shapes taken from ``hc schema`` (OpenRPC). In that
mode each JSON-RPC 2.0 request is one line on the child's standard input and
each response is one line on its standard output. The handshake sends
``hello {"generation": 1, "framed": false}``; the framed mode, whose binary
layout is not part of the schema or help text, is never requested, and a
``hello`` result that reports ``framed: true`` closes the session.

Observed on hc 0.1.21 (T-87 sample): the plain mode is the default; ``doc.get``
of a name never written returns ``{"value": null, "version": 0}``; a
``doc.set`` with a stale version fails with ``data.kind == "conflict"``; writing
``null`` deletes the value but the version keeps counting.

Limits of this module, by design:

- one request is in flight at a time; concurrent ``call`` coroutines queue on a
  lock, so a queued call can wait up to the running call's own timeout first;
- any timeout, cancellation, malformed, oversized or unmatched line closes the
  session, because the stream position is then unknown;
- only an allow-list of methods is sent (``DEFAULT_METHODS``); ``hello`` is
  sent once, by ``start``;
- ``DocStore`` writes only documents whose names start with ``answerback`` and
  only on the local core (it never sends ``on``).

Nothing in the bridge uses this module yet: there is no config field and no
service wiring.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any

from ...process import BridgeError, ChildProcess, spawn_owned, stop_child

PROTOCOL_GENERATION = 1
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_DOC_BYTES = 1024 * 1024
MAX_DOC_DEPTH = 64
MAX_HELLO_ITEMS = 1024
MAX_ERROR_TEXT = 300
_MAX_UINT64 = 2**64 - 1
_SCRUBBED_ENV = ("HC_TERMINAL_ID", "HC_SOCKET", "HC_SESSION")
DEFAULT_METHODS = frozenset({"rpc.discover", "doc.get", "doc.set"})
# Core's own name rule (hc 0.1.21 error text): 1-32 of a-z 0-9 - _ . starting
# with a letter or digit. Ours is stricter: the prefix ``answerback`` is required.
DOC_PREFIX = "answerback"
_DOC_NAME = re.compile(r"answerback[a-z0-9._-]{0,22}\Z")


class CoreRpcError(BridgeError):
    """Base class for every error from this module except refusals."""


class CoreSessionClosed(CoreRpcError):
    """The session is closed, ended, or was never started; it cannot be used."""


class CoreProtocolError(CoreSessionClosed):
    """Core's stream broke the line protocol; the session has been closed."""


class CoreCallTimeout(CoreSessionClosed):
    """No response arrived in time; the session has been closed."""


class CoreErrorReply(CoreRpcError):
    """Core answered a request with a JSON-RPC error; the session stays open."""

    def __init__(self, method: str, code: int, kind: str | None, message: str) -> None:
        self.method = method
        self.code = code
        self.kind = kind
        self.core_message = message
        super().__init__(f"Core {method} failed: {kind or code}: {message}")


class DocConflict(CoreErrorReply):
    """``doc.set`` was refused because the expected version is no longer current."""


class DocRefused(ValueError):
    """A document request was refused locally, before anything was sent."""


def _clean(text: str) -> str:
    """Bound untrusted Core text and drop control and escape characters."""
    kept = "".join(char if char.isprintable() else "?" for char in text[:MAX_ERROR_TEXT])
    return kept + ("..." if len(text) > MAX_ERROR_TEXT else "")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _decode(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            line.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (ValueError, RecursionError):
        raise CoreProtocolError("Core sent a line that is not valid JSON") from None
    if not isinstance(value, dict):
        raise CoreProtocolError("Core sent a line that is not a JSON object")
    return value


def _string_list(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > MAX_HELLO_ITEMS:
        raise CoreProtocolError(f"Core hello {field} is not a bounded list")
    if not all(isinstance(item, str) and len(item) <= 128 for item in value):
        raise CoreProtocolError(f"Core hello {field} holds a non-text item")
    return frozenset(value)


class CoreSession:
    """One owned ``hc connect`` child speaking plain line-mode JSON-RPC.

    Use as ``async with CoreSession() as core: await core.call(...)``. After any
    :class:`CoreSessionClosed` the session is finished; open a new one.
    """

    def __init__(
        self,
        binary: str = "hc",
        *,
        session: str | None = None,
        methods: frozenset[str] = DEFAULT_METHODS,
        max_line_bytes: int = MAX_LINE_BYTES,
        hello_timeout: float = 10.0,
    ) -> None:
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise ValueError("Core executable is required")
        if session is not None and (not session or len(session) > 128 or "\x00" in session):
            raise ValueError("Core session must be a nonempty name")
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        if "hello" in methods:
            raise ValueError("hello is sent only by start()")
        self.binary = binary
        self.session = session
        self.allowed_methods = frozenset(methods)
        self.max_line_bytes = max_line_bytes
        self.hello_timeout = hello_timeout
        self.generation: int | None = None
        self.core_version: str | None = None
        self.methods: frozenset[str] = frozenset()
        self.features: frozenset[str] = frozenset()
        self.close_reason: str | None = None
        self._proc: ChildProcess | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def closed(self) -> bool:
        """True once the session has been closed for any reason."""
        return self.close_reason is not None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in _SCRUBBED_ENV:
            env.pop(key, None)
        if self.session is not None:
            env["HC_SESSION"] = self.session
        return env

    async def __aenter__(self) -> CoreSession:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Start the child and perform ``hello``; on any failure no child is left."""
        if self._started:
            raise CoreSessionClosed("Core session was already started")
        self._started = True
        try:
            self._proc = await spawn_owned(
                self.binary,
                "connect",
                env=self._env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                limit=self.max_line_bytes + 1,
            )
        except BaseException as error:
            self.close_reason = f"start failed: {type(error).__name__}"
            raise
        try:
            result = await self._exchange(
                "hello",
                {"generation": PROTOCOL_GENERATION, "framed": False},
                self.hello_timeout,
            )
            self._accept_hello(result)
        except BaseException as error:
            await self._abort(f"hello failed: {type(error).__name__}")
            raise

    def _accept_hello(self, result: Any) -> None:
        if not isinstance(result, dict):
            raise CoreProtocolError("Core hello result is not an object")
        if result.get("framed") is not False:
            raise CoreProtocolError("Core hello did not confirm the plain line mode")
        generation = result.get("generation")
        if generation != PROTOCOL_GENERATION or isinstance(generation, bool):
            raise CoreProtocolError("Core speaks an unsupported protocol generation")
        version = result.get("version")
        if not isinstance(version, str) or len(version) > 64:
            raise CoreProtocolError("Core hello version is not bounded text")
        self.generation = PROTOCOL_GENERATION
        self.core_version = version
        self.methods = _string_list(result.get("methods"), "methods")
        self.features = _string_list(result.get("features", []), "features")

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 10.0,  # noqa: ASYNC109 — per-call deadline is the public API
    ) -> Any:
        """Send one request and return its ``result``.

        Raises :class:`CoreErrorReply` for a JSON-RPC error (session stays open),
        :class:`CoreSessionClosed` or a subclass when the session is or becomes
        unusable, and ``ValueError`` for a method outside the allow-list.
        """
        if method not in self.allowed_methods:
            raise ValueError(f"Core method {method!r} is not allowed on this session")
        if not timeout > 0:
            raise ValueError("timeout must be positive")
        async with self._lock:
            if self._proc is None or self.closed or self.generation is None:
                raise CoreSessionClosed(
                    f"Core session is closed: {self.close_reason or 'not started'}"
                )
            if method not in self.methods:
                raise ValueError(f"Core does not offer method {method!r}")
            try:
                return await self._exchange(method, dict(params or {}), timeout)
            except CoreErrorReply:
                raise
            except BaseException as error:
                await self._abort(f"{method} failed: {type(error).__name__}")
                raise

    async def _exchange(self, method: str, params: dict[str, Any], seconds: float) -> Any:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        request_id = self._next_id
        self._next_id += 1
        line = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        try:
            async with asyncio.timeout(seconds):
                try:
                    proc.stdin.write(line + b"\n")
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    raise CoreSessionClosed("Core connection closed while sending") from None
                while True:
                    message = await self._read(proc)
                    if "id" not in message and isinstance(message.get("method"), str):
                        continue  # A notification; nothing here subscribes, so ignore it.
                    return self._response(message, request_id, method)
        except TimeoutError:
            raise CoreCallTimeout(f"Core {method} gave no response in {seconds:g} s") from None

    async def _read(self, proc: ChildProcess) -> dict[str, Any]:
        assert proc.stdout is not None
        try:
            line = await proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise CoreProtocolError("Core sent a line over the size bound") from None
        if not line:
            raise CoreSessionClosed("Core connection ended")
        if not line.endswith(b"\n"):
            raise CoreSessionClosed("Core connection ended inside a line")
        if len(line) > self.max_line_bytes + 1:
            raise CoreProtocolError("Core sent a line over the size bound")
        return _decode(line)

    def _response(self, message: dict[str, Any], request_id: int, method: str) -> Any:
        if message.get("jsonrpc") != "2.0":
            raise CoreProtocolError("Core sent a line that is not JSON-RPC 2.0")
        if "method" in message:
            raise CoreProtocolError("Core sent a request; this session serves none")
        reply_id = message.get("id")
        if isinstance(reply_id, bool) or reply_id != request_id:
            raise CoreProtocolError("Core answered an unknown request id")
        if ("result" in message) == ("error" in message):
            raise CoreProtocolError("Core response must carry exactly one of result or error")
        if "result" in message:
            return message["result"]
        raise self._error(message["error"], method)

    @staticmethod
    def _error(error: Any, method: str) -> CoreErrorReply:
        if not isinstance(error, dict):
            raise CoreProtocolError("Core error is not an object")
        code, text, data = error.get("code"), error.get("message"), error.get("data")
        if isinstance(code, bool) or not isinstance(code, int) or not isinstance(text, str):
            raise CoreProtocolError("Core error has no code or message")
        kind = data.get("kind") if isinstance(data, dict) else None
        kind = _clean(kind) if isinstance(kind, str) else None
        cls = DocConflict if method == "doc.set" and kind == "conflict" else CoreErrorReply
        return cls(method, code, kind, _clean(text))

    async def close(self) -> None:
        """End the session: close stdin, give the child a moment, then kill and reap it."""
        if self.close_reason is None:
            self.close_reason = "closed"
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 1)
        except (TimeoutError, OSError):
            pass
        finally:
            await stop_child(proc, cleanup_seconds=2)

    async def _abort(self, reason: str) -> None:
        if self.close_reason is None:
            self.close_reason = reason
        proc, self._proc = self._proc, None
        if proc is not None:
            await stop_child(proc, cleanup_seconds=2)


def validate_doc_name(name: str) -> str:
    """Return ``name`` if it is an ``answerback`` document name Core also accepts."""
    if not isinstance(name, str) or not _DOC_NAME.fullmatch(name):
        raise DocRefused(
            f"Document names must start with {DOC_PREFIX!r} and use at most 32 of a-z 0-9 . _ -"
        )
    return name


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_UINT64:
        raise DocRefused("Document version must be an integer from 0 to 2**64-1")
    return value


def _result_version(value: Any, method: str) -> int:
    """Check a version read back from Core against the same 0..2**64-1 bound."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_UINT64:
        raise CoreProtocolError(f"Core {method} result has no valid version")
    return value


def _check_json(value: Any) -> None:
    """Walk ``value`` iteratively: plain JSON types, string keys, bounded depth."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DOC_DEPTH:
            raise DocRefused(f"Document value nests deeper than {MAX_DOC_DEPTH}")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise DocRefused("Document object keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise DocRefused("Document value holds a non-finite number")
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise DocRefused(f"Document value holds a non-JSON {type(item).__name__}")


def encode_doc_value(value: Any) -> bytes:
    """Serialise a document value compactly, refusing non-objects and values over 1 MiB."""
    if not isinstance(value, dict):
        raise DocRefused("Document values must be JSON objects")
    _check_json(value)
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_DOC_BYTES:
        raise DocRefused(f"Document value exceeds {MAX_DOC_BYTES} bytes")
    return encoded


class DocStore:
    """Versioned ``answerback*`` documents in the local Core, over a :class:`CoreSession`."""

    def __init__(self, session: CoreSession, *, timeout: float = 10.0) -> None:
        self.session = session
        self.timeout = timeout

    async def get(self, name: str) -> tuple[dict[str, Any] | None, int]:
        """Return ``(value, version)``; ``(None, version)`` when absent or deleted."""
        result = await self.session.call("doc.get", {"name": validate_doc_name(name)}, self.timeout)
        if not isinstance(result, dict) or "value" not in result:
            raise CoreProtocolError("Core doc.get result has no value")
        version = _result_version(result.get("version"), "doc.get")
        value = result["value"]
        if value is not None and not isinstance(value, dict):
            raise CoreProtocolError("Core document is not a JSON object")
        return value, version

    async def set(self, name: str, value: dict[str, Any], expected_version: int) -> int:
        """Write ``value`` if the document is still at ``expected_version``; return the new one.

        Raises :class:`DocConflict` when another writer got there first.
        """
        name = validate_doc_name(name)
        version = _version(expected_version)
        encode_doc_value(value)
        return await self._write(name, value, version)

    async def delete(self, name: str, expected_version: int) -> int:
        """Delete the value (Core keeps counting its version); return the new version."""
        return await self._write(validate_doc_name(name), None, _version(expected_version))

    async def _write(self, name: str, value: dict[str, Any] | None, version: int) -> int:
        result = await self.session.call(
            "doc.set", {"name": name, "value": value, "version": version}, self.timeout
        )
        new = result.get("version") if isinstance(result, dict) else None
        return _result_version(new, "doc.set")
