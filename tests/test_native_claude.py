"""Claude transport contracts exercised without a model, login or subprocess."""

import asyncio
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from answerback_bridge import native_claude
from answerback_bridge.process import BridgeError

_EOF = object()


class FakeResult(SimpleNamespace):
    pass


class FakeSystem(SimpleNamespace):
    pass


class FakeAssistant(SimpleNamespace):
    pass


class FakeRawTransport:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.emit_idle = True

    async def connect(self) -> None:
        pass

    async def write(self, _: str) -> None:
        pass

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self.queue.get()
            if event is _EOF:
                return
            if isinstance(event, Exception):
                raise event
            kind = (
                "result"
                if isinstance(event, FakeResult)
                else "system"
                if isinstance(event, FakeSystem)
                else "assistant"
            )
            yield {"type": kind, **vars(event)}
            if isinstance(event, FakeResult) and self.emit_idle:
                yield {
                    "type": "system",
                    "subtype": "session_state_changed",
                    "session_id": event.session_id,
                    "state": "idle",
                    "sdk_host_only": True,
                }

    async def close(self) -> None:
        pass

    async def end_input(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True


class FakeSDKClient:
    def __init__(self, options: Any, sdk: Any, transport: Any) -> None:
        self.options = options
        self.sdk = sdk
        self.transport = transport
        self.queue = transport.delegate.queue
        self.sent: list[dict[str, Any]] = []
        self.connected = False
        self.disconnected = False
        self.interrupts = 0

    async def connect(self) -> None:
        if self.sdk.connect_error:
            raise self.sdk.connect_error
        self.connected = True

    async def get_server_info(self) -> dict[str, Any]:
        return self.sdk.info

    async def query(self, prompt: AsyncIterator[dict[str, Any]]) -> None:
        async for message in prompt:
            self.sent.append(message)
            if self.sdk.respond:
                await self.sdk.respond(self, message)

    async def receive_messages(self) -> AsyncIterator[Any]:
        async for event in self.transport.read_messages():
            kind = event.pop("type")
            if kind == "result":
                # Match the installed SDK wheel, whose parser drops these fields.
                event.pop("user_message_uuid", None)
                event.pop("user_message_uuids", None)
                yield FakeResult(**event)
            elif kind == "system":
                yield FakeSystem(**event)
            else:
                yield FakeAssistant(**event)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native-config"))
    module = ModuleType("claude_agent_sdk")
    state = SimpleNamespace(
        clients=[],
        respond=None,
        connect_error=None,
        info={
            "claude_code_version": "2.1.322",
            "session_state": "idle",
            "current_permission_mode": "default",
            "hook_on_failure_supported": True,
            "hooks_applied": True,
        },
    )

    def client(options: Any, transport: Any) -> FakeSDKClient:
        value = FakeSDKClient(options, state, transport)
        state.clients.append(value)
        return value

    module.__dict__.update(
        __version__="0.2.182",
        ClaudeAgentOptions=SimpleNamespace,
        ClaudeSDKClient=client,
        ResultMessage=FakeResult,
        SystemMessage=FakeSystem,
        AssistantMessage=FakeAssistant,
        PermissionResultAllow=lambda **kw: SimpleNamespace(behavior="allow", **kw),
        PermissionResultDeny=lambda **kw: SimpleNamespace(behavior="deny", **kw),
        HookMatcher=SimpleNamespace,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    monkeypatch.setattr(
        "answerback_bridge.native_claude._subprocess_transport",
        lambda options: FakeRawTransport(),
        raising=False,
    )
    return state


@pytest.fixture
def adapter_type() -> Any:
    try:
        module = importlib.import_module("answerback_bridge.native_claude")
    except ModuleNotFoundError:
        pytest.fail("Claude native transport is not implemented")
    return module.ClaudeCLI


async def update(_: dict[str, Any]) -> None:
    pass


async def approve(_: dict[str, Any]) -> str:
    return "allow"


def terminal(
    session: str, prompt: str, *, text: str | None = "Native final answer", **extra: Any
) -> FakeResult:
    fields = {
        "session_id": session,
        "uuid": str(uuid4()),
        "user_message_uuid": prompt,
        "user_message_uuids": [prompt],
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "stop_reason": "end_turn",
        "result": text,
        "errors": None,
        "api_error_status": None,
        "deferred_tool_use": None,
    }
    fields.update(extra)
    return FakeResult(**fields)


def respond_with(
    factory: Callable[[dict[str, Any]], Any],
) -> Callable[[FakeSDKClient, dict[str, Any]], Awaitable[None]]:
    async def respond(client: FakeSDKClient, message: dict[str, Any]) -> None:
        await client.queue.put(factory(message))

    return respond


def tool_frame(
    session: str, prompt: str | None, call_id: str, *, message_id: str = "msg-1"
) -> dict[str, Any]:
    """Claude 2.1.322 decorates only the first assistant content frame with user UUIDs."""
    value = {
        "type": "assistant",
        "session_id": session,
        "parent_tool_use_id": None,
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Read",
                    "input": {"file_path": "README.md"},
                }
            ],
        },
    }
    if prompt is not None:
        value.update(user_message_uuid=prompt, user_message_uuids=[prompt])
    return value


def hook_input(session: str, native_prompt: str, call_id: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "prompt_id": native_prompt,
        "tool_use_id": call_id,
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
    }


async def test_native_hook_prompt_id_is_bound_through_attributed_tool_identity(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id, native_prompt = str(uuid4()), str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        guard = sdk.clients[0].options.hooks["PreToolUse"][0].hooks[0]
        adapter._observe_wire(tool_frame(session, prompt_id, "tool-1"))
        assert await guard(hook_input(session, native_prompt, "tool-1"), "tool-1", {}) == {}
        # A subsequent native API response need not repeat user_message_uuid.
        adapter._observe_wire(tool_frame(session, None, "tool-2", message_id="msg-2"))
        assert await guard(hook_input(session, native_prompt, "tool-2"), "tool-2", {}) == {}
        await sdk.clients[0].queue.put(terminal(session, prompt_id))
        assert (await task).status == "completed"


async def test_first_tool_can_follow_attributed_text_in_same_native_message(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id, native_prompt = str(uuid4()), str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        first = tool_frame(session, prompt_id, "unused")
        first["message"]["content"] = [{"type": "text", "text": "I will read the file."}]
        adapter._observe_wire(first)
        adapter._observe_wire(tool_frame(session, None, "tool-1"))
        assert (
            await adapter._pre_tool(hook_input(session, native_prompt, "tool-1"), "tool-1", {})
            == {}
        )
        await sdk.clients[0].queue.put(terminal(session, prompt_id))
        assert (await task).status == "completed"


@pytest.mark.parametrize(
    "change", ["unknown", "input", "name", "session", "prompt", "duplicate", "callback_id"]
)
async def test_hook_identity_guard_rejects_unproven_or_changed_tool(
    adapter_type: Any, sdk: Any, tmp_path: Path, change: str
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id, native_prompt = str(uuid4()), str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        adapter._observe_wire(tool_frame(session, prompt_id, "tool-1"))
        await adapter._pre_tool(hook_input(session, native_prompt, "tool-1"), "tool-1", {})
        adapter._observe_wire(tool_frame(session, None, "tool-2", message_id="msg-2"))
        payload = hook_input(session, native_prompt, "tool-2")
        callback_id = "tool-2"
        if change == "unknown":
            payload["tool_use_id"] = callback_id = "unknown"
        elif change == "input":
            payload["tool_input"] = {"file_path": "different.txt"}
        elif change == "name":
            payload["tool_name"] = "Glob"
        elif change == "session":
            payload["session_id"] = str(uuid4())
        elif change == "prompt":
            payload["prompt_id"] = str(uuid4())
        elif change == "duplicate":
            payload["tool_use_id"] = callback_id = "tool-1"
        else:
            callback_id = "different"
        with pytest.raises(BridgeError):
            await adapter._pre_tool(payload, callback_id, {})
        with pytest.raises(BridgeError):
            await task


async def test_unattributed_first_tool_cannot_define_native_prompt_mapping(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        adapter._observe_wire(tool_frame(session, None, "tool-1"))
        with pytest.raises(BridgeError, match="prompt"):
            await adapter._pre_tool(hook_input(session, str(uuid4()), "tool-1"), "tool-1", {})
        with pytest.raises(BridgeError):
            await task


@pytest.mark.parametrize("changed_type", [False, True])
async def test_tool_input_identity_ignores_order_but_preserves_json_types(
    adapter_type: Any, sdk: Any, tmp_path: Path, changed_type: bool
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        frame = tool_frame(session, prompt_id, "tool-1")
        frame["message"]["content"][0]["input"] = {"offset": 1, "file_path": "README.md"}
        adapter._observe_wire(frame)
        payload = hook_input(session, str(uuid4()), "tool-1")
        payload["tool_input"] = {"file_path": "README.md", "offset": True if changed_type else 1}
        if changed_type:
            with pytest.raises(BridgeError, match="differs"):
                await adapter._pre_tool(payload, "tool-1", {})
            with pytest.raises(BridgeError):
                await task
        else:
            assert await adapter._pre_tool(payload, "tool-1", {}) == {}
            await sdk.clients[0].queue.put(terminal(session, prompt_id))
            await task


async def test_native_hook_prompt_cannot_be_reused_by_a_later_sdk_prompt(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        native_prompt = str(uuid4())
        for index in range(2):
            prompt_id, call_id = str(uuid4()), f"tool-{index}"
            task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
            await asyncio.sleep(0)
            adapter._observe_wire(tool_frame(session, prompt_id, call_id))
            if index:
                with pytest.raises(BridgeError, match="reused"):
                    await adapter._pre_tool(
                        hook_input(session, native_prompt, call_id), call_id, {}
                    )
                with pytest.raises(BridgeError):
                    await task
            else:
                await adapter._pre_tool(hook_input(session, native_prompt, call_id), call_id, {})
                await sdk.clients[0].queue.put(terminal(session, prompt_id))
                await task


@pytest.mark.parametrize("duplicate", [False, True])
async def test_raw_tool_ledger_refuses_repeated_ids_and_excess_pending_tools(
    adapter_type: Any, sdk: Any, tmp_path: Path, duplicate: bool
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        count = (
            1
            if duplicate
            else importlib.import_module("answerback_bridge.native_claude").MAX_PENDING_TOOLS
        )
        for index in range(count):
            adapter._observe_wire(tool_frame(session, prompt_id, f"tool-{index}"))
        with pytest.raises(BridgeError, match="repeated|excessive"):
            adapter._observe_wire(
                tool_frame(session, prompt_id, "tool-0" if duplicate else "overflow")
            )
        with pytest.raises(BridgeError):
            await task


async def test_owned_startup_scopes_sdk_settings_and_translates_mcp(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type(
        "/installed/claude", str(tmp_path), update, approve, home=tmp_path / "home"
    ) as adapter:
        session = await adapter.open_session(
            mcp_servers=[
                {
                    "name": "answerback",
                    "command": "/python",
                    "args": ["-m", "answerback_bridge", "mcp"],
                    "env": [{"name": "MODE", "value": "test"}],
                    "_meta": {"legacy": "ignored"},
                }
            ]
        )
        options = sdk.clients[0].options
        assert options.session_id == session
        assert options.resume is None
        assert options.fork_session is False
        assert options.continue_conversation is False
        assert options.cli_path == "/installed/claude"
        assert options.cwd == str(tmp_path)
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.skills == []
        assert options.permission_mode == "default"
        assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
        assert options.allowed_tools == []
        assert options.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home")
        assert options.mcp_servers == {
            "answerback": {
                "type": "stdio",
                "command": "/python",
                "args": ["-m", "answerback_bridge", "mcp"],
                "env": {"MODE": "test"},
            }
        }
        assert "Agent" not in options.tools
        assert options.hooks["PreToolUse"][0].on_failure == "block"
    assert sdk.clients[0].disconnected


async def test_resume_uses_exact_owned_id_without_new_session_or_fork(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    session = str(uuid4())
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        assert await adapter.open_session(session) == session
        options = sdk.clients[0].options
        assert options.resume == session
        assert options.session_id is None
        assert options.fork_session is False
        assert options.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "native-config")


@pytest.mark.parametrize(
    "changed",
    [
        {"session_state": "running"},
        {"current_permission_mode": "bypassPermissions"},
        {"hook_on_failure_supported": False},
        {"hooks_applied": False},
        {"claude_code_version": "2.1.100"},
    ],
)
async def test_refuses_unqualified_or_busy_native_startup(
    adapter_type: Any, sdk: Any, tmp_path: Path, changed: dict[str, Any]
) -> None:
    sdk.info.update(changed)
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(BridgeError):
            await adapter.open_session()
    assert sdk.clients[0].disconnected


@pytest.mark.parametrize(
    "hook_evidence",
    [
        {},
        {"hooks_applied": None},
        {"hooks_applied": 0},
        {"hooks_applied": 1},
        {"hooks_applied": "yes"},
        {"hooks_applied": "false"},
    ],
    ids=["missing", "null", "zero", "one", "yes-string", "false-string"],
)
async def test_startup_requires_explicit_hook_application(
    adapter_type: Any, sdk: Any, tmp_path: Path, hook_evidence: dict[str, Any]
) -> None:
    """red_at_base: missing or non-boolean evidence cannot qualify required hooks."""
    sdk.info.pop("hooks_applied")
    sdk.info.update(hook_evidence)
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(
            BridgeError, match="Claude did not apply the required fail-closed tool hooks"
        ):
            await adapter.open_session()
        assert adapter._reader is None
    assert sdk.clients[0].disconnected


@pytest.mark.parametrize("applied", [False, True])
async def test_startup_preserves_boolean_hook_application_behavior(
    adapter_type: Any, sdk: Any, tmp_path: Path, applied: bool
) -> None:
    """pins_existing: explicit True qualifies hooks and explicit False refuses startup."""
    sdk.info["hooks_applied"] = applied
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        if applied:
            assert await adapter.open_session()
        else:
            with pytest.raises(BridgeError, match="fail-closed tool hooks"):
                await adapter.open_session()
    assert sdk.clients[0].disconnected


async def test_exact_prompt_uuid_and_session_produce_native_result(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    sdk.respond = respond_with(lambda m: terminal(m["session_id"], m["uuid"]))
    prompt_id = str(uuid4())
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        result = await adapter.prompt(session, "Authorized task", prompt_id=prompt_id)
        sent = sdk.clients[0].sent[0]
        assert sent["uuid"] == prompt_id
        assert sent["message"] == {"role": "user", "content": "Authorized task"}
        assert sent["parent_tool_use_id"] is None
        assert result.status == "completed"
        assert result.text == "Native final answer"
        assert result.session_id == session
        assert result.evidence["prompt_id"] == prompt_id


@pytest.mark.parametrize("wrong", ["session", "prompt", "unattributed", "result_uuid"])
async def test_wrong_or_missing_native_identity_never_completes(
    adapter_type: Any, sdk: Any, tmp_path: Path, wrong: str
) -> None:
    def event(message: dict[str, Any]) -> FakeResult:
        value = terminal(message["session_id"], message["uuid"])
        if wrong == "session":
            value.session_id = str(uuid4())
        elif wrong == "prompt":
            value.user_message_uuid = str(uuid4())
            value.user_message_uuids = [value.user_message_uuid]
        elif wrong == "unattributed":
            value.user_message_uuid = None
            value.user_message_uuids = None
        else:
            value.uuid = None
        return value

    sdk.respond = respond_with(event)
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


async def test_unqualified_batch_result_cannot_mask_a_conflicting_singular_prompt(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    sdk.respond = respond_with(
        lambda m: terminal(
            m["session_id"],
            m["uuid"],
            user_message_uuid=str(uuid4()),
            user_message_uuids=[str(uuid4()), m["uuid"]],
        )
    )
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError, match="prompt"):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


@pytest.mark.parametrize("target", ["assistant", "result"])
@pytest.mark.parametrize(
    "change",
    [
        "wrong_singular",
        "integer_singular",
        "null_singular",
        "duplicate_ids",
        "other_id",
        "only_other_id",
        "empty_ids",
        "null_ids",
    ],
)
async def test_prompt_declarations_cannot_mask_conflicts(
    adapter_type: Any, sdk: Any, tmp_path: Path, target: str, change: str
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        declarations: dict[str, Any] = {
            "user_message_uuid": prompt_id,
            "user_message_uuids": [prompt_id],
        }
        if change == "wrong_singular":
            declarations["user_message_uuid"] = str(uuid4())
        elif change == "integer_singular":
            declarations["user_message_uuid"] = 123
        elif change == "null_singular":
            declarations["user_message_uuid"] = None
        else:
            declarations["user_message_uuids"] = {
                "duplicate_ids": [prompt_id, prompt_id],
                "other_id": [prompt_id, str(uuid4())],
                "only_other_id": [str(uuid4())],
                "empty_ids": [],
                "null_ids": None,
            }[change]
        if target == "assistant":
            frame = tool_frame(session, prompt_id, "tool-1") | declarations
            with pytest.raises(BridgeError, match="prompt"):
                adapter._observe_wire(frame)
        else:
            await sdk.clients[0].queue.put(terminal(session, prompt_id, **declarations))
        with pytest.raises(BridgeError, match="prompt"):
            await task


async def test_absent_hook_prompt_id_requires_each_tool_to_be_directly_attributed(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        adapter._observe_wire(tool_frame(session, prompt_id, "tool-1"))
        first = hook_input(session, str(uuid4()), "tool-1")
        del first["prompt_id"]
        assert await adapter._pre_tool(first, "tool-1", {}) == {}
        assert adapter._native_hook_prompt is None
        adapter._observe_wire(tool_frame(session, None, "tool-2", message_id="msg-2"))
        second = hook_input(session, str(uuid4()), "tool-2")
        del second["prompt_id"]
        with pytest.raises(BridgeError, match="prompt"):
            await adapter._pre_tool(second, "tool-2", {})
        with pytest.raises(BridgeError):
            await task


@pytest.mark.parametrize("phase", ["before", "during"])
async def test_latched_protocol_failure_can_never_return_permission_allow(
    adapter_type: Any, sdk: Any, tmp_path: Path, phase: str
) -> None:
    called = False
    active: Any = None

    async def decide(_: dict[str, Any]) -> str:
        nonlocal called
        called = True
        if phase == "during":
            active._fail(BridgeError("Native attribution conflict"))
        return "allow"

    async with adapter_type("claude", str(tmp_path), update, decide) as adapter:
        active = adapter
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        if phase == "before":
            adapter._fail(BridgeError("Native attribution conflict"))
        with pytest.raises(BridgeError, match="attribution"):
            await adapter._permission(
                "Read",
                {"file_path": "README.md"},
                SimpleNamespace(tool_use_id="call-1", agent_id=None, title="Read file"),
            )
        assert called == (phase == "during")
        with pytest.raises(BridgeError):
            await task


async def test_success_subtype_with_native_error_is_failed(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    sdk.respond = respond_with(
        lambda m: terminal(
            m["session_id"], m["uuid"], is_error=True, api_error_status=429, text="API Error"
        )
    )
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        result = await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
        assert result.status == "failed"
        assert result.evidence["api_error_status"] == 429


async def test_final_text_is_not_fabricated_from_assistant_chunks(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async def respond(client: FakeSDKClient, message: dict[str, Any]) -> None:
        await client.queue.put(
            FakeAssistant(session_id=message["session_id"], content=[{"text": "Partial answer"}])
        )
        await client.queue.put(terminal(message["session_id"], message["uuid"], text=None))

    sdk.respond = respond
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        result = await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
        assert result.text is None


async def test_eof_without_terminal_result_fails_explicitly(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    sdk.respond = respond_with(lambda _: _EOF)
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError, match="terminal|ended|closed"):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


async def test_cancel_ack_does_not_complete_without_attributed_aborted_result(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        await adapter.cancel(session)
        assert sdk.clients[0].interrupts == 1
        assert not task.done()
        await sdk.clients[0].queue.put(
            terminal(session, prompt_id, terminal_reason="aborted_tools", text=None)
        )
        assert (await task).status == "cancelled"


async def test_cancel_timeout_retires_permissions_and_refuses_retry(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: cancel must retire permissions even without prompt's own cleanup."""
    monkeypatch.setattr("answerback_bridge.native_claude.SHUTDOWN_TIMEOUT", 0.02)
    waiting = asyncio.Event()
    retired = asyncio.Event()

    async def decide(_: dict[str, Any]) -> str:
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()
        return "allow"

    async with adapter_type("claude", str(tmp_path), update, decide) as adapter:
        session = await adapter.open_session()
        # Isolate cancel's cleanup from the separate finally in prompt().
        adapter._active_prompt = str(uuid4())
        client = sdk.clients[0]

        async def interrupt() -> None:
            client.interrupts += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "interrupt", interrupt)
        permission = asyncio.create_task(
            client.options.can_use_tool(
                "Read", {"file_path": "README.md"}, SimpleNamespace(tool_use_id="pending-read")
            )
        )
        try:
            async with asyncio.timeout(1):
                await waiting.wait()
                with pytest.raises(BridgeError, match="interrupt was not acknowledged") as first:
                    await adapter.cancel(session)
                assert isinstance(first.value.__cause__, TimeoutError)
                assert retired.is_set()
                assert permission.cancelled()
                assert not adapter._permissions
                assert not client.disconnected
                with pytest.raises(BridgeError, match="interrupt was not acknowledged") as retry:
                    await adapter.cancel(session)
                assert retry.value is first.value
                assert client.interrupts == 1
        finally:
            permission.cancel()
            await asyncio.gather(permission, return_exceptions=True)
    assert client.disconnected


async def test_cancel_timeout_settles_the_active_prompt(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: timeout failure wakes the real prompt waiter without a terminal result."""
    monkeypatch.setattr("answerback_bridge.native_claude.SHUTDOWN_TIMEOUT", 0.02)
    sent = asyncio.Event()

    async def respond(client: FakeSDKClient, message: dict[str, Any]) -> None:
        sent.set()

    sdk.respond = respond
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        client = sdk.clients[0]

        async def interrupt() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "interrupt", interrupt)
        prompt = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        try:
            async with asyncio.timeout(1):
                await sent.wait()
                with pytest.raises(BridgeError, match="interrupt was not acknowledged"):
                    await adapter.cancel(session)
                with pytest.raises(BridgeError, match="interrupt was not acknowledged"):
                    await prompt
                assert adapter._active_prompt is None
        finally:
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
    assert client.disconnected


async def test_unknown_session_concurrent_prompt_and_reused_prompt_are_refused(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        with pytest.raises(BridgeError):
            await adapter.prompt(str(uuid4()), "Task", prompt_id=prompt_id)
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        with pytest.raises(BridgeError, match="flight|active"):
            await adapter.prompt(session, "Second", prompt_id=str(uuid4()))
        await sdk.clients[0].queue.put(terminal(session, prompt_id))
        await task
        with pytest.raises(BridgeError, match="reused|already"):
            await adapter.prompt(session, "Replay", prompt_id=prompt_id)
        assert len(sdk.clients[0].sent) == 1


async def test_missing_optional_sdk_is_actionable_and_does_not_install(
    adapter_type: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(BridgeError, match=r"answerback\[claude\]"):
        async with adapter_type("claude", str(tmp_path), update, approve):
            pass


@pytest.mark.parametrize("choice,behavior", [("allow", "allow"), ("deny", "deny"), (None, "deny")])
async def test_permission_decisions_are_one_time_and_preserve_original_input(
    adapter_type: Any, sdk: Any, tmp_path: Path, choice: str | None, behavior: str
) -> None:
    seen: list[dict[str, Any]] = []

    async def decide(request: dict[str, Any]) -> str | None:
        seen.append(request)
        request["tool_call"]["rawInput"]["command"] = "changed after display"
        return choice

    async with adapter_type("claude", str(tmp_path), update, decide) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        result = await sdk.clients[0].options.can_use_tool(
            "Bash",
            {"command": "touch example"},
            SimpleNamespace(tool_use_id="call-1", agent_id=None, title="Create example"),
        )
        assert result.behavior == behavior
        assert seen[0]["session_id"] == session
        assert seen[0]["tool_call"]["toolCallId"] == "call-1"
        assert {o["kind"] for o in seen[0]["options"]} == {"allow_once", "reject_once"}
        if behavior == "allow":
            assert result.updated_input == {"command": "touch example"}
            assert getattr(result, "updated_permissions", None) is None
        else:
            assert result.interrupt is (choice is None)
        await sdk.clients[0].queue.put(terminal(session, prompt_id))
        await task


@pytest.mark.parametrize("bad", ["AskUserQuestion", "Agent", "background", "subagent"])
async def test_unsupported_interactions_fail_task_before_native_execution(
    adapter_type: Any, sdk: Any, tmp_path: Path, bad: str
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        tool = bad if bad in {"AskUserQuestion", "Agent"} else "Bash"
        input_data = {"run_in_background": True} if bad == "background" else {}
        with pytest.raises(BridgeError, match="unsupported|background|subagent"):
            await sdk.clients[0].options.can_use_tool(
                tool,
                input_data,
                SimpleNamespace(
                    tool_use_id="call-1",
                    agent_id="child-1" if bad == "subagent" else None,
                    title=None,
                ),
            )
        with pytest.raises(BridgeError):
            await task


async def test_pretool_guard_blocks_background_even_if_native_permission_would_allow(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        guard = sdk.clients[0].options.hooks["PreToolUse"][0].hooks[0]
        with pytest.raises(BridgeError, match="background"):
            await guard(
                {
                    "session_id": session,
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sleep 1", "run_in_background": True},
                },
                "call-1",
                {},
            )
        with pytest.raises(BridgeError):
            await task


async def test_elicitation_is_cancelled_explicitly_and_fails_task(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        callback = sdk.clients[0].options.hooks["Elicitation"][0].hooks[0]
        result = await callback({"session_id": session}, None, {})
        assert result["hookSpecificOutput"] == {"hookEventName": "Elicitation", "action": "cancel"}
        with pytest.raises(BridgeError, match="elicitation"):
            await task


async def test_cancelled_sdk_permission_retires_human_callback(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    waiting = asyncio.Event()
    retired = asyncio.Event()

    async def decide(_: dict[str, Any]) -> str:
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()
        return "allow"

    async with adapter_type("claude", str(tmp_path), update, decide) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        permission = asyncio.create_task(
            sdk.clients[0].options.can_use_tool(
                "Bash", {}, SimpleNamespace(tool_use_id="call-1", agent_id=None, title=None)
            )
        )
        await waiting.wait()
        permission.cancel()
        await asyncio.gather(permission, return_exceptions=True)
        assert retired.is_set()
        await sdk.clients[0].queue.put(
            terminal(session, prompt_id, terminal_reason="aborted_tools")
        )
        assert (await task).status == "cancelled"


@pytest.mark.parametrize(
    "servers",
    [
        [{"name": "x", "command": "a"}, {"name": "x", "command": "b"}],
        [
            {
                "name": "x",
                "command": "a",
                "env": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}],
            }
        ],
        [{"name": "x", "type": "unknown", "url": "https://example.test"}],
    ],
)
async def test_invalid_mcp_config_is_refused_before_sdk_spawn(
    adapter_type: Any, sdk: Any, tmp_path: Path, servers: list[dict[str, Any]]
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(BridgeError):
            await adapter.open_session(mcp_servers=servers)
    assert sdk.clients == []


async def test_empty_resume_id_is_rejected_instead_of_starting_an_untracked_session(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(BridgeError, match="UUID"):
            await adapter.open_session("")
    assert sdk.clients == []


async def test_malformed_initialization_version_has_an_explicit_adapter_error(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    sdk.info["claude_code_version"] = 100
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(BridgeError):
            await adapter.open_session()


async def test_late_hook_for_a_different_prompt_is_rejected(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        guard = sdk.clients[0].options.hooks["PreToolUse"][0].hooks[0]
        with pytest.raises(BridgeError, match="prompt"):
            await guard(
                {
                    "session_id": session,
                    "prompt_id": str(uuid4()),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo example"},
                },
                "call-1",
                {},
            )
        with pytest.raises(BridgeError):
            await task


async def test_unknown_tool_family_is_not_treated_as_an_ordinary_permission(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        with pytest.raises(BridgeError, match="unsupported"):
            await sdk.clients[0].options.can_use_tool(
                "NewInteractiveQuestion",
                {},
                SimpleNamespace(tool_use_id="call-1", agent_id=None, title=None),
            )
        with pytest.raises(BridgeError):
            await task


async def test_duplicate_native_terminal_is_refused(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async def respond(client: FakeSDKClient, message: dict[str, Any]) -> None:
        event = terminal(message["session_id"], message["uuid"])
        await client.queue.put(event)
        await client.queue.put(event)

    sdk.respond = respond
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError, match="duplicat"):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


@pytest.mark.parametrize(
    "fields",
    [
        {"result": "x" * 200_001},
        {"is_error": "false"},
        {"user_message_uuids": "not-a-list"},
        {"errors": {"message": "wrong shape"}},
        {"uuid": "x" * 2000},
    ],
)
async def test_malformed_or_oversized_native_terminal_is_refused(
    adapter_type: Any, sdk: Any, tmp_path: Path, fields: dict[str, Any]
) -> None:
    sdk.respond = respond_with(lambda m: terminal(m["session_id"], m["uuid"], **fields))
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


async def test_unknown_permission_answer_is_not_silently_denied(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async def bad_answer(_: dict[str, Any]) -> str:
        return "allow_always"

    async with adapter_type("claude", str(tmp_path), update, bad_answer) as adapter:
        session = await adapter.open_session()
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        await asyncio.sleep(0)
        with pytest.raises(BridgeError, match="one-time"):
            await sdk.clients[0].options.can_use_tool(
                "Bash", {}, SimpleNamespace(tool_use_id="call-1", agent_id=None, title=None)
            )
        with pytest.raises(BridgeError):
            await task


@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_empty_native_result_is_unavailable_for_shared_completion(
    adapter_type: Any, sdk: Any, tmp_path: Path, text: str
) -> None:
    sdk.respond = respond_with(lambda m: terminal(m["session_id"], m["uuid"], text=text))
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        assert (await adapter.prompt(session, "Task", prompt_id=str(uuid4()))).text is None


async def test_non_list_mcp_configuration_is_rejected(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        with pytest.raises(BridgeError):
            await adapter.open_session(mcp_servers={})
    assert sdk.clients == []


async def test_success_terminal_while_human_permission_is_pending_is_inconsistent(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    waiting = asyncio.Event()
    retired = asyncio.Event()

    async def decide(_: dict[str, Any]) -> str:
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()
        return "allow"

    async with adapter_type("claude", str(tmp_path), update, decide) as adapter:
        session = await adapter.open_session()
        prompt_id = str(uuid4())
        task = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=prompt_id))
        await asyncio.sleep(0)
        permission = asyncio.create_task(
            sdk.clients[0].options.can_use_tool(
                "Bash", {}, SimpleNamespace(tool_use_id="call-1", agent_id=None, title=None)
            )
        )
        await waiting.wait()
        await sdk.clients[0].queue.put(terminal(session, prompt_id))
        with pytest.raises(BridgeError, match="permission"):
            await task
        await asyncio.gather(permission, return_exceptions=True)
        assert retired.is_set()


async def test_terminal_without_native_idle_retains_uncertainty(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerback_bridge.native_claude.SETTLE_TIMEOUT", 0.01, raising=False)
    sdk.respond = respond_with(lambda m: terminal(m["session_id"], m["uuid"]))
    async with adapter_type("claude", str(tmp_path), update, approve) as adapter:
        session = await adapter.open_session()
        sdk.clients[0].transport.delegate.emit_idle = False
        with pytest.raises(BridgeError, match="idle"):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


async def test_valid_terminal_identity_is_reported_before_idle_timeout(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("answerback_bridge.native_claude.SETTLE_TIMEOUT", 0.01)
    updates: list[dict[str, Any]] = []
    result_id = str(uuid4())

    async def remember(event: dict[str, Any]) -> None:
        updates.append(event)

    sdk.respond = respond_with(lambda m: terminal(m["session_id"], m["uuid"], uuid=result_id))
    async with adapter_type("claude", str(tmp_path), remember, approve) as adapter:
        session = await adapter.open_session()
        sdk.clients[0].transport.delegate.emit_idle = False
        with pytest.raises(BridgeError, match="idle"):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
    assert {"session_id": session, "turn_id": result_id} in [
        {"session_id": event.get("session_id"), "turn_id": event.get("turn_id")}
        for event in updates
    ]


async def test_mismatched_result_never_reports_a_verified_terminal_identity(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    updates: list[dict[str, Any]] = []

    async def remember(event: dict[str, Any]) -> None:
        updates.append(event)

    sdk.respond = respond_with(lambda m: terminal(m["session_id"], str(uuid4())))
    async with adapter_type("claude", str(tmp_path), remember, approve) as adapter:
        session = await adapter.open_session()
        with pytest.raises(BridgeError):
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
    assert not any(event.get("turn_id") for event in updates)


async def test_native_provider_settings_survive_without_ambient_execution_settings(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "native"
    home.mkdir()
    selected = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_MANTLE": "1",
        "AWS_REGION": "fixture-region",
        "AWS_PROFILE": "fixture-profile",
        "ANTHROPIC_MODEL": "fixture-model",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "fixture-fable",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    for key in selected:
        monkeypatch.delenv(key, raising=False)
    settings = {
        "env": {
            **selected,
            "PATH": "/unwanted",
            "BASH_ENV": "/unwanted/hook",
            "CLAUDE_CONFIG_DIR": "/unwanted/profile",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_MEM_MODEL": "unwanted",
        },
        "permissions": {"allow": ["Bash(*)"]},
        "hooks": {"SessionStart": [{"command": "unwanted"}]},
        "enabledPlugins": {"unwanted": True},
    }
    path = home / "settings.json"
    original = json.dumps(settings)
    path.write_text(original)
    async with adapter_type("claude", str(tmp_path), update, approve, home=home) as adapter:
        await adapter.open_session()
        options = sdk.clients[0].options
        for key, value in selected.items():
            assert options.env[key] == value
        assert options.env["CLAUDE_CONFIG_DIR"] == str(home)
        assert not {
            "PATH",
            "BASH_ENV",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
            "CLAUDE_MEM_MODEL",
        } & (options.env.keys())
        assert options.allowed_tools == []
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.skills == []
        assert set(options.hooks) == {"PreToolUse", "Elicitation"}
    assert path.read_text() == original


async def test_process_provider_environment_keeps_precedence_even_when_empty(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "native"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"env": {"AWS_REGION": "from-file", "AWS_PROFILE": "from-file"}})
    )
    monkeypatch.setenv("AWS_REGION", "from-process")
    monkeypatch.setenv("AWS_PROFILE", "")
    async with adapter_type("claude", str(tmp_path), update, approve, home=home) as adapter:
        await adapter.open_session()
        env = sdk.clients[0].options.env
        assert "AWS_REGION" not in env
        assert "AWS_PROFILE" not in env


@pytest.mark.parametrize(
    "settings",
    [
        '{"env":',
        '{"env":[]}',
        '{"env":{"CLAUDE_CODE_USE_BEDROCK":true}}',
        '{"env":{"AWS_REGION":"one","AWS_REGION":"two"}}',
        '{"env":{"AWS_REGION":"bad\\u0000value"}}',
    ],
)
async def test_invalid_native_provider_settings_fail_before_process_creation(
    adapter_type: Any, sdk: Any, tmp_path: Path, settings: str
) -> None:
    home = tmp_path / "native"
    home.mkdir()
    (home / "settings.json").write_text(settings)
    async with adapter_type("claude", str(tmp_path), update, approve, home=home) as adapter:
        with pytest.raises(BridgeError, match="settings|environment"):
            await adapter.open_session()
    assert sdk.clients == []


async def test_oversized_native_provider_settings_fail_before_process_creation(
    adapter_type: Any, sdk: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_claude, "MAX_SETTINGS_BYTES", 4096)
    home = tmp_path / "native"
    home.mkdir()
    (home / "settings.json").write_text('{"padding":"' + "x" * 4096 + '"}')
    async with adapter_type("claude", str(tmp_path), update, approve, home=home) as adapter:
        with pytest.raises(BridgeError, match="exceed the supported bound"):
            await adapter.open_session()
    assert sdk.clients == []


def test_settings_bound_is_inclusive_and_the_refusal_names_the_remedy(tmp_path: Path) -> None:
    bound = native_claude.MAX_SETTINGS_BYTES
    assert bound == 8 * 1024 * 1024
    path = tmp_path / "settings.json"
    head = b'{"env":{"AWS_REGION":"fixture-region"},"padding":"'
    tail = b'"}'
    path.write_bytes(head + b"x" * (bound - len(head) - len(tail)) + tail)
    assert path.stat().st_size == bound
    assert native_claude._provider_environment(tmp_path, {}) == {"AWS_REGION": "fixture-region"}
    # One appended byte, so the 8 MiB body is written only once in the whole module.
    with path.open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(BridgeError) as refused:
        native_claude._provider_environment(tmp_path, {})
    message = str(refused.value)
    assert "exceed the supported bound" in message
    assert f"bound of {bound} bytes" in message
    assert f"settings.json is {bound + 1} bytes" in message
    assert "only the env member is read" in message
    assert "shrink the file" in message
    assert "--home" in message
    assert "fixture-region" not in message


def test_bulky_unrelated_settings_member_does_not_hide_provider_environment(
    tmp_path: Path,
) -> None:
    selected = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "fixture-region"}
    projects = {
        f"/fixture/workspaces/{index:04d}/{'p' * 140}": {"hasTrustDialogAccepted": True}
        for index in range(3000)
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"projects": projects, "env": selected}))
    assert 550_000 < path.stat().st_size < 650_000
    assert native_claude._provider_environment(tmp_path, {}) == selected


class _GrowsAfterFstat:
    """Stand in for os inside the adapter: the file gains a byte once its size is known."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def fstat(self, descriptor: int) -> os.stat_result:
        info = os.fstat(descriptor)
        with self._path.open("ab") as stream:
            stream.write(b" ")
        return info


def test_settings_that_grow_after_the_size_check_are_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = 4096
    monkeypatch.setattr(native_claude, "MAX_SETTINGS_BYTES", bound)
    path = tmp_path / "settings.json"
    head = '{"env":{"AWS_REGION":"fixture-region"},"padding":"'
    path.write_text(head + "x" * (bound - len(head) - 2) + '"}')
    assert path.stat().st_size == bound
    assert native_claude._provider_environment(tmp_path, {}) == {"AWS_REGION": "fixture-region"}
    monkeypatch.setattr(native_claude, "os", _GrowsAfterFstat(path))
    with pytest.raises(BridgeError) as refused:
        native_claude._provider_environment(tmp_path, {})
    message = str(refused.value)
    assert path.stat().st_size == bound + 1
    assert "exceed the supported bound" in message
    assert f"bound of {bound} bytes" in message
    assert "only the env member is read" in message
    assert "--home" in message
    assert "fixture-region" not in message


async def test_native_settings_must_be_a_regular_file(
    adapter_type: Any, sdk: Any, tmp_path: Path
) -> None:
    home = tmp_path / "native"
    (home / "settings.json").mkdir(parents=True)
    async with adapter_type("claude", str(tmp_path), update, approve, home=home) as adapter:
        with pytest.raises(BridgeError, match="settings"):
            await adapter.open_session()
    assert sdk.clients == []


async def test_actual_sdk_forwards_provider_environment_with_execution_scope_retained(
    adapter_type: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("claude_agent_sdk")
    home = tmp_path / "native"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_REGION": "file-region",
                    "ANTHROPIC_MODEL": "fixture-model",
                    "BASH_ENV": "/unwanted/hook",
                },
                "permissions": {"allow": ["Bash(*)"]},
                "hooks": {"SessionStart": [{"command": "unwanted"}]},
            }
        )
    )
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("BASH_ENV", raising=False)
    monkeypatch.setenv("AWS_REGION", "process-region")
    executable = tmp_path / "native-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        " if request.get('type')!='control_request': continue\n"
        " info={'claude_code_version':'2.1.322','session_state':'idle',"
        "'current_permission_mode':'default','hook_on_failure_supported':True,"
        "'hooks_applied':True,'fixture_env':{k:os.environ.get(k) for k in "
        "['CLAUDE_CODE_USE_BEDROCK','AWS_REGION','ANTHROPIC_MODEL','BASH_ENV']},"
        "'fixture_argv':sys.argv[1:],'fixture_hooks':list(request['request'].get('hooks',{}))}\n"
        " print(json.dumps({'type':'control_response','response':{'subtype':'success',"
        "'request_id':request['request_id'],'response':info}}),flush=True)\n"
    )
    executable.chmod(0o700)
    async with adapter_type(str(executable), str(tmp_path), update, approve, home=home) as adapter:
        await adapter.open_session()
        info = await adapter._client.get_server_info()
        assert info["fixture_env"] == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "process-region",
            "ANTHROPIC_MODEL": "fixture-model",
            "BASH_ENV": None,
        }
        assert "--setting-sources=" in info["fixture_argv"]
        assert "--strict-mcp-config" in info["fixture_argv"]
        assert "--settings" not in info["fixture_argv"]
        assert set(info["fixture_hooks"]) == {"PreToolUse", "Elicitation"}


def test_cli_version_refusal_names_the_configured_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the refusal text aligned with the minimum version constant."""
    monkeypatch.setattr(native_claude, "MINIMUM_CLI_VERSION", (9, 8, 7))

    assert str(native_claude._cli_version_error("2.1.330")) == (
        "Claude CLI 9.8.7 or newer is required for this protocol"
    )
