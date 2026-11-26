"""Bounded ACP v1 transport with explicit provider launch and session policies."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar, cast

from acp import Client, RequestError, connect_to_agent
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    NewSessionRequest,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallUpdate,
)
from pydantic import BaseModel

from . import __version__
from .process import BridgeError, ChildProcess, spawn_owned, stop_child

MAX_FRAME_BYTES = 1024 * 1024
MAX_PROMPT_CHARS = 200_000
REQUEST_TIMEOUT = 25.0
MCP_READINESS_TIMEOUT = 60.0
MCP_SESSION_TIMEOUT = 65.0
SHUTDOWN_TIMEOUT = 2.0
# The JSON-RPC code range _cause_text treats as a real 32-bit signed code (T-51 X1).
_MIN_RPC_CODE = -2147483648
_MAX_RPC_CODE = 2147483647
# ACP's own code for "authentication required" (acp.RequestError.auth_required).
# T-45 may take both constants into its catalogue; see acp_client.py's _cause_text.
ACP_AUTH_REQUIRED_CODE = -32000
ACP_AUTH_REQUIRED_TEXT = (
    f"RequestError {ACP_AUTH_REQUIRED_CODE} (in ACP this code means that "
    "authentication is required: sign in with the provider's own CLI, then "
    "check this receipt before you retry)"
)
_T = TypeVar("_T")


@dataclass(frozen=True)
class PromptResult:
    text: str
    stop_reason: str
    session_id: str


class BoundedACPReader(asyncio.StreamReader):
    """Keep SDK 1.0 from coalescing oversized NDJSON lines.

    The bound includes the newline. This StreamReader subclass satisfies the SDK's
    concrete admission check; only its two transport read methods are delegated.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        limit: int = MAX_FRAME_BYTES,
        on_error: Callable[[ConnectionError], None] | None = None,
        on_frame: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(limit=limit)
        self._reader = reader
        self._frame_limit = limit
        self._on_error = on_error
        self._on_frame = on_frame

    def _oversized(self) -> ConnectionError:
        error = ConnectionError(f"ACP frame exceeded the {self._frame_limit}-byte bound")
        if self._on_error is not None:
            self._on_error(error)
        return error

    async def readuntil(self, separator: Any = b"\n") -> bytes:
        # Delegate the runtime's separator forms, which expanded in Python 3.13.
        try:
            data = await self._reader.readuntil(separator)
        except asyncio.LimitOverrunError:
            raise self._oversized() from None
        except asyncio.IncompleteReadError as error:
            if len(error.partial) > self._frame_limit:
                raise self._oversized() from None
            raise
        if len(data) > self._frame_limit:
            raise self._oversized()
        if self._on_frame is not None:
            self._on_frame(data)
        return data

    async def readexactly(self, n: int) -> bytes:
        if n > self._frame_limit:
            raise self._oversized()
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as error:
            if len(error.partial) > self._frame_limit:
                raise self._oversized() from None
            raise


class ACPClient:
    """One ACP connection, with at most one prompt in flight.

    Loading a provider session does not attach to, or establish ownership of, an
    existing GUI. The caller is responsible for selecting sessions it may control.
    """

    expected_agent: str | None = None

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: Callable[[dict[str, Any]], Awaitable[None]],
        on_permission: Callable[[dict[str, Any]], Awaitable[str | None]],
    ) -> None:
        self._binary = binary
        self._cwd = str(Path(cwd).resolve())
        self._on_update = on_update
        self._on_permission = on_permission
        self._process: ChildProcess | None = None
        self.native_stopped: bool | None = None
        self._connection: ClientSideConnection | None = None
        self._failure: asyncio.Future[BridgeError] | None = None
        self._closed = False
        self._prompt_dispatched = False
        self._close_task: asyncio.Task[None] | None = None
        self._sessions: set[str] = set()
        self._opening = False
        self._active_session: str | None = None
        self._prompt_request_id: str | int | None = None
        self._early_messages: list[dict[str, Any]] = []
        self._early_bytes = 0
        self._collecting = False
        self._chunks: list[str] = []
        self._text_length = 0
        self._turn_cancelled = asyncio.Event()
        self.capabilities: dict[str, Any] = {}
        self.agent_info: dict[str, Any] = {}
        self.initialization: dict[str, Any] = {}

    def command(self) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str] | None:
        """Provider-specific child overrides; the default preserves inherited settings."""
        return None

    def validate_frame(self, data: bytes) -> None:
        """Optional provider validation of each bounded incoming NDJSON frame."""

    async def validate_session(self, session_id: str, response: BaseModel) -> None:
        """Provider-specific confirmation after a standard ACP session request."""

    def provider_message(self, message: dict[str, Any]) -> bool:
        return False

    def capture_message(
        self,
        message: dict[str, Any],
        *,
        collecting: bool,
        prompt_request_id: str | int | None,
    ) -> None:
        """Observe one inbound message synchronously at the prompt response boundary.

        Provider subclasses may override this hook to record bounded evidence in arrival
        order. ``collecting`` and ``prompt_request_id`` are snapshots from before the kernel
        processes ``message``. The hook must not retain message bodies or block.
        """

    def replay(self, params: dict[str, Any], update: dict[str, Any]) -> bool:
        return False

    async def __aenter__(self) -> Self:
        if self._closed or self._connection is not None:
            raise BridgeError("ACP client is already connected or closed")
        self._failure = asyncio.get_running_loop().create_future()
        try:
            self._process = await spawn_owned(
                *self.command(),
                cwd=self._cwd,
                env=self.environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                limit=MAX_FRAME_BYTES,
            )
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            reader = BoundedACPReader(
                self._process.stdout,
                MAX_FRAME_BYTES,
                lambda error: self._fail(BridgeError(str(error))),
                self.validate_frame,
            )
            self._connection = connect_to_agent(
                cast(Client, self),
                self._process.stdin,
                reader,
                receive_timeout=None,
                observers=[self._observe],
            )
            initialized = await self._call(
                self._connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="answerback", version=__version__),
                ),
                "initialize",
                max_seconds=REQUEST_TIMEOUT,
            )
            if initialized.protocol_version != 1:
                raise BridgeError("Agent did not negotiate ACP protocol version 1")
            self.initialization = self.serialize(initialized)
            self.capabilities = (
                self.serialize(initialized.agent_capabilities)
                if initialized.agent_capabilities is not None
                else {}
            )
            self.agent_info = (
                self.serialize(initialized.agent_info) if initialized.agent_info else {}
            )
            if (
                self.expected_agent is not None
                and self.agent_info.get("name") != self.expected_agent
            ):
                raise BridgeError(
                    f"The ACP server did not identify itself as {self.expected_agent}"
                )
            return self
        except BaseException as error:
            await self.close()
            if isinstance(error, (BridgeError, asyncio.CancelledError)):
                raise
            raise BridgeError(f"Unable to start ACP: {self._cause_text(error)}") from error

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        """Public reflection of the private _closed flag, for SessionControl."""
        return self._closed

    @property
    def prompt_dispatched(self) -> bool:
        """True once a session/prompt request was handed to the transport.

        Set in one place, ``_call``, directly before the request task is created. While
        it is False, no byte of any prompt has left this process. It is never reset:
        once a prompt may have been sent, any later failure of this client counts as
        sent. Callers classify a failed run with it (a provider's ACP worker runtime).
        """
        return self._prompt_dispatched

    def _connected(self) -> ClientSideConnection:
        if self._closed or self._connection is None:
            raise BridgeError("ACP client is not connected or is closed")
        if self._failure is not None and self._failure.done():
            raise self._failure.result()
        return self._connection

    @property
    def binary(self) -> str:
        """Configured ACP executable for provider command implementations."""
        return self._binary

    @property
    def prompt_in_flight(self) -> bool:
        """Whether this client currently owns an active prompt session."""
        return self._active_session is not None

    @staticmethod
    def serialize(model: BaseModel) -> dict[str, Any]:
        """Return the stable JSON representation used at provider extension hooks."""
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)

    @staticmethod
    def _cause_text(error: BaseException) -> str:
        """Reduce a cause to what may be shown: never its text, never its data.

        A ``BridgeError`` is the bridge's own reviewed text and may be shown as-is.
        Anything else came from outside -- the SDK, the OS, the provider or the
        store -- and is reduced to its class. An ``acp.RequestError``'s own
        ``code`` is shown too, plus for the one known authentication code, the
        bridge's own actionable sentence -- but only when that code is an
        ``int`` JSON-RPC could have meant: the SDK copies ``error_obj["code"]``
        into ``RequestError.code`` with no runtime check (``acp/connection.py``),
        so the wire can hand this property a string, a float, ``None``, a
        ``dict``, a ``list``, or a too-large integer; ``bool`` is excluded too,
        because it is a subclass of ``int``. Any such non-integer or
        out-of-range shape is reduced to an "unusable error code" marker with
        no part of the value, its type or its length disclosed (finding X1).
        The class is kept because nothing else records the cause: these
        modules have no logger.
        """
        if isinstance(error, BridgeError):
            return str(error)
        name = type(error).__name__
        if isinstance(error, RequestError):
            code = error.code
            if type(code) is not int or not (_MIN_RPC_CODE <= code <= _MAX_RPC_CODE):
                return f"{name} (unusable error code)"
            if code == ACP_AUTH_REQUIRED_CODE:
                return ACP_AUTH_REQUIRED_TEXT
            return f"{name} {code}"
        return name

    def _fail(self, error: BridgeError) -> None:
        if self._failure is not None and not self._failure.done():
            self._failure.set_result(error)
        self._turn_cancelled.set()

    async def _call(
        self,
        operation: Coroutine[Any, Any, _T],
        label: str,
        *,
        max_seconds: float | None,
        dispatches_prompt: bool = False,
    ) -> _T:
        assert self._failure is not None
        if dispatches_prompt:
            # The dispatch boundary (finding ACP-1): nothing before this line suspends,
            # and the only SDK code that ran (its synchronous compatibility wrapper)
            # returned the request coroutine without writing, queueing or registering
            # it, so no prompt byte can have left. From the task's first step on, the
            # write may have begun, and nobody outside the SDK can tell how much of it
            # did. Only ACPClient.prompt passes the flag.
            self._prompt_dispatched = True
        task = asyncio.create_task(operation)
        try:
            async with asyncio.timeout(max_seconds):
                done, _ = await asyncio.wait(
                    {task, self._failure}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._failure in done:
                    raise self._failure.result()
                return task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.close()
            if isinstance(error, BridgeError):
                raise
            if isinstance(error, TimeoutError):
                raise BridgeError(f"ACP {label} timed out; do not resubmit blindly") from error
            raise BridgeError(f"ACP {label} failed: {self._cause_text(error)}") from error
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        return await self._open_session(session_id, mcp_servers)

    async def _open_session(
        self,
        session_id: str | None,
        mcp_servers: list[dict[str, Any]] | None,
        *,
        metadata: dict[str, Any] | None = None,
        request_limit: float | None = None,
        on_ready: Callable[[str, BaseModel], Awaitable[None]] | None = None,
    ) -> str:
        connection = self._connected()
        if self._active_session is not None or self._opening:
            raise BridgeError("Cannot change ACP sessions during a prompt or another session open")
        if session_id is not None and self.capabilities.get("loadSession") is not True:
            raise BridgeError("The ACP agent does not advertise session/load")
        if session_id == "":
            raise BridgeError("ACP session ID must not be empty")
        try:
            definitions = NewSessionRequest(cwd=self._cwd, mcp_servers=mcp_servers or [])
        except ValueError as error:
            raise BridgeError(f"Invalid ACP MCP server definitions: {error}") from error
        metadata = metadata or {}
        request_limit = REQUEST_TIMEOUT if request_limit is None else request_limit
        self._opening = True
        try:
            if session_id is None:
                response = await self._call(
                    connection.new_session(
                        cwd=self._cwd, mcp_servers=definitions.mcp_servers, **metadata
                    ),
                    "session/new",
                    max_seconds=request_limit,
                )
                session_id = response.session_id
                if not session_id:
                    raise BridgeError("ACP returned an empty session ID")
            else:
                response = await self._call(
                    connection.load_session(
                        cwd=self._cwd,
                        session_id=session_id,
                        mcp_servers=definitions.mcp_servers,
                        **metadata,
                    ),
                    "session/load",
                    max_seconds=request_limit,
                )
            await self.validate_session(session_id, response)
            if on_ready is not None:
                await on_ready(session_id, response)
            self._sessions.add(session_id)
            return session_id
        except (BridgeError, asyncio.CancelledError):
            await self.close()
            raise
        finally:
            self._opening = False

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        connection = self._connected()
        lifecycle = self.capabilities.get("sessionCapabilities") or {}
        if not isinstance(lifecycle.get("list"), dict):
            raise BridgeError("The ACP agent does not advertise session/list")
        result = await self._call(
            connection.list_sessions(cwd=cwd, cursor=cursor),
            "session/list",
            max_seconds=REQUEST_TIMEOUT,
        )
        return self.serialize(result)

    async def prompt(self, session_id: str, message: str) -> PromptResult:
        connection = self._connected()
        if session_id not in self._sessions:
            raise BridgeError("ACP session must be opened before prompting")
        if self._opening:
            raise BridgeError("ACP session setup and MCP readiness must finish before prompting")
        if self._active_session is not None:
            raise BridgeError("An ACP prompt is already in flight")
        self._active_session = session_id
        self._turn_cancelled = asyncio.Event()
        self._chunks = []
        self._text_length = 0
        try:
            result = await self._call(
                connection.prompt(session_id=session_id, prompt=[TextContentBlock(text=message)]),
                "session/prompt",
                max_seconds=None,
                dispatches_prompt=True,
            )
            return PromptResult("".join(self._chunks), result.stop_reason, session_id)
        except asyncio.CancelledError:
            # A lost prompt response leaves execution uncertain; forbid connection reuse.
            await self.close()
            raise
        finally:
            self._turn_cancelled.set()
            self._collecting = False
            self._active_session = None
            self._prompt_request_id = None
            self._early_messages = []
            self._early_bytes = 0
            self._chunks = []

    def _observe(self, event: StreamEvent) -> None:
        """Capture arrival order before asynchronous update handlers can interleave."""
        message = event.message
        if event.direction == StreamDirection.OUTGOING:
            if message.get("method") == "session/prompt" and self._active_session is not None:
                self._prompt_request_id = message["id"]
                self._collecting = True
                early_messages, self._early_messages = self._early_messages, []
                self._early_bytes = 0
                for pending in early_messages:
                    self._capture_message(pending)
            return
        if self.provider_message(message):
            return
        if self._active_session is not None and self._prompt_request_id is None:
            # SDK send observers run after drain(). A fast peer can respond first;
            # retain that bounded arrival sequence until the SDK reveals the request ID.
            relevant = message.get("method") == "session/update" or (
                "method" not in message and "id" in message
            )
            if relevant:
                # surrogatepass: a message holding a lone UTF-16 surrogate (finding F3)
                # must be counted like any other text, not raise UnicodeEncodeError here
                # and vanish -- the SDK's observer dispatch swallows any exception from
                # this call. Byte count for ordinary text is unchanged either way.
                size = len(
                    json.dumps(message, ensure_ascii=False).encode("utf-8", errors="surrogatepass")
                )
                if len(self._early_messages) >= 64 or self._early_bytes + size > MAX_FRAME_BYTES:
                    self._fail(BridgeError("ACP early-response buffer exceeded its bound"))
                    return
                self._early_messages.append(message)
                self._early_bytes += size
            return
        self._capture_message(message)

    def _capture_message(self, message: dict[str, Any]) -> None:
        self.capture_message(
            message,
            collecting=self._collecting,
            prompt_request_id=self._prompt_request_id,
        )
        response_id = message.get("id")
        if (
            "method" not in message
            and type(response_id) is type(self._prompt_request_id)
            and response_id == self._prompt_request_id
        ):
            self._collecting = False
        if message.get("method") != "session/update" or not self._collecting:
            return
        params = message.get("params") or {}
        if not isinstance(params, dict) or params.get("sessionId") != self._active_session:
            self._fail(BridgeError("ACP update referenced an unexpected session"))
            return
        update = params.get("update")
        if not isinstance(update, dict):
            self._fail(BridgeError("ACP update was malformed"))
            return
        if self.replay(params, update):
            return
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text":
            return
        text = content.get("text")
        if not isinstance(text, str):
            self._fail(BridgeError("ACP text update was malformed"))
            return
        if self._text_length + len(text) > MAX_PROMPT_CHARS:
            self._fail(BridgeError(f"ACP answer exceeded the {MAX_PROMPT_CHARS}-character bound"))
            return
        self._text_length += len(text)
        self._chunks.append(text)

    async def session_update(self, session_id: str, update: BaseModel, **kwargs: Any) -> None:
        payload = self.serialize(update)
        payload["session_id"] = session_id
        try:
            await self._on_update(payload)
        except Exception as error:
            self._fail(BridgeError(f"ACP update callback failed: {self._cause_text(error)}"))

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        cancelled = RequestPermissionResponse(outcome=DeniedOutcome())
        if session_id != self._active_session:
            self._fail(BridgeError("ACP permission referenced an unexpected session"))
            return cancelled
        turn_cancelled = self._turn_cancelled
        if turn_cancelled.is_set():
            return cancelled
        allowed = {
            option.option_id: option
            for option in options
            if option.kind in {"allow_once", "reject_once"}
        }
        if len({option.option_id for option in options}) != len(options):
            self._fail(BridgeError("ACP permission supplied duplicate option IDs"))
            return cancelled
        if not allowed:
            return cancelled

        async def ask() -> str | None:
            return await self._on_permission(
                {
                    "session_id": session_id,
                    "tool_call": self.serialize(tool_call),
                    "options": [self.serialize(option) for option in allowed.values()],
                }
            )

        answer = asyncio.create_task(ask())
        cancellation = asyncio.create_task(turn_cancelled.wait())
        try:
            await asyncio.wait({answer, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if turn_cancelled.is_set() or answer.cancelled():
                return cancelled
            choice = answer.result()
            if choice is None:
                return cancelled
            if not isinstance(choice, str) or choice not in allowed:
                self._fail(BridgeError("Invalid or persistent ACP permission choice"))
                return cancelled
            # "AllowedOutcome" means a selection, including a reject_once selection.
            return RequestPermissionResponse(outcome=AllowedOutcome(option_id=choice))
        except Exception as error:
            self._fail(BridgeError(f"ACP permission callback failed: {self._cause_text(error)}"))
            return cancelled
        finally:
            answer.cancel()
            cancellation.cancel()
            await asyncio.gather(answer, cancellation, return_exceptions=True)

    async def cancel(self, session_id: str) -> None:
        connection = self._connected()
        if session_id not in self._sessions:
            raise BridgeError("ACP cancellation requires an opened session")
        if session_id == self._active_session:
            self._turn_cancelled.set()
        await self._call(
            connection.cancel(session_id=session_id), "session/cancel", max_seconds=REQUEST_TIMEOUT
        )

    async def ext_method(self, method: str, params: dict[str, Any]) -> Any:
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass

    async def read_text_file(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    async def create_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/kill")

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._turn_cancelled.set()
            self._close_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # Cancellation must not leave the owned agent running after this method returns.
            await self._close_task
            raise

    async def _shutdown(self) -> None:
        try:
            if self._connection is not None:
                with suppress(Exception):
                    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                        await self._connection.close()
        finally:
            if self._process is not None:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                await stop_child(self._process)
                self.native_stopped = True


def __getattr__(name: str) -> Any:
    """Resolve the historical KiroACP export from its provider package."""
    if name != "KiroACP":
        raise AttributeError(name)
    from importlib import import_module

    value = import_module("answerback_bridge.providers.kiro.acp").KiroACP
    globals()[name] = value
    return value
