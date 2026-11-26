"""Independent regressions for the Claude adapter's installed-SDK boundary."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from answerback_bridge import native_claude
from answerback_bridge.native_claude import ClaudeCLI
from answerback_bridge.process import BridgeError

sdk = pytest.importorskip("claude_agent_sdk")


async def ignore(_: dict[str, Any]) -> None:
    pass


async def deny(_: dict[str, Any]) -> str:
    return "deny"


def native_result(session: str, prompt: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": session,
        "uuid": str(uuid4()),
        "user_message_uuid": prompt,
        "user_message_uuids": [prompt],
        "terminal_reason": "completed",
        "stop_reason": "end_turn",
        "result": "The attributed native result",
        **fields,
    }


class WirePeer(sdk.Transport):
    """Public SDK transport seam, preserving real Query and message_parser code."""

    def __init__(self, result_fields: dict[str, Any] | None = None) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.ready = False
        self.result_fields = result_fields or {}
        self.result_pair: str | None = None
        self.native_state: str | None = "idle"

    async def connect(self) -> None:
        self.ready = True

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            assert message["request"]["subtype"] == "initialize"
            await self.queue.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {
                            "claude_code_version": "2.1.322",
                            "session_state": "idle",
                            "current_permission_mode": "default",
                            "hook_on_failure_supported": True,
                            "hooks_applied": True,
                        },
                    },
                }
            )
        elif message["type"] == "user":
            result = native_result(message["session_id"], message["uuid"], **self.result_fields)
            if self.native_state is not None:
                await self.queue.put(
                    {
                        "type": "system",
                        "subtype": "session_state_changed",
                        "sdk_host_only": True,
                        "session_id": message["session_id"],
                        "state": self.native_state,
                    }
                )
            await self.queue.put(result)
            if self.result_pair is not None:
                other = dict(result)
                if self.result_pair == "conflicting":
                    other["result"] = "A contradictory final answer"
                if self.result_pair == "extra":
                    other["uuid"] = str(uuid4())
                    other["is_error"] = True
                await self.queue.put(other)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self.queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self.ready = False
        await self.queue.put(None)

    async def end_input(self) -> None:
        pass

    def is_ready(self) -> bool:
        return self.ready


async def test_installed_sdk_round_trip_retains_exact_prompt_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = WirePeer()
    monkeypatch.setattr(native_claude, "_subprocess_transport", lambda options: peer)
    async with ClaudeCLI(
        "unused-native-binary", str(tmp_path), ignore, deny, home=tmp_path / "claude-config"
    ) as adapter:
        session = await adapter.open_session()
        result = await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
        assert result.status == "completed"
        assert result.text == "The attributed native result"


async def test_deferred_native_failure_cannot_mark_session_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = WirePeer(
        {
            "is_error": True,
            "subtype": "error_during_execution",
            "terminal_reason": "deferred_tool_use",
            "deferred_tool_use": {
                "id": "pending-tool",
                "name": "Bash",
                "input": {"command": "echo deferred"},
            },
        }
    )
    monkeypatch.setattr(native_claude, "_subprocess_transport", lambda options: peer)
    async with ClaudeCLI(
        "unused-native-binary", str(tmp_path), ignore, deny, home=tmp_path / "claude-config"
    ) as adapter:
        session = await adapter.open_session()
        try:
            normalized = await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
        except BridgeError as error:
            assert "deferred" in str(error)
            return  # Explicit unsupported interaction keeps the reservation uncertain.
        assert normalized.evidence.get("resumable", True) is False


async def test_repeated_cancellation_waits_for_owned_cleanup(tmp_path: Path) -> None:
    disconnect_started = asyncio.Event()
    finish_disconnect = asyncio.Event()
    disconnect_finished = asyncio.Event()

    async def disconnect() -> None:
        disconnect_started.set()
        await finish_disconnect.wait()
        disconnect_finished.set()

    adapter = ClaudeCLI("unused", str(tmp_path), ignore, deny)
    adapter._client = SimpleNamespace(disconnect=disconnect)
    closing = asyncio.create_task(adapter.__aexit__())
    try:
        await disconnect_started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done(), "Raw cancellation abandoned owned SDK cleanup before completion"
        finish_disconnect.set()
        await asyncio.gather(closing, return_exceptions=True)
        assert disconnect_finished.is_set()
    finally:
        finish_disconnect.set()
        await asyncio.gather(closing, return_exceptions=True)
        if adapter._close_task is not None:
            await asyncio.gather(adapter._close_task, return_exceptions=True)


@pytest.mark.parametrize("pair", ["duplicate", "conflicting", "extra"])
async def test_observed_extra_native_terminals_cannot_be_silently_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pair: str
) -> None:
    peer = WirePeer()
    peer.result_pair = pair
    monkeypatch.setattr(native_claude, "_subprocess_transport", lambda options: peer)
    with pytest.raises(BridgeError, match="duplicat|unsolicited|conflict|terminal"):
        async with ClaudeCLI(
            "unused-native-binary", str(tmp_path), ignore, deny, home=tmp_path / "claude-config"
        ) as adapter:
            session = await adapter.open_session()
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))


@pytest.mark.parametrize("state", ["requires_action", "running", None])
async def test_native_unsettled_state_cannot_disappear_behind_sdk_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    peer = WirePeer()
    peer.native_state = state
    monkeypatch.setattr(native_claude, "_subprocess_transport", lambda options: peer)
    with pytest.raises(BridgeError, match="action|state|idle|unsettled"):
        async with ClaudeCLI(
            "unused-native-binary", str(tmp_path), ignore, deny, home=tmp_path / "claude-config"
        ) as adapter:
            session = await adapter.open_session()
            await adapter.prompt(session, "Task", prompt_id=str(uuid4()))
