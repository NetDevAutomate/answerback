"""Owned Grok Build ACP workers with explicit scope and terminal-result checks."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from acp.connection import StreamDirection, StreamEvent
from acp.schema import NewSessionRequest
from pydantic import BaseModel

from ...acp_client import REQUEST_TIMEOUT, ACPClient
from ...process import CLI_ENV_KEYS, BridgeError
from .protocol import NativeResult, PermissionCallback, UpdateCallback

MAX_CONNECTION_PROMPTS = 1024
GROK_ENV_KEYS = CLI_ENV_KEYS | {"XAI_API_KEY"}


def _mcp_manifest(servers: Any, cwd: str) -> list[dict[str, Any]]:
    """Compare definitions, not just names that could conceal an override."""
    if not isinstance(servers, list):
        raise BridgeError("Grok did not report a valid MCP server manifest")
    if any(
        not isinstance(server, dict)
        or not isinstance(server.get("name"), str)
        or not server["name"]
        for server in servers
    ):
        raise BridgeError("Grok MCP server definitions lack valid names")
    try:
        request = NewSessionRequest(cwd=cwd, mcp_servers=servers)
    except ValueError as error:
        raise BridgeError("Grok MCP server definitions were invalid") from error
    manifest = request.model_dump(mode="json", by_alias=True, exclude_none=True)["mcpServers"]
    # SDK 1.0 filters invalid rows instead of failing the containing request.
    if len(manifest) != len(servers):
        raise BridgeError("Grok MCP server definitions were discarded by schema validation")
    names = [server["name"] for server in manifest]
    if len(set(names)) != len(names):
        raise BridgeError("Grok MCP server definitions contain duplicate names")
    return sorted(manifest, key=lambda server: server["name"])


class _GrokACP(ACPClient):
    """Provider policy around the shared bounded ACP transport."""

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: UpdateCallback,
        on_permission: PermissionCallback,
        home: str,
    ) -> None:
        super().__init__(binary, cwd, on_update, on_permission)
        self.home = home
        self.socket_path: str | None = None
        self.version: str | None = None
        self.terminal_request_id: str | int | None = None
        self._terminal_ids: set[tuple[type, str | int]] = set()
        self._expected_mcp: list[dict[str, Any]] | None = None
        self._observed_mcp: list[dict[str, Any]] | None = None
        self._permission_ids: set[tuple[type, str | int]] = set()
        self._pending_permissions: set[tuple[type, str | int]] = set()
        self._permissions_drained = asyncio.Event()
        self._permissions_drained.set()

    def command(self) -> list[str]:
        if self.socket_path is None:
            raise BridgeError("Grok worker has no owned socket directory")
        return [self._binary, "agent", "--no-leader", "stdio", "--leader-socket", self.socket_path]

    def environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in GROK_ENV_KEYS if key in os.environ}
        environment["GROK_HOME"] = self.home
        environment["GROK_DISABLE_AUTOUPDATER"] = "1"
        for vendor in ("CURSOR", "CLAUDE"):
            for feature in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
                environment[f"GROK_{vendor}_{feature}_ENABLED"] = "0"
        return environment

    async def __aenter__(self) -> Self:
        try:
            await super().__aenter__()
            metadata = self.initialization.get("_meta")
            if (
                not isinstance(metadata, dict)
                or metadata.get("grokShell") is not True
                or not isinstance(metadata.get("agentVersion"), str)
                or not metadata["agentVersion"]
            ):
                raise BridgeError("ACP peer did not identify a supported Grok Build worker")
            native_cwd = metadata.get("currentWorkingDirectory")
            if not isinstance(native_cwd, str):
                raise BridgeError("Grok initialized without the selected workspace")
            resolved_cwd = await asyncio.to_thread(Path(native_cwd).resolve)
            if str(resolved_cwd) != self._cwd:
                raise BridgeError("Grok initialized outside the selected workspace")
            self.version = metadata["agentVersion"]
            if _mcp_manifest(metadata.get("mcpServers"), self._cwd):
                raise BridgeError(
                    "Grok inherited MCP servers; use an authenticated dedicated GROK_HOME profile"
                )
            # Only documented noninteractive methods may be selected automatically.
            # In particular, an advertised grok.com login can open a browser.
            methods = self.initialization.get("authMethods") or []
            method_ids = {method.get("id") for method in methods if isinstance(method, dict)}
            method_id = (
                "xai.api_key"
                if os.environ.get("XAI_API_KEY") and "xai.api_key" in method_ids
                else "cached_token"
                if "cached_token" in method_ids
                else None
            )
            if method_id is not None:
                await self._call(
                    self._connected().authenticate(method_id=method_id, headless=True),
                    "authenticate",
                    max_seconds=REQUEST_TIMEOUT,
                )
            return self
        except BaseException:
            await self.close()
            raise

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        if session_id is not None:
            raise BridgeError("Grok resume policy preservation is not yet qualified")
        if self._active_session is not None or self._opening:
            raise BridgeError("Cannot open a Grok session during another open or prompt")
        if self._sessions:
            raise BridgeError("Only one owned session may be opened per Grok worker")
        self._expected_mcp = _mcp_manifest(mcp_servers or [], self._cwd)
        return await self._open_session(
            None,
            mcp_servers,
            metadata={"yoloMode": False, "autoMode": False},
        )

    async def validate_session(self, session_id: str, response: BaseModel) -> None:
        if len(session_id) > 512:
            raise BridgeError("Grok returned an oversized session identifier")
        if session_id in self._sessions:
            raise BridgeError("Grok reused an admitted session identifier for a new session")
        if self._observed_mcp is None or self._observed_mcp:
            raise BridgeError("Grok did not report empty inherited MCP scope on this connection")

    def provider_message(self, message: dict[str, Any]) -> bool:
        method = message.get("method")
        if method == "session/request_permission" and "id" in message:
            ident = message["id"]
            if not isinstance(ident, (str, int)) or isinstance(ident, bool):
                self._fail(BridgeError("Grok permission has an invalid request identifier"))
                return False
            key = (type(ident), ident)
            if key in self._permission_ids or len(self._permission_ids) >= MAX_CONNECTION_PROMPTS:
                self._fail(
                    BridgeError("Grok permission request identifier was reused or over limit")
                )
                return False
            self._permission_ids.add(key)
            self._pending_permissions.add(key)
            self._permissions_drained.clear()
        if "id" in message and method is not None and method != "session/request_permission":
            self._fail(BridgeError(f"Grok requested unsupported interactive method {method!r}"))
            return False
        if method != "_x.ai/mcp/servers_updated":
            return False
        params = message.get("params")
        try:
            observed = _mcp_manifest(
                params.get("mcpServers") if isinstance(params, dict) else None, self._cwd
            )
            # The 1.0.77 native probe returned [] even with explicit ACP servers.
            # This is discovered configuration, not readiness of injected servers.
            # It can also arrive during initialization, before session admission.
            # The connection-wide report need not repeat for this worker's sole session.
            if observed:
                raise BridgeError("Grok reported inherited MCP servers outside the explicit scope")
            self._observed_mcp = observed
        except BridgeError as error:
            self._fail(error)
        return True

    def validate_frame(self, data: bytes) -> None:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member")
                result[key] = value
            return result

        def invalid_constant(value: str) -> Any:
            raise ValueError("non-JSON numeric constant")

        try:
            message = json.loads(
                data, object_pairs_hook=unique_object, parse_constant=invalid_constant
            )
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid JSON-RPC envelope")
            if "id" in message and type(message["id"]) not in (int, str):
                raise ValueError("invalid JSON-RPC request identifier")
            if "method" in message:
                if not isinstance(message["method"], str) or not message["method"]:
                    raise ValueError("invalid JSON-RPC method")
                if "result" in message or "error" in message:
                    raise ValueError("mixed request and response envelope")
            elif "id" not in message or ("result" in message) == ("error" in message):
                raise ValueError("invalid JSON-RPC response")
        except (ValueError, UnicodeError, RecursionError) as error:
            self._fail(BridgeError("Grok emitted a malformed ACP protocol frame"))
            raise ConnectionError("Grok emitted a malformed ACP protocol frame") from error

    def _observe(self, event: StreamEvent) -> None:
        message = event.message
        ident = message.get("id")
        if (
            event.direction == StreamDirection.OUTGOING
            and "method" not in message
            and type(ident) in (str, int)
        ):
            self._pending_permissions.discard((type(ident), ident))
            if not self._pending_permissions:
                self._permissions_drained.set()
        if (
            event.direction == StreamDirection.OUTGOING
            and event.message.get("id") is None
            and isinstance(event.message.get("error"), dict)
            and event.message["error"].get("code") in {-32700, -32600}
        ):
            self._fail(BridgeError("Grok emitted a malformed ACP protocol frame"))
        super()._observe(event)

    def _capture_message(self, message: dict[str, Any]) -> None:
        ident = message.get("id")
        if (
            "method" not in message
            and isinstance(ident, (str, int))
            and not isinstance(ident, bool)
        ):
            key = (type(ident), ident)
            if key in self._terminal_ids:
                self._fail(BridgeError("Grok repeated an ACP prompt terminal response"))
                return
            if type(ident) is type(self._prompt_request_id) and ident == self._prompt_request_id:
                self._terminal_ids.add(key)
                self.terminal_request_id = ident
        super()._capture_message(message)

    async def session_update(self, session_id: str, update: BaseModel, **kwargs: Any) -> None:
        # Setup/history notifications cannot become task progress or task output.
        if self._active_session is not None:
            await super().session_update(session_id, update, **kwargs)

    async def drain_permissions(self) -> None:
        self._connected()
        await self._call(
            self._permissions_drained.wait(),
            "pending permission response drain",
            max_seconds=REQUEST_TIMEOUT,
        )


class GrokCLI:
    """Native transport for new, bridge-owned Grok Build sessions.

    The existing native authentication profile is used in place. No credentials
    are read or copied by this adapter. Saved-session policy preservation has not
    been qualified, so the worker layer must start a new session for each task.
    """

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: UpdateCallback,
        on_permission: PermissionCallback,
        *,
        home: str | Path | None = None,
    ) -> None:
        selected_home = home if home is not None else os.environ.get("GROK_HOME")
        self.home = str(Path(selected_home or Path.home() / ".grok").expanduser().resolve())
        self._on_update = on_update
        self._client = _GrokACP(binary, cwd, on_update, on_permission, self.home)
        self._socket_directory: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False
        self._active_prompt: str | None = None
        self._prompt_ids: set[str] = set()

    async def __aenter__(self) -> Self:
        if self._closed or self._socket_directory is not None:
            raise BridgeError("Grok worker is already connected or closed")
        self._socket_directory = tempfile.TemporaryDirectory(prefix="ab-grok-")
        self._client.socket_path = str(Path(self._socket_directory.name) / "leader.sock")
        try:
            await self._client.__aenter__()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        return await self._client.open_session(session_id, mcp_servers)

    async def prompt(self, session_id: str, message: str, *, prompt_id: str) -> NativeResult:
        if (
            not isinstance(prompt_id, str)
            or not prompt_id
            or len(prompt_id) > 512
            or prompt_id in self._prompt_ids
            or len(self._prompt_ids) >= MAX_CONNECTION_PROMPTS
            or self._active_prompt is not None
        ):
            raise BridgeError(
                "Grok prompt identity is invalid, reused, or another prompt is active"
            )
        self._prompt_ids.add(prompt_id)
        self._active_prompt = prompt_id
        self._client.terminal_request_id = None
        try:
            result = await self._client.prompt(session_id, message)
            ident = self._client.terminal_request_id
            if ident is None:
                raise BridgeError("Grok returned without a correlated terminal response")
            await self._on_update(
                {
                    "sessionUpdate": "native_terminal_observed",
                    "session_id": session_id,
                    "turn_id": str(ident),
                }
            )
            await self._client.drain_permissions()
            text = result.text if result.text.strip() else None
            status = (
                "cancelled"
                if result.stop_reason == "cancelled"
                else "completed"
                if result.stop_reason == "end_turn"
                else "failed"
            )
            return NativeResult(
                session_id=session_id,
                status=status,
                text=text,
                turn_id=str(ident),
                evidence={
                    "protocol": "acp",
                    "protocol_version": 1,
                    "provider_version": self._client.version,
                    "profile_home": self.home,
                    "prompt_id": prompt_id,
                    "rpc_request_id": ident,
                    "stop_reason": result.stop_reason,
                    "final_answer_available": text is not None,
                    "mcp_connection_readiness_verified": False,
                    "resumable": False,
                },
            )
        finally:
            self._active_prompt = None

    @property
    def closed(self) -> bool:
        """True once this wrapper or its inner client reports itself closed.

        ``_call`` closes the inner ``_GrokACP`` on an RPC failure before this
        wrapper's own ``close()`` ever runs (T-51 X4): during that inner close,
        which waits for the provider process to end, the wrapper's own
        ``_closed`` flag is still false. Without reading through, a cancel
        arriving in that window calls ``ACPWorkerRuntime.cancel`` -> this
        wrapper's own ``cancel()`` -> the inner client's ``cancel()``, which
        raises ``BridgeError("ACP client is not connected or is closed")``,
        although the owner task is already on its way to settle the receipt.
        """
        return self._closed or self._client.closed

    @property
    def native_stopped(self) -> bool | None:
        """The shared ACP client owns Grok's isolated process group."""
        return self._client.native_stopped

    async def cancel(self, session_id: str) -> None:
        await self._client.cancel(session_id)

    async def close(self) -> None:
        self._closed = True
        try:
            await self._client.close()
        finally:
            if self._socket_directory is not None:
                self._socket_directory.cleanup()
