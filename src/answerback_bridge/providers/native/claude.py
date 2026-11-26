"""Owned Claude Code sessions through the optional official Agent SDK.

The adapter supports foreground coding turns and native one-time permission
prompts. It excludes project settings/skills, subagents, background tools and
interactive elicitation. Settings scoping is not an operating-system sandbox.
The caller supplies only session IDs its durable ownership ledger already owns.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from ...process import BridgeError
from .protocol import NativeResult, PermissionCallback, UpdateCallback

MAX_FRAME_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_PERMISSION_BYTES = 32_000
MAX_SETTINGS_BYTES = 8 * 1024 * 1024
MAX_PENDING_TOOLS = 128
MAX_TOOL_HISTORY = 4096
MAX_CANCEL_LIFECYCLE = 16
MINIMUM_CLI_VERSION = (2, 1, 322)
SDK_VERSION = "0.2.182"
SHUTDOWN_TIMEOUT = 25.0
SETTLE_TIMEOUT = 5.0
CODING_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
UNSUPPORTED_TOOLS = {
    "Agent",
    "Task",
    "TaskOutput",
    "TaskStop",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "CronCreate",
    "CronDelete",
    "CronList",
}
FOREGROUND_TASK_EVENTS = {"task_started", "task_progress", "task_notification"}
PROVIDER_ENV_KEYS = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_BASE_URL",
    }
)


def _settings_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate settings key")
        result[key] = value
    return result


def _settings_bound_error(size: int | None) -> BridgeError:
    """Name the bound and the remedy; the message never carries file content."""
    observed = (
        "settings.json grew while it was read" if size is None else f"settings.json is {size} bytes"
    )
    return BridgeError(
        "Claude native provider settings exceed the supported bound of "
        f"{MAX_SETTINGS_BYTES} bytes ({observed}); only the env member is read, so shrink the "
        "file or register the workspace with --home pointing at a smaller profile"
    )


def _missing_sdk_error() -> BridgeError:
    return BridgeError(
        "Claude Code requires the optional answerback[claude] dependency; "
        "install that extra and configure an installed Claude CLI"
    )


def _sdk_version_error(version: object) -> BridgeError | None:
    if version != SDK_VERSION:
        return BridgeError(f"Claude adapter requires claude-agent-sdk=={SDK_VERSION}")
    return None


def _cli_version_from_output(text: str) -> str | None:
    """Extract Claude's leading semantic version from its version-command output."""
    version = re.match(r"(\d+\.\d+\.\d+)\b", text.lstrip())
    return None if version is None else version.group(1)


def _cli_version_error(version_text: object) -> BridgeError | None:
    version = (
        re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version_text)
        if isinstance(version_text, str)
        else None
    )
    if version is None or tuple(map(int, version.groups())) < MINIMUM_CLI_VERSION:
        minimum_version = ".".join(map(str, MINIMUM_CLI_VERSION))
        return BridgeError(f"Claude CLI {minimum_version} or newer is required for this protocol")
    return None


def _provider_environment(home: Path, process: Mapping[str, str]) -> dict[str, str]:
    """Read only provider/auth/model environment settings; never load execution policy.

    Native credentials and their referenced files remain in place. Values exist
    only in memory and the owned child's environment, never in receipt metadata.
    An existing process value wins, including an intentionally empty value.
    """
    try:
        descriptor = os.open(home / "settings.json", os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise BridgeError("Claude native provider settings could not be read") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BridgeError("Claude native provider settings must be a regular file")
        if info.st_size > MAX_SETTINGS_BYTES:
            raise _settings_bound_error(info.st_size)
        data = bytearray()
        while len(data) <= MAX_SETTINGS_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_SETTINGS_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_SETTINGS_BYTES:
            raise _settings_bound_error(None)
    except OSError:
        raise BridgeError("Claude native provider settings could not be read") from None
    finally:
        os.close(descriptor)
    try:
        settings = json.loads(data, object_pairs_hook=_settings_object)
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("Claude native provider settings contain malformed JSON") from None
    if not isinstance(settings, dict) or not isinstance(settings.get("env", {}), dict):
        raise BridgeError("Claude native provider settings environment must be an object")
    forwarded: dict[str, str] = {}
    for key, value in settings.get("env", {}).items():
        if key not in PROVIDER_ENV_KEYS and not re.fullmatch(
            r"ANTHROPIC_DEFAULT_[A-Z0-9_]+_MODEL", key
        ):
            continue
        if not isinstance(value, str) or "\0" in value:
            raise BridgeError("Claude native provider settings environment value is invalid")
        if key not in process:
            forwarded[key] = value
    _json(forwarded, MAX_PERMISSION_BYTES, "provider settings environment")
    return forwarded


def _subprocess_transport(options: Any) -> Any:
    """Use the pinned SDK's process owner through its public custom Transport seam.

    The 0.2.182 wheel drops user-message attribution in its typed result parser.
    A raw observer preserves those fields without changing the SDK's parser or
    reimplementing its permission/control/process lifecycle. This one backend
    import is deliberately pinned and covered by installed-artifact tests.
    """
    backend = importlib.import_module("claude_agent_sdk._internal.transport.subprocess_cli")
    transport_options = copy.copy(options)
    # The SDK configures this on an internal options copy only. A supplied custom
    # transport must receive the same stdio route while client options retain the
    # can_use_tool callback and no conflicting permission_prompt_tool_name.
    transport_options.permission_prompt_tool_name = "stdio"

    async def empty() -> AsyncIterator[dict[str, Any]]:
        for item in ():
            yield item

    return backend.SubprocessCLITransport(prompt=empty(), options=transport_options)


class _ObservedTransport:
    """Delegate the SDK Transport interface and observe immutable native frames."""

    def __init__(self, delegate: Any, observe: Callable[[dict[str, Any]], None]) -> None:
        self.delegate = delegate
        self._observe = observe

    async def connect(self) -> None:
        await self.delegate.connect()

    async def write(self, data: str) -> None:
        await self.delegate.write(data)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        async for frame in self.delegate.read_messages():
            self._observe(frame)
            yield frame

    async def close(self) -> None:
        await self.delegate.close()

    async def end_input(self) -> None:
        await self.delegate.end_input()

    def is_ready(self) -> bool:
        return self.delegate.is_ready()


def _uuid(value: str | None, label: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise BridgeError(f"Claude {label} must be a canonical UUID") from None
    return value


def _json(value: Any, limit: int, label: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise BridgeError(f"Claude {label} is not valid JSON") from None
    if len(encoded.encode("utf-8")) > limit:
        raise BridgeError(f"Claude {label} exceeds the supported bound")
    return encoded


def _tool_input_identity(value: dict[str, Any]) -> str:
    validated = _json(value, MAX_PERMISSION_BYTES, "native tool input")
    return json.dumps(json.loads(validated), sort_keys=True, ensure_ascii=False)


def _has_exact_prompt_attribution(frame: dict[str, Any], active: str | None) -> bool:
    """Only the observed single-prompt contract is qualified for owned turns."""
    singular, plural = "user_message_uuid" in frame, "user_message_uuids" in frame
    if not singular and not plural:
        return False
    if active is None:
        raise BridgeError("Claude prompt attribution has no active prompt")
    if singular:
        prompt = _uuid(frame["user_message_uuid"], "prompt attribution UUID")
        if prompt != active:
            raise BridgeError("Claude prompt attribution referenced another prompt")
    if plural and frame["user_message_uuids"] != [active]:
        # Batches, duplicate IDs, null, and contradictory declarations are not
        # evidence for the one SDK prompt this connection has in flight.
        raise BridgeError("Claude prompt attribution is not the qualified single prompt")
    return True


def _pairs(items: Any, label: str) -> dict[str, str]:
    if not isinstance(items, list):
        raise BridgeError(f"Claude MCP {label} must be name/value entries")
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BridgeError(f"Claude MCP {label} entry is malformed")
        name, value = item.get("name"), item.get("value")
        if (
            not isinstance(name, str)
            or not name
            or "\0" in name
            or "=" in name
            or name in result
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise BridgeError(f"Claude MCP {label} entry is invalid or duplicated")
        result[name] = value
    return result


def _mcp_config(servers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(servers, list):
        raise BridgeError("Claude MCP configuration must be a list")
    _json(servers, MAX_PERMISSION_BYTES, "MCP configuration")
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if not isinstance(server, dict):
            raise BridgeError("Claude MCP server is malformed")
        name = server.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise BridgeError("Claude MCP server name is missing or duplicated")
        kind = server.get("type", "stdio")
        if kind == "stdio":
            command = server.get("command")
            args = server.get("args", [])
            if (
                not isinstance(command, str)
                or not command
                or "\0" in command
                or not isinstance(args, list)
                or not all(isinstance(arg, str) and "\0" not in arg for arg in args)
            ):
                raise BridgeError("Claude MCP stdio command is malformed")
            result[name] = {
                "type": "stdio",
                "command": command,
                "args": list(args),
                "env": _pairs(server.get("env", []), "environment"),
            }
        elif kind in {"http", "sse"}:
            url = server.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise BridgeError("Claude MCP URL is invalid")
            result[name] = {
                "type": kind,
                "url": url,
                "headers": _pairs(server.get("headers", []), "headers"),
            }
        else:
            raise BridgeError(f"Claude MCP transport {kind!r} is unsupported")
    return result


class ClaudeCLI:
    """One SDK process; ``home`` is the exact CLAUDE_CONFIG_DIR, not an OS home."""

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: UpdateCallback,
        on_permission: PermissionCallback,
        *,
        home: Path | str | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = str(Path(cwd).resolve())
        self._home = Path(home).resolve() if home is not None else None
        self._on_update = on_update
        self._on_permission = on_permission
        self._sdk: Any = None
        self._client: Any = None
        self._entered = False
        self._opening = False
        self._closed = False
        self._stream_ended = False
        self._session_id: str | None = None
        self._mcp_prefixes: tuple[str, ...] = ()
        self._active_prompt: str | None = None
        self._seen_prompts: set[str] = set()
        self._seen_results: set[str] = set()
        self._raw_results: dict[str, dict[str, Any]] = {}
        self._native_idle = asyncio.Event()
        self._permission_ids: set[str] = set()
        self._permissions: set[asyncio.Task[Any]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._failure: asyncio.Future[BridgeError] | None = None
        self._result: asyncio.Future[NativeResult] | None = None
        self._close_task: asyncio.Task[None] | None = None
        # The SDK child has no isolated process group; its close suppresses wait errors.
        self.native_stopped: bool | None = None
        self._close_waiter: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._tool_uses: dict[str, tuple[str, str, bool]] = {}
        self._seen_tool_uses: set[str] = set()
        self._attributed_messages: set[str] = set()
        self._foreground_tools: dict[str, tuple[str, str]] = {}
        self._foreground_tasks: dict[str, tuple[str, str | None, bool]] = {}
        self._task_events: dict[str, tuple[str, str, str, str]] = {}
        self._seen_task_events: set[str] = set()
        self._cancel_lifecycle: list[dict[str, Any]] = []
        # Foreground tasks already settled when our cancel was requested; every
        # other started task must show its stop in ``_cancel_lifecycle``.
        self._settled_before_cancel: frozenset[str] = frozenset()
        self._native_hook_prompt: str | None = None
        self._seen_hook_prompts: set[str] = set()

    async def __aenter__(self) -> Self:
        if self._entered or self._closed:
            raise BridgeError("Claude adapter is already entered or closed")
        try:
            self._sdk = importlib.import_module("claude_agent_sdk")
        except ImportError:
            raise _missing_sdk_error() from None
        error = _sdk_version_error(getattr(self._sdk, "__version__", None))
        if error is not None:
            raise error
        self._entered = True
        self._failure = asyncio.get_running_loop().create_future()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_waiter = asyncio.create_task(self._wait_for_close())
        assert self._close_waiter is not None
        cancelled = False
        while True:
            try:
                # Multiple raw cancellations must not abandon owned SDK cleanup.
                await asyncio.shield(self._close_waiter)
                break
            except asyncio.CancelledError:
                if self._close_waiter.cancelled():
                    raise BridgeError(
                        "Claude cleanup was interrupted; shutdown is unconfirmed"
                    ) from None
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _wait_for_close(self) -> None:
        assert self._close_task is not None
        done, _ = await asyncio.wait((self._close_task,), timeout=SHUTDOWN_TIMEOUT)
        if not done:
            # The SDK's close keeps its own child identity and escalation. Do not
            # cancel it on this timeout or claim the subprocess has stopped.
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            raise BridgeError("Claude owned shutdown exceeded its bound; shutdown is unconfirmed")
        await self._close_task

    async def _close(self) -> None:
        self._closed = True
        await self._retire_permissions()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._client is not None:
            await self._client.disconnect()
        if self._result is not None and not self._result.done():
            self._result.cancel()

    def _check(self) -> None:
        if not self._entered or self._closed:
            raise BridgeError("Claude adapter is not open")
        if self._failure is not None and self._failure.done():
            raise self._failure.result()
        if self._stream_ended:
            raise BridgeError("Claude message stream has ended; this connection cannot be reused")

    def _fail(self, error: BridgeError) -> None:
        if self._failure is not None and not self._failure.done():
            self._failure.set_result(error)

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        self._check()
        if self._client is not None or self._opening:
            raise BridgeError("Claude adapter has already opened a session")
        self._opening = True
        try:
            return await self._open_session(session_id, mcp_servers)
        finally:
            self._opening = False

    async def _open_session(
        self, session_id: str | None, mcp_servers: list[dict[str, Any]] | None
    ) -> str:
        selected = _uuid(session_id, "session ID") if session_id is not None else str(uuid4())
        servers = _mcp_config(mcp_servers if mcp_servers is not None else [])
        self._mcp_prefixes = tuple(f"mcp__{name}__" for name in servers)
        process_environment = dict(os.environ)
        native_home = self._home
        if native_home is None:
            native_home = await asyncio.to_thread(
                lambda: (
                    Path(process_environment.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                    .expanduser()
                    .resolve()
                )
            )
        env = await asyncio.to_thread(_provider_environment, native_home, process_environment)
        self._check()
        env.update(
            CLAUDE_CONFIG_DIR=str(native_home),
            CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS="1",
        )
        options = self._sdk.ClaudeAgentOptions(
            cli_path=self._binary,
            cwd=self._cwd,
            session_id=selected if session_id is None else None,
            resume=session_id,
            fork_session=False,
            continue_conversation=False,
            system_prompt={"type": "preset", "preset": "claude_code"},
            permission_mode="default",
            can_use_tool=self._permission,
            tools=list(CODING_TOOLS),
            allowed_tools=[],
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            mcp_servers=servers,
            env=env,
            max_buffer_size=MAX_FRAME_BYTES,
            verbatim_prompts=True,
            hooks={
                "PreToolUse": [self._sdk.HookMatcher(hooks=[self._pre_tool], on_failure="block")],
                # The official SDK exposes the Elicitation hook output schema;
                # it forwards hook event names to native initialization unchanged.
                "Elicitation": [
                    self._sdk.HookMatcher(hooks=[self._elicitation], on_failure="block")
                ],
            },
        )
        self._session_id = selected
        transport = _ObservedTransport(_subprocess_transport(options), self._observe_wire)
        self._client = self._sdk.ClaudeSDKClient(options=options, transport=transport)
        try:
            await self._client.connect()
            info = await self._client.get_server_info()
            self._validate_info(info)
            self._check()
        except BaseException:
            # Context cleanup also covers a partially started SDK process.
            await self.__aexit__()
            raise
        self._reader = asyncio.create_task(self._read_messages())
        return selected

    @staticmethod
    def _validate_info(info: Any) -> None:
        if not isinstance(info, dict):
            raise BridgeError("Claude did not return initialization evidence")
        error = _cli_version_error(info.get("claude_code_version", ""))
        if error is not None:
            raise error
        if info.get("session_state") != "idle":
            raise BridgeError("Claude did not confirm an idle session")
        if info.get("current_permission_mode") not in {"default", "manual"}:
            raise BridgeError("Claude did not confirm normal tool permissions")
        if (
            info.get("hook_on_failure_supported") is not True
            or info.get("hooks_applied") is not True
        ):
            raise BridgeError("Claude did not apply the required fail-closed tool hooks")

    async def prompt(self, session_id: str, message: str, *, prompt_id: str) -> NativeResult:
        self._check()
        if self._client is None or session_id != self._session_id:
            raise BridgeError("Claude session must be opened before prompting")
        if self._active_prompt is not None:
            raise BridgeError("A Claude prompt is already in flight")
        _uuid(prompt_id, "prompt ID")
        if prompt_id in self._seen_prompts or len(self._seen_prompts) >= 1024:
            raise BridgeError("Claude prompt ID was reused or its history bound was reached")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_TEXT_CHARS:
            raise BridgeError("Claude prompt is empty or exceeds the supported bound")
        self._seen_prompts.add(prompt_id)
        self._active_prompt = prompt_id
        self._native_idle.clear()
        self._cancel_requested = False
        self._permission_ids.clear()
        self._tool_uses.clear()
        self._attributed_messages.clear()
        self._clear_foreground_tasks()
        self._native_hook_prompt = None
        self._result = asyncio.get_running_loop().create_future()

        async def messages() -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "user",
                "uuid": prompt_id,
                "session_id": session_id,
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": message},
            }

        try:
            await self._client.query(messages())
            assert self._failure is not None
            await asyncio.wait((self._result, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure.done():
                raise self._failure.result()
            result = self._result.result()
            await self._on_update(
                {
                    "sessionUpdate": "native_terminal",
                    "session_id": result.session_id,
                    "turn_id": result.turn_id,
                }
            )
            await self._wait_for_idle()
            return replace(result, evidence={**result.evidence, "native_idle": True})
        except BaseException:
            self._fail(BridgeError("Claude prompt did not settle; connection reuse is unsafe"))
            raise
        finally:
            await self._retire_permissions()
            self._active_prompt = None
            self._tool_uses.clear()
            self._attributed_messages.clear()
            self._clear_foreground_tasks()

    async def _wait_for_idle(self) -> None:
        assert self._failure is not None
        waiting = asyncio.create_task(self._native_idle.wait())
        try:
            await asyncio.wait(
                (waiting, self._failure),
                timeout=SETTLE_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._failure.done():
                raise self._failure.result()
            if not self._native_idle.is_set():
                raise BridgeError("Claude terminal result did not establish native idle")
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def _read_messages(self) -> None:
        try:
            async for event in self._client.receive_messages():
                if isinstance(event, self._sdk.ResultMessage):
                    result = self._terminal(event)
                    if self._result is None or self._result.done():
                        raise BridgeError(
                            "Claude emitted a duplicate or unsolicited terminal result"
                        )
                    self._result.set_result(result)
                    continue
                data = getattr(event, "data", {})
                native_session = getattr(event, "session_id", None)
                if isinstance(data, dict):
                    native_session = native_session or data.get("session_id")
                if native_session and native_session != self._session_id:
                    raise BridgeError("Claude event referenced an unexpected session")
                subtype = getattr(event, "subtype", "")
                if subtype in FOREGROUND_TASK_EVENTS:
                    identity = (
                        subtype,
                        getattr(event, "session_id", None),
                        getattr(event, "task_id", None),
                        getattr(event, "tool_use_id", None),
                    )
                    if self._task_events.pop(getattr(event, "uuid", None), None) != identity:
                        raise BridgeError("Claude task event lacks qualified native evidence")
                elif subtype == "elicitation" or (
                    isinstance(subtype, str) and subtype.startswith("task_")
                ):
                    raise BridgeError(f"Claude interaction {subtype!r} is unsupported")
                await self._on_update(
                    {
                        "sessionUpdate": subtype or type(event).__name__,
                        "session_id": self._session_id,
                    }
                )
            self._stream_ended = True
            if self._result is None or not self._result.done():
                self._fail(BridgeError("Claude stream ended without a correlated terminal result"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._stream_ended = True
            if isinstance(error, BridgeError) or self._result is None or not self._result.done():
                self._fail(
                    error
                    if isinstance(error, BridgeError)
                    else BridgeError(f"Claude SDK {type(error).__name__}: {str(error)[:2000]}")
                )

    def _terminal(self, event: Any) -> NativeResult:
        if (
            getattr(event, "deferred_tool_use", None) is not None
            or getattr(event, "terminal_reason", None) == "deferred_tool_use"
        ):
            raise BridgeError("Claude deferred tool interaction is unsupported")
        if event.session_id != self._session_id:
            raise BridgeError("Claude result referenced an unexpected session")
        result_id = getattr(event, "uuid", None)
        if not isinstance(result_id, str) or not result_id or result_id in self._seen_results:
            raise BridgeError("Claude result identity is missing or duplicated")
        _uuid(result_id, "result ID")
        raw = self._raw_results.pop(result_id, None)
        if raw is None:
            raise BridgeError("Claude typed result lacks matching native terminal evidence")
        for key in (
            "session_id",
            "uuid",
            "subtype",
            "is_error",
            "result",
            "terminal_reason",
            "stop_reason",
            "errors",
            "api_error_status",
        ):
            if getattr(event, key, None) != raw.get(key):
                raise BridgeError("Claude typed result disagrees with its native terminal evidence")
        if not _has_exact_prompt_attribution(raw, self._active_prompt):
            raise BridgeError("Claude result does not identify the active prompt")
        if type(event.is_error) is not bool or not isinstance(event.subtype, str):
            raise BridgeError("Claude terminal result is malformed")
        text = getattr(event, "result", None)
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT_CHARS):
            raise BridgeError("Claude final text is invalid or exceeds the supported bound")
        reason = getattr(event, "terminal_reason", None)
        if reason in {"aborted_streaming", "aborted_tools"}:
            status = "cancelled"
        elif event.is_error or event.subtype != "success":
            status = "failed"
        elif reason not in {None, "completed"}:
            raise BridgeError(f"Claude terminal reason {reason!r} is unsupported")
        else:
            status = "completed"
        if status == "completed" and self._permissions:
            raise BridgeError("Claude completed while a tool permission remained pending")
        if status == "completed" and any(not task[2] for task in self._foreground_tasks.values()):
            raise BridgeError("Claude completed while a foreground task remained unsettled")
        if status == "cancelled" and self._cancel_requested:
            unwitnessed = self._unwitnessed_cancelled_task()
            if unwitnessed is not None:
                task_id, call_id = unwitnessed
                raise BridgeError(
                    "Claude cancelled without the native stop witness for foreground task "
                    f"{task_id!r} (tool use {call_id!r})"
                )
        errors = getattr(event, "errors", None)
        if errors is not None and (
            not isinstance(errors, list) or not all(isinstance(error, str) for error in errors)
        ):
            raise BridgeError("Claude native errors are malformed")
        self._seen_results.add(result_id)
        return NativeResult(
            session_id=event.session_id,
            status=status,
            text=text if text and text.strip() else None,
            turn_id=result_id,
            evidence={
                "protocol": "claude-sdk-stream-json",
                "prompt_id": self._active_prompt,
                "result_id": result_id,
                "subtype": event.subtype,
                "terminal_reason": reason,
                "stop_reason": getattr(event, "stop_reason", None),
                "is_error": event.is_error,
                "api_error_status": getattr(event, "api_error_status", None),
                "errors": [error[:2000] for error in (errors or [])[:16]],
                # Native acknowledgement of our own cancel: the lifecycle frames
                # Claude emitted for the admitted foreground Bash after interrupt.
                "cancel_requested": self._cancel_requested,
                "cancel_lifecycle": [dict(entry) for entry in self._cancel_lifecycle],
                # A terminal turn proves its outcome; this adapter has not proved
                # a durable native history record plus idle state after that turn.
                "resumable": False,
            },
        )

    def _observe_wire(self, frame: dict[str, Any]) -> None:
        try:
            self._capture_wire(frame)
        except BridgeError as error:
            # Query may already have queued the first parsed result when a later
            # raw frame contradicts it. Latch before the SDK schedules consumers.
            self._fail(error)
            raise

    def _capture_wire(self, frame: dict[str, Any]) -> None:
        encoded = _json(frame, MAX_FRAME_BYTES, "native frame")
        if not isinstance(frame, dict):
            raise BridgeError("Claude native frame must be an object")
        if frame.get("type") == "assistant":
            self._capture_tool_uses(frame)
        subtype = frame.get("subtype")
        if (
            frame.get("type") == "system"
            and isinstance(subtype, str)
            and subtype.startswith("task_")
        ):
            self._capture_foreground_task(frame)
        if frame.get("type") == "system" and frame.get("subtype") == "session_state_changed":
            # The SDK deliberately hides sdk_host_only state events from its
            # typed iterator. They still carry authority over the native run.
            if self._active_prompt is not None:
                if frame.get("session_id") != self._session_id:
                    raise BridgeError("Claude native state referenced an unexpected session")
                if frame.get("state") == "idle":
                    self._native_idle.set()
                else:
                    self._native_idle.clear()
        if frame.get("type") == "control_request":
            request = frame.get("request")
            if not isinstance(request, dict) or request.get("subtype") not in {
                "can_use_tool",
                "hook_callback",
                "mcp_message",
            }:
                raise BridgeError("Claude native interactive request is unsupported")
        if frame.get("type") != "result":
            return
        result_id = _uuid(frame.get("uuid"), "result ID")
        if result_id in self._raw_results or result_id in self._seen_results:
            raise BridgeError("Claude native terminal identity is duplicated")
        if len(self._raw_results) >= 8:
            raise BridgeError("Claude native terminal evidence exceeded its bound")
        # Snapshot before the SDK or caller can mutate a parsed dictionary.
        self._raw_results[result_id] = json.loads(encoded)

    def _clear_foreground_tasks(self) -> None:
        self._foreground_tools.clear()
        self._foreground_tasks.clear()
        self._task_events.clear()
        self._seen_task_events.clear()
        self._cancel_lifecycle.clear()
        self._settled_before_cancel = frozenset()

    def _unwitnessed_cancelled_task(self) -> tuple[str, str] | None:
        """Name a foreground task our cancel left without a recorded native stop.

        ``aborted_tools`` or ``aborted_streaming`` is Claude's acknowledgement
        of the interrupt itself. A Bash that was running when we interrupted
        (or that started afterwards) additionally has to end with a
        ``task_notification`` recorded in ``_cancel_lifecycle``; a cancel while
        blocked on a permission has no such task and needs no witness.
        """
        witnessed = {
            (entry["task_id"], entry["tool_use_id"])
            for entry in self._cancel_lifecycle
            if entry["subtype"] == "task_notification"
        }
        for task_id, (call_id, _, _) in self._foreground_tasks.items():
            if task_id not in self._settled_before_cancel and (task_id, call_id) not in witnessed:
                return task_id, call_id
        return None

    def _capture_foreground_task(self, frame: dict[str, Any]) -> None:
        """Qualify informational lifecycle data; never grant permission or run a tool.

        A requested cancel does not change which frames qualify. Interrupting a
        running foreground Bash makes Claude end that task with a ``stopped``
        notification before the aborted terminal result; the frame is the native
        acknowledgement of our cancel and is kept as evidence. Frames outside the
        admitted session, task and Bash identity are refused as before.
        """
        subtype, session = frame.get("subtype"), frame.get("session_id")
        if (
            not isinstance(subtype, str)
            or subtype not in FOREGROUND_TASK_EVENTS
            or not isinstance(session, str)
            or self._active_prompt is None
            or self._closed
            or session != self._session_id
            or frame.get("agent_id") is not None
            or frame.get("parent_tool_use_id") is not None
            or ("task_type" in frame and frame["task_type"] != "local_bash")
            or ("is_backgrounded" in frame and frame["is_backgrounded"] is not False)
        ):
            raise BridgeError("Claude task lifecycle is unsupported or not foreground")
        ident = _uuid(frame.get("uuid"), "task event ID")
        task_id, call_id, run_id = (
            frame.get("task_id"),
            frame.get("tool_use_id"),
            frame.get("run_id"),
        )
        if (
            not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 512
            or not isinstance(call_id, str)
            or not call_id
            or len(call_id) > 512
            or (
                run_id is not None
                and (not isinstance(run_id, str) or not run_id or len(run_id) > 512)
            )
            or ident in self._seen_task_events
            or len(self._seen_task_events) >= MAX_TOOL_HISTORY
            or len(self._task_events) >= MAX_PENDING_TOOLS
        ):
            raise BridgeError("Claude task lifecycle identity is invalid or duplicated")
        if subtype == "task_started":
            admitted = self._foreground_tools.get(call_id)
            if (
                frame.get("task_type") != "local_bash"
                or frame.get("is_backgrounded") is not False
                or admitted is None
                or admitted[0] != self._active_prompt
                or task_id in self._foreground_tasks
                or any(task[0] == call_id for task in self._foreground_tasks.values())
            ):
                raise BridgeError("Claude foreground task lacks its admitted Bash identity")
            self._foreground_tasks[task_id] = (call_id, run_id, False)
        else:
            task = self._foreground_tasks.get(task_id)
            if (
                task is None
                or task[0] != call_id
                or task[2]
                or (run_id is not None and run_id != task[1])
            ):
                raise BridgeError("Claude task lifecycle does not match its foreground start")
            if subtype == "task_notification":
                if frame.get("status") not in {"completed", "failed", "stopped"}:
                    raise BridgeError("Claude foreground task has an unknown terminal status")
                self._foreground_tasks[task_id] = (task[0], task[1], True)
        self._seen_task_events.add(ident)
        self._task_events[ident] = (subtype, session, task_id, call_id)
        if self._cancel_requested and len(self._cancel_lifecycle) < MAX_CANCEL_LIFECYCLE:
            self._cancel_lifecycle.append(
                {
                    "task_id": task_id,
                    "tool_use_id": call_id,
                    "subtype": subtype,
                    "status": frame.get("status") if subtype == "task_notification" else None,
                    "time": time.time(),
                }
            )

    def _capture_tool_uses(self, frame: dict[str, Any]) -> None:
        """Bind tool proposals before SDK parsing can discard their user UUIDs."""
        message = frame.get("message")
        if not isinstance(message, dict) or self._active_prompt is None:
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        tools = [
            item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
        attributed = _has_exact_prompt_attribution(frame, self._active_prompt)
        if (tools or attributed) and (
            frame.get("session_id") != self._session_id
            or frame.get("parent_tool_use_id") is not None
        ):
            raise BridgeError("Claude assistant tool scope does not match the active session")
        message_id = message.get("id")
        if attributed:
            if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
                raise BridgeError("Claude attributed assistant message identity is invalid")
            if len(self._attributed_messages) >= 1024:
                raise BridgeError("Claude assistant message history exceeded its bound")
            self._attributed_messages.add(message_id)
        attributed = attributed or (
            isinstance(message_id, str) and message_id in self._attributed_messages
        )
        for tool in tools:
            call_id, name, input_data = tool.get("id"), tool.get("name"), tool.get("input")
            if (
                not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 512
                or call_id in self._seen_tool_uses
                or len(self._seen_tool_uses) >= MAX_TOOL_HISTORY
                or len(self._tool_uses) >= MAX_PENDING_TOOLS
                or not isinstance(name, str)
                or not name
                or len(name) > 512
                or not isinstance(input_data, dict)
            ):
                raise BridgeError("Claude native tool identity is invalid, repeated, or excessive")
            encoded = _tool_input_identity(input_data)
            self._seen_tool_uses.add(call_id)
            self._tool_uses[call_id] = (name, encoded, attributed)

    def _verify_tool_hook(self, input_data: dict[str, Any], tool_use_id: str | None) -> None:
        call_id = input_data.get("tool_use_id")
        if not isinstance(call_id, str) or call_id != tool_use_id or call_id not in self._tool_uses:
            raise BridgeError("Claude tool hook lacks the active prompt's native tool identity")
        name, encoded, attributed = self._tool_uses[call_id]
        if name != input_data["tool_name"] or encoded != _tool_input_identity(
            input_data["tool_input"]
        ):
            raise BridgeError("Claude tool hook differs from its native tool proposal")
        native_prompt = input_data.get("prompt_id")
        if native_prompt is not None:
            _uuid(native_prompt, "native hook prompt ID")
        if self._native_hook_prompt is None:
            # CLI prompt_id and SDK user-message UUID are distinct namespaces.
            # The first mapping needs the exact attributed assistant tool proposal.
            # An absent optional hook prompt_id is accepted only on that direct
            # evidence. It establishes no mapping for unattributed continuations.
            if not attributed:
                raise BridgeError("Claude tool hook does not identify the active prompt")
            if native_prompt is not None:
                if native_prompt in self._seen_hook_prompts:
                    raise BridgeError("Claude native hook prompt ID was reused")
                self._native_hook_prompt = native_prompt
                self._seen_hook_prompts.add(native_prompt)
        elif native_prompt != self._native_hook_prompt:
            raise BridgeError("Claude tool hook referenced an unexpected native prompt")
        del self._tool_uses[call_id]

    def _tool_allowed(self, tool: str, input_data: dict[str, Any], agent_id: Any) -> None:
        if self._active_prompt is None or self._closed:
            raise BridgeError("Claude tool request has no active task")
        if agent_id:
            raise BridgeError("Claude subagent interaction is unsupported")
        if tool in UNSUPPORTED_TOOLS:
            raise BridgeError(f"Claude tool interaction {tool!r} is unsupported")
        if tool not in CODING_TOOLS and not tool.startswith(self._mcp_prefixes):
            raise BridgeError(f"Claude tool family {tool!r} is unsupported")
        if input_data.get("run_in_background"):
            raise BridgeError("Claude background tool execution is unsupported")

    async def _pre_tool(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        try:
            self._check()
            if input_data.get("session_id") != self._session_id:
                raise BridgeError("Claude tool hook referenced an unexpected session")
            self._tool_allowed(
                input_data["tool_name"], input_data["tool_input"], input_data.get("agent_id")
            )
            self._verify_tool_hook(input_data, tool_use_id)
            if input_data["tool_name"] == "Bash":
                assert tool_use_id is not None and self._active_prompt is not None
                self._foreground_tools[tool_use_id] = (
                    self._active_prompt,
                    _tool_input_identity(input_data["tool_input"]),
                )
            return {}
        except Exception as error:
            failure = (
                error
                if isinstance(error, BridgeError)
                else BridgeError("Claude tool hook is malformed")
            )
            self._fail(failure)
            if failure is error:
                raise
            raise failure from error

    async def _elicitation(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        self._fail(BridgeError("Claude MCP elicitation is unsupported by one-time permissions"))
        return {"hookSpecificOutput": {"hookEventName": "Elicitation", "action": "cancel"}}

    async def _permission(self, tool: str, input_data: dict[str, Any], context: Any) -> Any:
        current = asyncio.current_task()
        try:
            self._check()
            self._tool_allowed(tool, input_data, getattr(context, "agent_id", None))
            encoded = _json(input_data, MAX_PERMISSION_BYTES, "tool input")
            call_id = getattr(context, "tool_use_id", None)
            if (
                not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 512
                or call_id in self._permission_ids
                or len(self._permission_ids) >= 1024
                or len(self._permissions) >= 8
            ):
                raise BridgeError("Claude tool permission identity is invalid or duplicated")
            self._permission_ids.add(call_id)
            # A notification cannot claim execution while the actual one-time
            # permission is pending or denied. Native policy may also allow a
            # pre-tool-qualified foreground call without invoking this callback.
            foreground = self._foreground_tools.pop(call_id, None)
            if current is not None:
                self._permissions.add(current)
            request = {
                "session_id": self._session_id,
                "tool_call": {
                    "toolCallId": call_id,
                    "title": getattr(context, "title", None) or tool,
                    "rawInput": json.loads(encoded),
                },
                "options": [
                    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Reject once", "kind": "reject_once"},
                ],
            }
            _json(request, MAX_PERMISSION_BYTES, "permission request")
            # The shared runtime owns human waiting and pauses its execution clock.
            choice = await self._on_permission(request)
            self._check()
            if choice == "allow" and not self._cancel_requested and not self._closed:
                if tool == "Bash" and foreground == (
                    self._active_prompt,
                    _tool_input_identity(input_data),
                ):
                    self._foreground_tools[call_id] = foreground
                return self._sdk.PermissionResultAllow(
                    updated_input=json.loads(encoded), updated_permissions=None
                )
            if choice not in {"allow", "deny", None}:
                raise BridgeError("Claude permission answer must be a supplied one-time option")
            return self._sdk.PermissionResultDeny(
                message="The operation was not approved.",
                interrupt=choice is None or self._cancel_requested or self._closed,
            )
        except asyncio.CancelledError:
            # SDK control_cancel_request abandons this native prompt. Propagate it
            # so the SDK sends no stale control response; shared UI retires in finally.
            raise
        except Exception as error:
            failure = (
                error
                if isinstance(error, BridgeError)
                else BridgeError(f"Claude permission callback failed: {type(error).__name__}")
            )
            self._fail(failure)
            if failure is error:
                raise
            raise failure from error
        finally:
            if current is not None:
                self._permissions.discard(current)

    async def _retire_permissions(self) -> None:
        pending = [task for task in self._permissions if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def closed(self) -> bool:
        """True once teardown has started, not only once ``_close`` has set ``_closed``.

        ``__aexit__`` creates ``_close_task`` before it ever awaits (T-51 X3): a
        cancellation that is already runnable at that instant must not still see
        an open adapter and call through to ``cancel()``, which would raise for a
        turn that in fact ended normally. ``_closed`` itself still flips only
        inside ``_close()``, unchanged, so every other reader of that private flag
        keeps its own timing.
        """
        return self._closed or self._close_task is not None

    async def cancel(self, session_id: str) -> None:
        self._check()
        if self._client is None or session_id != self._session_id or self._active_prompt is None:
            raise BridgeError("No owned active Claude prompt can be cancelled")
        if not self._cancel_requested:
            self._settled_before_cancel = frozenset(
                task_id for task_id, task in self._foreground_tasks.items() if task[2]
            )
        self._cancel_requested = True
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                await self._client.interrupt()
        except TimeoutError as error:
            failure = BridgeError("Claude interrupt was not acknowledged; connection is now failed")
            self._fail(failure)
            raise failure from error
        finally:
            await self._retire_permissions()
