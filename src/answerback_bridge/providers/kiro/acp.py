"""Kiro CLI ACP launch, trust, and MCP-readiness policy."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from ... import acp_client as shared_acp
from ...acp_client import ACPClient
from ...process import BridgeError
from .config import KIRO_TOOL_NAME, validate_kiro_trust_tools


@dataclass(frozen=True)
class _MCPServerStatus:
    status: str
    enabled_tools: frozenset[str]
    # False for the v2 engine's `_kiro.dev/mcp/server_initialized`, which names the
    # server and nothing else: the required tools are then not verified by the bridge.
    verified: bool = True


class _MCPReadiness:
    """Keep only required-server readiness from Kiro's MCP notifications.

    The v3 engine sends `_kiro/mcp/status`, a full snapshot of every server with its
    status and tools; `observe` keeps the required rows and `ready` verifies the required
    tools are enabled. The v2 engine sends `_kiro.dev/mcp/server_initialized` per server,
    with no tool list; `observe_initialized` records the server as connected and
    unverified, and `ready` accepts it, naming it in `unverified` for the receipt.
    """

    def __init__(self, required: dict[str, set[str]], session_id: str | None) -> None:
        self.required = {name: frozenset(tools) for name, tools in required.items()}
        self.session_id = session_id
        self.snapshots: dict[str, dict[str, _MCPServerStatus]] = {}
        self.changed = asyncio.Event()
        self.unverified: set[str] = set()

    def _session_of(self, params: Any) -> str | None:
        if not isinstance(params, dict):
            return None
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            return None
        if self.session_id is not None and session_id != self.session_id:
            return None
        if session_id not in self.snapshots and len(self.snapshots) >= 8:
            raise BridgeError("ACP MCP candidate session count exceeded its bound")
        return session_id

    def observe_initialized(self, params: Any) -> None:
        session_id = self._session_of(params)
        if session_id is None:
            return
        name = params.get("serverName")
        if not isinstance(name, str) or name not in self.required:
            return
        snapshot = dict(self.snapshots.get(session_id, {}))
        snapshot[name] = _MCPServerStatus("connected", frozenset(), verified=False)
        self.snapshots[session_id] = snapshot
        self.changed.set()

    def observe(self, params: Any) -> None:
        servers = params.get("servers") if isinstance(params, dict) else None
        if not isinstance(servers, list):
            return
        session_id = self._session_of(params)
        if session_id is None:
            return
        snapshot: dict[str, _MCPServerStatus] = {}
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = server.get("name")
            if not isinstance(name, str) or name not in self.required:
                continue
            status = server.get("status")
            if status not in ("connecting", "connected", "failed", "disabled"):
                status = "unknown"
            if name in snapshot:
                snapshot[name] = _MCPServerStatus("unknown", frozenset())
                continue
            tools = server.get("tools")
            enabled = frozenset(
                tool["name"]
                for tool in (tools if isinstance(tools, list) else [])
                if isinstance(tool, dict)
                and tool.get("disabled") is False
                and isinstance(tool.get("name"), str)
                and tool["name"] in self.required[name]
            )
            snapshot[name] = _MCPServerStatus(status, enabled)
        # Missing rows remove earlier state: Kiro sends replacements, not patches.
        self.snapshots[session_id] = snapshot
        self.changed.set()

    def bind(self, session_id: str) -> None:
        self.session_id = session_id
        self.snapshots = {session_id: self.snapshots.get(session_id, {})}

    def ready(self) -> bool:
        snapshot = self.snapshots.get(self.session_id or "", {})
        ready = True
        for name, required_tools in self.required.items():
            server = snapshot.get(name)
            if server is None:
                ready = False
                continue
            if server.status in {"failed", "disabled"}:
                raise BridgeError(f"ACP MCP server {name!r} is {server.status}")
            if server.status != "connected":
                ready = False
                continue
            if not server.verified:
                self.unverified.add(name)
                continue
            if not required_tools <= server.enabled_tools:
                raise BridgeError(f"ACP MCP server {name!r} lacks verified enabled required tools")
        return ready

    async def wait(self) -> None:
        while True:
            self.changed.clear()
            if self.ready():
                return
            await self.changed.wait()


class KiroACP(ACPClient):
    """Kiro CLI launch, profile selection and MCP readiness extensions."""

    _mcp_readiness: _MCPReadiness | None = None
    # After open_session: the required MCP servers the v2 engine reported initialized
    # without a tool list, so their required tools were not verified by the bridge.
    mcp_unverified_servers: frozenset[str] = frozenset()

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: Callable[[dict[str, Any]], Awaitable[None]],
        on_permission: Callable[[dict[str, Any]], Awaitable[str | None]],
        *,
        agent_engine: Literal["v3", "v2"] = "v3",
        trust_all_tools: bool = False,
        trust_tools: list[str] | tuple[str, ...] = (),
        agent: str | None = None,
    ) -> None:
        if agent_engine not in ("v2", "v3"):
            raise ValueError("Kiro ACP engine must be 'v2' or 'v3'")
        if type(trust_all_tools) is not bool:
            raise ValueError("Kiro trust-all-tools switch must be a boolean")
        if trust_all_tools and agent_engine != "v2":
            raise ValueError("Kiro --trust-all-tools (-a) is a v2-only flag")
        trusted = validate_kiro_trust_tools(trust_tools)
        if trusted and agent_engine != "v2":
            raise ValueError("Kiro --trust-tools is a v2-only flag")
        if trusted and trust_all_tools:
            raise ValueError("Kiro --trust-tools and --trust-all-tools (-a) are mutually exclusive")
        if agent is not None and (type(agent) is not str or not KIRO_TOOL_NAME.fullmatch(agent)):
            raise ValueError("Kiro --agent must name an on-disk agent")
        if agent is not None and agent_engine != "v2":
            raise ValueError("Kiro --agent is a v2-only flag")
        super().__init__(binary, cwd, on_update, on_permission)
        self._agent_engine = agent_engine
        self._trust_all_tools = trust_all_tools
        self._trust_tools = trusted
        self._agent = agent

    @property
    def trust_mode(self) -> str | list[str]:
        """What this launch auto-approves: `ask` (nothing), `all` (`-a`), or the names
        given to `--trust-tools`. Recorded on the receipt as `kiro_trust`."""
        if self._trust_all_tools:
            return "all"
        if self._trust_tools:
            return list(self._trust_tools)
        return "ask"

    def command(self) -> list[str]:
        command = [self._binary, "acp", "--agent-engine", self._agent_engine]
        if self._agent_engine == "v3":
            command.extend(("--auth-method", "cli"))
        if self._agent is not None:
            command.extend(("--agent", self._agent))
        if self._trust_all_tools:
            command.append("-a")
        if self._trust_tools:
            command.extend(("--trust-tools", ",".join(self._trust_tools)))
        return command

    def environment(self) -> dict[str, str]:
        """Forward explicit native, locale and network settings for the session."""
        from ...acp_environment import NETWORK_ENV_KEYS

        allowed = {
            "PATH",
            "HOME",
            "SHELL",
            "TERM",
            "LANG",
            "LANGUAGE",
            "LC_ALL",
            "LC_CTYPE",
            "LC_COLLATE",
            "LC_MESSAGES",
            "LC_MONETARY",
            "LC_NUMERIC",
            "LC_TIME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        } | NETWORK_ENV_KEYS
        return {name: value for name in allowed if (value := os.environ.get(name)) is not None}

    def provider_message(self, message: dict[str, Any]) -> bool:
        method = message.get("method")
        if self._mcp_readiness is None:
            return False
        try:
            if method == "_kiro/mcp/status":
                self._mcp_readiness.observe(message.get("params"))
                return True
            if method == "_kiro.dev/mcp/server_initialized" and self._agent_engine == "v2":
                self._mcp_readiness.observe_initialized(message.get("params"))
                return True
        except BridgeError as error:
            self._fail(error)
            return True
        return False

    def replay(self, params: dict[str, Any], update: dict[str, Any]) -> bool:
        for envelope in (params, update):
            meta = envelope.get("_meta") or {}
            kiro = meta.get("kiro") if isinstance(meta, dict) else None
            if isinstance(kiro, dict) and kiro.get("replay"):
                return True
        return False

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        required_mcp_tools: dict[str, set[str]] | None = None,
        agent_profile: dict[str, Any] | None = None,
    ) -> str:
        # Refuse overlap before replacing the current readiness observer.
        if self._active_session is not None or self._opening:
            raise BridgeError("Cannot change ACP sessions during a prompt or another session open")
        profile_id = agent_profile["id"] if agent_profile is not None else None
        metadata = (
            {"kiro": {"modeId": profile_id, "customAgents": [agent_profile]}}
            if agent_profile is not None
            else {}
        )
        readiness = _MCPReadiness(required_mcp_tools, session_id) if required_mcp_tools else None
        deadline = asyncio.get_running_loop().time() + shared_acp.MCP_READINESS_TIMEOUT
        self._mcp_readiness = readiness
        self.mcp_unverified_servers = frozenset()

        async def ready(ident: str, response: BaseModel) -> None:
            values = self.serialize(response)
            reported = (values.get("modes") or {}).get("currentModeId")
            if profile_id is not None and reported != profile_id:
                message = f"ACP did not select required agent profile {profile_id!r}"
                if self._agent_engine == "v2":
                    # v2 ignores the inline customAgents metadata and selects only an
                    # on-disk agent named by `--agent` (observed on Kiro CLI 3.5.0).
                    message += (
                        f" (Kiro reported {reported!r}); the v2 engine selects an agent only "
                        f"from an installed agent file named by --agent {profile_id}"
                    )
                raise BridgeError(message)
            if readiness is not None:
                readiness.bind(ident)
                if not readiness.ready():
                    await self._call(
                        readiness.wait(),
                        "MCP readiness",
                        max_seconds=max(0, deadline - asyncio.get_running_loop().time()),
                    )

        try:
            return await self._open_session(
                session_id,
                mcp_servers,
                metadata=metadata,
                request_limit=shared_acp.MCP_SESSION_TIMEOUT
                if readiness is not None
                else shared_acp.REQUEST_TIMEOUT,
                on_ready=ready,
            )
        finally:
            if readiness is not None:
                self.mcp_unverified_servers = frozenset(readiness.unverified)
            self._mcp_readiness = None
