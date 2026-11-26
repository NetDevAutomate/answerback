"""Owned Codex app-server transport, separate from Desktop queue/history access.

The caller must authorize every resumed ID through its durable ownership registry.
A source label or discovered native thread never establishes that ownership.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn, Self, TypeVar

from ... import __version__
from ...process import BridgeError, ChildProcess, codex_env, spawn_owned, stop_child
from .protocol import NativeResult, PermissionCallback, UpdateCallback

MAX_FRAME_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_EARLY_EVENTS = 64
REQUEST_TIMEOUT = 25.0
SHUTDOWN_TIMEOUT = 2.0
_T = TypeVar("_T")


class _ProtocolEOF(BridgeError):
    """Clean stream exhaustion can follow a fully correlated terminal event."""


@dataclass
class _Turn:
    session_id: str
    prompt_id: str
    result: asyncio.Future[NativeResult]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    turn_id: str | None = None
    submitted: bool = False
    cancel_requested: bool = False
    early: list[dict[str, Any]] = field(default_factory=list)
    early_bytes: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    permission_ids: set[tuple[type, str | int]] = field(default_factory=set)


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BridgeError(f"Codex {label} is invalid")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError(f"Codex {label} is malformed")
    return value


def _request_key(value: Any) -> tuple[type, str | int]:
    if type(value) is str and 0 < len(value) <= 512:
        return str, value
    if type(value) is int and -(2**63) <= value < 2**63:
        return int, value
    raise BridgeError("Codex request ID is invalid")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError("Codex protocol contains duplicate object keys")
        result[key] = value
    return result


def _finite_json(value: str) -> None:
    raise BridgeError("Codex protocol contains invalid non-finite JSON")


def _mcp_definitions(servers: list[dict[str, Any]] | None, cwd: str) -> dict[str, Any]:
    """Translate complete ACP stdio definitions; never merge inherited MCP maps."""
    if servers is None:
        return {}
    if not isinstance(servers, list) or len(servers) > 16:
        raise BridgeError("Codex MCP definitions exceed the supported bound")
    native: dict[str, Any] = {}
    for server in servers:
        server = _object(server, "MCP definition")
        name = server.get("name")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None
            or name in native
        ):
            raise BridgeError("Codex MCP names must be valid and unique")
        if server.get("type", "stdio") != "stdio" or set(server) - {
            "name",
            "type",
            "command",
            "args",
            "env",
            "_meta",
        }:
            raise BridgeError("Unsupported Codex MCP definition; only explicit stdio is supported")
        command = server.get("command")
        args = server.get("args", [])
        env = server.get("env", [])
        if (
            not isinstance(command, str)
            or not Path(command).is_absolute()
            or "\0" in command
            or not isinstance(args, list)
            or any(not isinstance(arg, str) or "\0" in arg for arg in args)
            or not isinstance(env, list)
        ):
            raise BridgeError("Codex MCP command, args or environment is invalid")
        environment: dict[str, str] = {}
        for entry in env:
            entry = _object(entry, "MCP environment")
            key, value = entry.get("name"), entry.get("value")
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or key in environment
                or not isinstance(value, str)
                or "\0" in value
                or set(entry) != {"name", "value"}
            ):
                raise BridgeError("Codex MCP environment is invalid or duplicated")
            environment[key] = value
        native[name] = {
            "command": command,
            "args": args,
            "env": environment,
            "cwd": cwd,
            "enabled": True,
            "required": True,
        }
    if len(json.dumps(native).encode()) > 32_000:
        raise BridgeError("Codex MCP definitions exceed the supported bound")
    return native


class CodexCLI:
    """One owned stdio process with one prompt in flight and normal approvals."""

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: UpdateCallback,
        on_permission: PermissionCallback,
        *,
        home: str | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = str(Path(cwd).resolve())
        self._home = home
        self._on_update = on_update
        self._on_permission = on_permission
        self._process: ChildProcess | None = None
        self.native_stopped: bool | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._failure: asyncio.Future[BridgeError] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._sessions: set[str] = set()
        self._used_turns: set[str] = set()
        self._active: _Turn | None = None
        self._opening = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._permissions: dict[tuple[type, str | int], asyncio.Task[None]] = {}

    async def __aenter__(self) -> Self:
        if self._process is not None or self._closed:
            raise BridgeError("Codex transport is already connected or closed")
        self._failure = asyncio.get_running_loop().create_future()
        try:
            environment = codex_env(self._home)
            # Desktop identity must not override the owned native client's originator.
            for key in (
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                "CODEX_SESSION_ID",
                "CODEX_THREAD_ID",
            ):
                environment.pop(key, None)
            self._process = await spawn_owned(
                self._binary,
                "app-server",
                "--stdio",
                "-c",
                "features.apps=false",
                "-c",
                "features.plugins=false",
                "-c",
                "mcp_servers={}",
                cwd=self._cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME_BYTES,
                start_new_session=os.name == "posix",
            )
            self._reader = asyncio.create_task(self._read())
            self._stderr_reader = asyncio.create_task(self._read_stderr())
            await self._call(
                "initialize",
                {
                    "clientInfo": {"name": "answerback-native-cli", "version": __version__},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self._send({"method": "initialized", "params": {}})
            return self
        except BaseException as error:
            await self.close()
            if isinstance(error, (BridgeError, asyncio.CancelledError)):
                raise
            raise BridgeError(f"Unable to start owned Codex: {error}") from error

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def _connected(self) -> None:
        if self._closed or self._process is None:
            raise BridgeError("Codex transport is not connected or is closed")
        if self._failure is not None and self._failure.done():
            raise self._failure.result()

    def _fail(self, error: BridgeError) -> None:
        if self._failure is not None and not self._failure.done():
            self._failure.set_result(error)

    async def _wait(self, future: asyncio.Future[_T]) -> _T:
        assert self._failure is not None
        done, _ = await asyncio.wait({future, self._failure}, return_when=asyncio.FIRST_COMPLETED)
        if self._failure in done:
            error = self._failure.result()
            # Only clean EOF is compatible with an already received result.
            # Conflicting terminal events and every other integrity failure win.
            if future not in done or not isinstance(error, _ProtocolEOF):
                raise error
        return future.result()

    async def _send(self, message: dict[str, Any]) -> None:
        self._connected()
        encoded = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_FRAME_BYTES:
            raise BridgeError("Codex outgoing frame exceeded its bound")
        assert self._process is not None and self._process.stdin is not None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self._write_lock:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
        except (OSError, TimeoutError) as error:
            failure = BridgeError("Codex write failed; delivery may be uncertain")
            self._fail(failure)
            raise failure from error

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._connected()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self._send({"id": request_id, "method": method, "params": params})
                response = await self._wait(future)
            if "error" in response:
                error = _object(response["error"], "RPC error")
                raise BridgeError(
                    f"Codex {method} rejected: {str(error.get('message', ''))[:2000]}"
                )
            return _object(response.get("result"), f"{method} result")
        except TimeoutError as error:
            failure = BridgeError(f"Codex {method} timed out; delivery may be uncertain")
            self._fail(failure)
            raise failure from error
        finally:
            self._pending.pop(request_id, None)
            future.cancel()

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                if len(line) > MAX_FRAME_BYTES:
                    raise BridgeError("Codex incoming frame exceeded its bound")
                message = _object(
                    json.loads(line, object_pairs_hook=_unique_pairs, parse_constant=_finite_json),
                    "message",
                )
                await self._dispatch(message)
            if not self._closed:
                self._fail(_ProtocolEOF("Codex protocol EOF; unfinished delivery is uncertain"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(
                error if isinstance(error, BridgeError) else BridgeError(f"Codex protocol: {error}")
            )

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            self._stderr.extend(chunk)
            del self._stderr[:-4000]

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            _request_key(message["id"])
        if "method" not in message:
            request_id = message.get("id")
            if type(request_id) is int and request_id in self._pending:
                if ("result" in message) == ("error" in message):
                    raise BridgeError("Codex response must contain one result or error")
                future = self._pending[request_id]
                if not future.done():
                    future.set_result(message)
            return
        if not isinstance(message["method"], str):
            raise BridgeError("Codex notification method is malformed")
        if "id" in message:
            await self._permission_request(message)
            return
        await self._notification(message)

    def _buffer(self, state: _Turn, message: dict[str, Any]) -> None:
        state.early_bytes += len(json.dumps(message).encode())
        if len(state.early) >= MAX_EARLY_EVENTS or state.early_bytes > MAX_FRAME_BYTES:
            raise BridgeError("Codex early event buffer exceeded its bound")
        state.early.append(message)

    async def _unsupported_request(self, message: dict[str, Any], reason: str) -> NoReturn:
        await self._send({"id": message["id"], "error": {"code": -32601, "message": reason}})
        raise BridgeError(reason)

    async def _permission_request(self, message: dict[str, Any]) -> None:
        state = self._active
        params = _object(message.get("params"), "permission params")
        if state is None or not state.submitted or params.get("threadId") != state.session_id:
            await self._unsupported_request(message, "Unsupported Codex permission session")
        assert state is not None
        if state.turn_id is None:
            self._buffer(state, message)
            return
        if params.get("turnId") != state.turn_id or state.result.done():
            await self._unsupported_request(message, "Unsupported stale Codex permission turn")
        method = message["method"]
        is_command = method == "item/commandExecution/requestApproval"
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        } or (
            params.get("kind", "command") not in {"command", "writeStdin"}
            or params.get("networkApprovalContext") is not None
            or params.get("additionalPermissions") is not None
            or params.get("grantRoot") is not None
        ):
            await self._unsupported_request(
                message, "Unsupported or broad Codex permission request"
            )
        if state.cancel_requested:
            # The user has already cancelled the turn; no new approval may revive it.
            await self._send({"id": message["id"], "result": {"decision": "cancel"}})
            return
        key = _request_key(message["id"])
        if (
            key in state.permission_ids
            or len(self._permissions) >= 8
            or len(state.permission_ids) >= 128
        ):
            await self._unsupported_request(
                message, "Unsupported duplicate or excessive permission"
            )
        state.permission_ids.add(key)
        item_id = _identity(params.get("itemId"), "permission item ID")
        item = state.items.get(item_id, {})
        raw = dict(params)
        if is_command:
            command = params.get("command") or item.get("command")
            if not isinstance(command, str) or not command:
                await self._unsupported_request(message, "Unsupported command without review text")
            title = command
        else:
            if item.get("type") != "fileChange" or not isinstance(item.get("changes"), list):
                await self._unsupported_request(message, "Unsupported file changes without preview")
            raw["changes"] = item["changes"]
            title = "Apply the displayed file changes"
        allowed = {"accept", "decline", "cancel"}
        decisions = params.get("availableDecisions")
        if decisions is not None:
            if not isinstance(decisions, list):
                await self._unsupported_request(message, "Unsupported Codex permission decisions")
            allowed &= {value for value in decisions if isinstance(value, str)}
        if not allowed:
            await self._unsupported_request(message, "Unsupported broad Codex permission decisions")
        request = {
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "native_request_id": message["id"],
            "tool_call": {
                "toolCallId": item_id,
                "kind": "execute" if is_command else "edit",
                "title": title[:1000],
                "rawInput": raw,
            },
            "options": [
                {"optionId": choice, "name": label, "kind": kind}
                for choice, label, kind in [
                    ("accept", "Allow once", "allow_once"),
                    ("decline", "Decline", "reject_once"),
                    ("cancel", "Cancel turn", "reject_once"),
                ]
                if choice in allowed
            ],
        }
        if len(json.dumps(request).encode()) > 32_000:
            await self._unsupported_request(
                message, "Unsupported oversized Codex permission preview"
            )

        async def ask() -> None:
            try:
                answer = await self._on_permission(request)
                if (
                    self._active is not state
                    or state.result.done()
                    or state.cancel_requested
                    or key not in self._permissions
                ):
                    return
                choice = "cancel" if answer is None else answer
                if not isinstance(choice, str) or choice not in allowed:
                    raise BridgeError("Invalid or persistent Codex permission choice")
                await self._send({"id": message["id"], "result": {"decision": choice}})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._fail(
                    error
                    if isinstance(error, BridgeError)
                    else BridgeError("Codex permission callback failed")
                )
            finally:
                self._permissions.pop(key, None)

        self._permissions[key] = asyncio.create_task(ask())

    def _clear_permissions(self) -> list[asyncio.Task[None]]:
        tasks = list(self._permissions.values())
        self._permissions.clear()
        for task in tasks:
            task.cancel()
        return tasks

    async def _notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        state = self._active
        if state is None or not state.submitted:
            return
        params = _object(message.get("params"), "notification params")
        if params.get("threadId") != state.session_id:
            return
        if state.turn_id is None:
            self._buffer(state, message)
            return
        if method == "serverRequest/resolved":
            task = self._permissions.pop(_request_key(params.get("requestId")), None)
            if task is not None:
                task.cancel()
            return
        turn = params.get("turn", {})
        event_turn = turn.get("id") if isinstance(turn, dict) else None
        if params.get("turnId", event_turn) != state.turn_id:
            return
        if method in {"item/started", "item/completed"}:
            item = _object(params.get("item"), "item")
            item_id = _identity(item.get("id"), "item ID")
            if method == "item/completed":
                state.items.pop(item_id, None)
                self._answer_item(state, item)
            elif item.get("type") in {"commandExecution", "fileChange"}:
                state.items[item_id] = item
                if len(state.items) > 64 or len(json.dumps(state.items).encode()) > MAX_FRAME_BYTES:
                    raise BridgeError("Codex pending item previews exceeded their bound")
        elif method == "turn/completed":
            native_turn = _object(turn, "completed turn")
            native_status = native_turn.get("status")
            if native_status not in {"completed", "failed", "interrupted"}:
                raise BridgeError("Codex terminal event has a nonterminal status")
            items = native_turn.get("items")
            if not isinstance(items, list):
                raise BridgeError("Codex completed turn items are malformed")
            for item in items:
                self._answer_item(state, _object(item, "turn item"))
            status: Literal["completed", "failed", "cancelled"] = "completed"
            if native_status == "failed":
                status = "failed"
            elif native_status == "interrupted":
                status = "cancelled"
            text = "\n\n".join(state.answers.values())
            result = NativeResult(
                state.session_id,
                status,
                text if text.strip() else None,
                state.turn_id,
                {
                    "protocol": "codex-app-server-v2",
                    "terminal_method": method,
                    "native_status": native_status,
                    "prompt_id": state.prompt_id,
                    "error": native_turn.get("error"),
                    "completed_at": native_turn.get("completedAt"),
                },
            )
            if state.result.done():
                if state.result.result() != result:
                    raise BridgeError("Codex sent conflicting terminal events")
                return
            await self._on_update(
                {"sessionUpdate": method, "session_id": state.session_id, "turn_id": state.turn_id}
            )
            self._clear_permissions()
            state.result.set_result(result)
            return
        await self._on_update(
            {"sessionUpdate": method, "session_id": state.session_id, "turn_id": state.turn_id}
        )

    @staticmethod
    def _answer_item(state: _Turn, item: dict[str, Any]) -> None:
        if item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
            return
        item_id = _identity(item.get("id"), "answer item ID")
        text = item.get("text")
        if not isinstance(text, str):
            raise BridgeError("Codex final answer text is malformed")
        if item_id in state.answers and state.answers[item_id] != text:
            raise BridgeError("Codex sent conflicting final answer items")
        state.answers[item_id] = text
        if sum(map(len, state.answers.values())) > MAX_TEXT_CHARS or len(state.answers) > 64:
            raise BridgeError("Codex final answer exceeded its bound")

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        self._connected()
        if self._active is not None or self._opening:
            raise BridgeError("Cannot change Codex session while work is active")
        if session_id is not None:
            _identity(session_id, "session ID")
        native_mcp = _mcp_definitions(mcp_servers, self._cwd)
        self._opening = True
        try:
            params: dict[str, Any] = {
                "cwd": self._cwd,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": "workspace-write",
                "serviceName": "answerback-native-cli",
                "config": {
                    "mcp_servers": native_mcp,
                    "features.apps": False,
                    "features.plugins": False,
                },
            }
            if session_id is None:
                params["ephemeral"] = False
            else:
                params.pop("serviceName")
                params.update(threadId=session_id, excludeTurns=True)
            response = await self._call(
                "thread/start" if session_id is None else "thread/resume", params
            )
            thread = _object(response.get("thread"), "opened thread")
            opened = _identity(thread.get("id"), "opened thread ID")
            if session_id is not None and opened != session_id:
                raise BridgeError("Codex resumed a different session")
            if _object(thread.get("status"), "thread status").get("type") != "idle":
                raise BridgeError("Codex opened thread is not idle")
            if response.get("cwd") != self._cwd or thread.get("cwd") != self._cwd:
                raise BridgeError("Codex opened a different working directory")
            if (
                response.get("approvalPolicy") != "on-request"
                or response.get("approvalsReviewer") != "user"
            ):
                raise BridgeError("Codex approval policy differs from requested human review")
            sandbox = _object(response.get("sandbox"), "sandbox policy")
            roots = sandbox.get("writableRoots", [])
            if (
                sandbox.get("type") != "workspaceWrite"
                or sandbox.get("networkAccess", False) is not False
                or not isinstance(roots, list)
                or any(root != self._cwd for root in roots)
            ):
                raise BridgeError("Codex sandbox scope differs from requested project")
            # Scope comes from the complete per-thread config replacement above.
            # Status/list is discovery with global fallback, even with threadId.
            # Query only requested names for readiness; an empty map needs no
            # discovery. These reads never establish absence of inherited servers.
            for name in native_mcp:
                readiness = await self._call(
                    "mcpServerStatus/list",
                    {
                        "threadId": opened,
                        "serverName": name,
                        "limit": 100,
                        "detail": "toolsAndAuthOnly",
                    },
                )
                rows = readiness.get("data")
                if not isinstance(rows, list) or len(rows) != 1:
                    raise BridgeError("Codex MCP readiness did not identify the requested server")
                row = _object(rows[0], "MCP readiness entry")
                if (
                    row.get("name") != name
                    or row.get("runtimeStatus") != "connected"
                    or readiness.get("nextCursor") is not None
                ):
                    raise BridgeError("Codex requested MCP server is not verifiably ready")
            self._sessions.add(opened)
            return opened
        finally:
            self._opening = False

    async def prompt(self, session_id: str, message: str, *, prompt_id: str) -> NativeResult:
        self._connected()
        if session_id not in self._sessions:
            raise BridgeError("Codex prompt requires an opened session")
        if self._active is not None or self._opening:
            raise BridgeError("Codex already has active work")
        _identity(prompt_id, "prompt ID")
        if not isinstance(message, str) or not message or len(message) > MAX_TEXT_CHARS:
            raise BridgeError("Codex prompt is empty or exceeds its bound")
        state = _Turn(session_id, prompt_id, asyncio.get_running_loop().create_future())
        self._active = state
        try:
            metadata = await self._call("thread/read", {"threadId": session_id})
            thread = _object(metadata.get("thread"), "thread metadata")
            if thread.get("id") != session_id or thread.get("status") != {"type": "idle"}:
                raise BridgeError("Codex thread is not idle; refusing to steer active work")
            state.submitted = True
            response = await self._call(
                "turn/start",
                {
                    "threadId": session_id,
                    "clientUserMessageId": prompt_id,
                    "input": [{"type": "text", "text": message}],
                    "cwd": self._cwd,
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandboxPolicy": {
                        "type": "workspaceWrite",
                        "writableRoots": [self._cwd],
                        "networkAccess": False,
                    },
                },
            )
            turn = _object(response.get("turn"), "started turn")
            state.turn_id = _identity(turn.get("id"), "turn ID")
            if state.turn_id in self._used_turns:
                raise BridgeError("Codex reused a turn ID; refusing ambiguous steering")
            self._used_turns.add(state.turn_id)
            state.ready.set()
            early, state.early = state.early, []
            for event in early:
                await self._dispatch(event)
            return await self._wait(state.result)
        except BaseException:
            if state.submitted and not state.result.done():
                self._fail(BridgeError("Codex prompt is unsettled; delivery remains uncertain"))
            raise
        finally:
            state.ready.set()
            self._active = None
            await asyncio.gather(*self._clear_permissions(), return_exceptions=True)

    @property
    def closed(self) -> bool:
        """Public reflection of the private _closed flag, for SessionControl."""
        return self._closed

    async def cancel(self, session_id: str) -> None:
        self._connected()
        state = self._active
        if session_id not in self._sessions or state is None or state.session_id != session_id:
            raise BridgeError("Codex cancellation requires an opened active session")
        state.cancel_requested = True
        permissions = self._clear_permissions()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await state.ready.wait()
            if state.turn_id is None:
                raise BridgeError("Codex native turn ID is unavailable; cancellation is unverified")
            if not state.result.done():
                await self._call(
                    "turn/interrupt", {"threadId": session_id, "turnId": state.turn_id}
                )
        finally:
            await asyncio.gather(*permissions, return_exceptions=True)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._fail(BridgeError("Codex transport closed; unfinished delivery is uncertain"))
            self._close_task = asyncio.create_task(self._shutdown())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        permissions = self._clear_permissions()
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            try:
                async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                    await self._process.wait()
            except TimeoutError:
                pass
            # The server may exit while descendants remain in its owned process group.
            await stop_child(self._process, cleanup_seconds=SHUTDOWN_TIMEOUT)
            self.native_stopped = True
        tasks = [
            task for task in (self._reader, self._stderr_reader, *permissions) if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
