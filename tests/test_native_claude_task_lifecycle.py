"""Foreground task notifications through the installed SDK; no native client runs."""

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from answerback_bridge import native_claude
from answerback_bridge.native_claude import ClaudeCLI
from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.process import BridgeError

sdk = pytest.importorskip("claude_agent_sdk")


def lifecycle(session: str, call: str) -> list[dict[str, Any]]:
    """Captured Claude 2.1.322 Bash start/end shape, with synthetic identities.

    task_progress uses the pinned SDK 0.2.182 schema; no progress event was
    emitted by the incident's short foreground Bash command.
    """
    shared = {
        "type": "system",
        "session_id": session,
        "task_id": "foreground-task",
        "tool_use_id": call,
        "run_id": "native-run",
    }
    return [
        {
            **shared,
            "subtype": "task_started",
            "uuid": str(uuid4()),
            "description": "Run the full project test suite",
            "task_type": "local_bash",
            "is_backgrounded": False,
        },
        {
            **shared,
            "subtype": "task_progress",
            "uuid": str(uuid4()),
            "description": "Run the full project test suite",
            "usage": {"total_tokens": 0, "tool_uses": 1, "duration_ms": 1000},
        },
        {
            **shared,
            "subtype": "task_notification",
            "uuid": str(uuid4()),
            "status": "completed",
            "output_file": "",
            "summary": "Run the full project test suite",
        },
    ]


CANCEL_DAMAGE = (
    "cancel_wrong_session",
    "cancel_agent",
    "cancel_child_context",
    "cancel_background",
    "cancel_subagent",
    "cancel_wrong_task",
    "cancel_wrong_tool",
)

# Post-cancel frames that pass the foreground qualification but break the
# identity or admission guards, with the refusal each must still produce.
CANCEL_GUARDS = {
    "cancel_malformed_uuid": "task event ID must be a canonical UUID",
    "cancel_duplicate_uuid": "lifecycle identity is invalid or duplicated",
    "cancel_unadmitted_start": "lacks its admitted Bash identity",
}

# Cancel scenarios that keep every frame qualified; the peer decides what Claude
# emits after the interrupt instead of damaging the stopped frame.
CANCEL_SHAPES = ("cancel", "cancel_blocked", "cancel_unwitnessed")


def stopped_after_interrupt(session: str, call: str, damage: str | None) -> dict[str, Any]:
    """Claude's lifecycle frame for a foreground Bash that an interrupt killed.

    The pinned SDK documents the CLI mapping a killed task to ``task_notification``
    with ``status: stopped``; ``damage`` turns it into a frame the adapter must
    still refuse after its own cancel.
    """
    frame = {
        **lifecycle(session, call)[-1],
        "status": "stopped",
        "summary": "Count to 100 with one second sleeps",
    }
    if damage == "cancel_wrong_session":
        frame["session_id"] = str(uuid4())
    elif damage == "cancel_agent":
        frame["agent_id"] = "subagent"
    elif damage == "cancel_child_context":
        frame["parent_tool_use_id"] = "subagent-call"
    elif damage == "cancel_background":
        frame["is_backgrounded"] = True
    elif damage == "cancel_subagent":
        frame["task_type"] = "local_agent"
    elif damage == "cancel_wrong_task":
        frame["task_id"] = "another-task"
    elif damage == "cancel_wrong_tool":
        frame["tool_use_id"] = "another-call"
    return frame


class LifecyclePeer(sdk.Transport):
    """Real Query/parser/hook dispatch over inert captured-shape frames."""

    def __init__(self, damage: str | None = None) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.hook = ""
        self.message: dict[str, Any] = {}
        self.damage = damage
        self.cancel = (
            damage in CANCEL_SHAPES
            or damage in CANCEL_DAMAGE
            or damage in CANCEL_GUARDS
            or (damage is not None and damage.startswith("cancel_flood_"))
        )
        self.hook_responses: list[dict] = []
        self.frames: list[dict] = []
        self.after_task_hook = asyncio.Event()
        self.bash_running = asyncio.Event()
        self.interrupts = 0
        # Tool use identities are never reused across prompts; a later prompt
        # on the same peer sets a prefix before it is sent.
        self.prefix = ""

    async def connect(self) -> None:
        pass

    async def emit(self, frame: dict) -> None:
        self.frames.append(copy.deepcopy(frame))
        await self.queue.put(frame)

    async def tool(self, call: str, name: str, payload: dict) -> None:
        session, prompt = self.message["session_id"], self.message["uuid"]
        await self.emit(
            {
                "type": "assistant",
                "session_id": session,
                "parent_tool_use_id": None,
                "uuid": str(uuid4()),
                "user_message_uuid": prompt,
                "user_message_uuids": [prompt],
                "message": {
                    "id": "message-" + call,
                    "role": "assistant",
                    "model": "fixture",
                    "content": [{"type": "tool_use", "id": call, "name": name, "input": payload}],
                },
            }
        )
        await self.emit(
            {
                "type": "control_request",
                "request_id": call,
                "request": {
                    "subtype": "hook_callback",
                    "callback_id": self.hook,
                    "tool_use_id": call,
                    "input": {
                        "session_id": session,
                        "hook_event_name": "PreToolUse",
                        "tool_name": name,
                        "tool_input": payload,
                        "tool_use_id": call,
                        "prompt_id": prompt,
                        "cwd": self.message.get("cwd", "/fixture"),
                    },
                },
            }
        )

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            request = message["request"]
            if request["subtype"] == "interrupt":
                await self.interrupted(message["request_id"])
                return
            assert request["subtype"] == "initialize"
            self.hook = request["hooks"]["PreToolUse"][0]["hookCallbackIds"][0]
            await self.emit(
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
            self.message = message
            await self.tool(
                self.prefix + "foreground-bash", "Bash", {"command": "uv run --offline pytest -q"}
            )
        elif message["type"] == "control_response":
            response = message["response"]
            self.hook_responses.append(response)
            if response["subtype"] != "success":
                return
            if response["request_id"] == self.prefix + "foreground-bash":
                if self.damage == "cancel_blocked":
                    # Native policy asks for the one-time permission; no task
                    # starts while the answer is pending.
                    await self.emit(
                        {
                            "type": "control_request",
                            "request_id": "permission-" + response["request_id"],
                            "request": {
                                "subtype": "can_use_tool",
                                "tool_name": "Bash",
                                "input": {"command": "uv run --offline pytest -q"},
                                "tool_use_id": self.prefix + "foreground-bash",
                            },
                        }
                    )
                    return
                events = lifecycle(self.message["session_id"], self.prefix + "foreground-bash")
                if self.damage == "background":
                    events[0]["is_backgrounded"] = True
                elif self.damage == "missing_background":
                    events[0].pop("is_backgrounded")
                elif self.damage == "subagent":
                    events[0]["task_type"] = "local_agent"
                elif self.damage == "wrong_session":
                    events[0]["session_id"] = str(uuid4())
                elif self.damage == "wrong_tool":
                    events[0]["tool_use_id"] = "another-call"
                elif self.damage == "missing_tool":
                    events[1].pop("tool_use_id")
                elif self.damage == "child_context":
                    events[1]["parent_tool_use_id"] = "subagent-call"
                elif self.damage == "invalid_background_flag":
                    events[0]["is_backgrounded"] = 0
                elif self.damage == "duplicate_event":
                    events[1] = dict(events[0])
                elif self.damage == "wrong_task":
                    events[1]["task_id"] = "another-task"
                elif self.damage == "wrong_run":
                    events[1]["run_id"] = "another-run"
                elif self.damage == "progress_without_start":
                    events.pop(0)
                elif self.damage == "unknown_status":
                    events[-1]["status"] = "maybe-complete"
                elif self.damage == "background_patch":
                    events[1] = {
                        **events[1],
                        "subtype": "task_updated",
                        "patch": {"status": "running"},
                    }
                elif self.damage == "unknown_task":
                    events[1]["subtype"] = "task_unrecognized"
                elif self.damage == "unsettled":
                    events.pop()
                elif self.damage in {"failed", "stopped"}:
                    events[-1]["status"] = self.damage
                elif self.damage == "optional_run_omitted":
                    events[1].pop("run_id")
                    events[-1].pop("run_id")
                if self.cancel:
                    # The Bash is still running: no terminal notification until an
                    # interrupt arrives, exactly as a long-lived foreground command.
                    events.pop()
                for event in events:
                    await self.emit(event)
                if self.cancel:
                    self.bash_running.set()
                    return
                await self.tool(
                    self.prefix + "after-task-mcp",
                    "mcp__answerback_actor__actor_receive",
                    {"wait_seconds": 0},
                )
            elif response["request_id"] == self.prefix + "after-task-mcp":
                self.after_task_hook.set()
                await self.finish("completed", "Native completed answer")

    async def interrupted(self, request_id: str) -> None:
        """Acknowledge the SDK interrupt, then end the killed Bash and the turn."""
        self.interrupts += 1
        await self.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": {}},
            }
        )
        session = self.message["session_id"]
        call = self.prefix + "foreground-bash"
        stopped = stopped_after_interrupt(session, call, self.damage)
        if self.damage is not None and self.damage.startswith("cancel_flood_"):
            # A long-lived Bash keeps reporting progress while the interrupt lands.
            for _ in range(int(self.damage.removeprefix("cancel_flood_"))):
                await self.emit({**lifecycle(session, call)[1], "uuid": str(uuid4())})
        elif self.damage == "cancel_malformed_uuid":
            stopped["uuid"] = stopped["uuid"].upper()
        elif self.damage == "cancel_duplicate_uuid":
            started = next(f for f in self.frames if f.get("subtype") == "task_started")
            stopped["uuid"] = started["uuid"]
        elif self.damage == "cancel_unadmitted_start":
            # A second Bash "starts" after our interrupt; its tool use was never
            # pre-tool-qualified or allowed, so it cannot become a foreground task.
            await self.emit(
                {
                    **lifecycle(session, "another-call")[0],
                    "task_id": "another-task",
                    "uuid": str(uuid4()),
                }
            )
        if self.damage not in {"cancel_blocked", "cancel_unwitnessed"}:
            await self.emit(stopped)
        await self.finish("aborted_tools", None)

    async def finish(self, reason: str, text: str | None) -> None:
        await self.emit(
            {
                "type": "result",
                "session_id": self.message["session_id"],
                "uuid": str(uuid4()),
                "user_message_uuid": self.message["uuid"],
                "user_message_uuids": [self.message["uuid"]],
                "subtype": "success",
                "is_error": False,
                "terminal_reason": reason,
                "stop_reason": "end_turn" if reason == "completed" else None,
                "result": text,
                "duration_ms": 1,
                "duration_api_ms": 1,
                "num_turns": 1,
            }
        )
        await self.emit(
            {
                "type": "system",
                "subtype": "session_state_changed",
                "sdk_host_only": True,
                "session_id": self.message["session_id"],
                "state": "idle",
            }
        )

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (frame := await self.queue.get()) is not None:
            yield frame

    async def close(self) -> None:
        await self.queue.put(None)

    async def end_input(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True


async def run_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str | None = None,
    *,
    second_prompt: bool = False,
) -> tuple[NativeResult, LifecyclePeer, list[dict[str, Any]], list[dict[str, Any]]]:
    peer = LifecyclePeer(damage)
    updates: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    progressed = asyncio.Event()
    blocked = asyncio.Event()

    async def update(value: dict[str, Any]) -> None:
        updates.append(value)
        if value.get("sessionUpdate") == "task_progress":
            progressed.set()

    async def permission(value: dict[str, Any]) -> str:
        grants.append(value)
        if damage == "cancel_blocked":
            # The person never answers; the cancel retires this wait.
            blocked.set()
            await asyncio.get_running_loop().create_future()
        return "deny"

    monkeypatch.setattr(native_claude, "_subprocess_transport", lambda options: peer)
    async with ClaudeCLI(
        "unused", str(tmp_path), update, permission, home=tmp_path / "home"
    ) as adapter:
        session = await adapter.open_session(
            mcp_servers=[
                {
                    "name": "answerback_actor",
                    "type": "stdio",
                    "command": "unused",
                    "args": [],
                    "env": [],
                }
            ]
        )
        prompt = asyncio.create_task(adapter.prompt(session, "Task", prompt_id=str(uuid4())))
        if damage == "cancel_blocked":
            await asyncio.wait_for(blocked.wait(), 3)
            assert not prompt.done()
            await adapter.cancel(session)
            assert peer.interrupts == 1
        elif peer.cancel:
            # Cancel only once the adapter has observed the running Bash, as the
            # live incident did (receipt `last_update: task_started`, ~20 s in).
            await asyncio.wait_for(peer.bash_running.wait(), 3)
            await asyncio.wait_for(progressed.wait(), 3)
            assert not prompt.done()
            await adapter.cancel(session)
            assert peer.interrupts == 1
        result = await asyncio.wait_for(prompt, 3)
        if second_prompt:
            # Same adapter and peer: a later, uncancelled prompt starts clean.
            peer.prefix, peer.damage, peer.cancel = "second-", None, False
            second = await asyncio.wait_for(
                adapter.prompt(session, "Task two", prompt_id=str(uuid4())), 3
            )
            return second, peer, updates, grants
        return result, peer, updates, grants


async def test_captured_foreground_bash_lifecycle_keeps_later_mcp_hook_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, peer, updates, grants = await run_case(tmp_path, monkeypatch)
    assert result.status == "completed" and result.evidence["native_idle"] is True
    assert peer.after_task_hook.is_set()
    assert [r["subtype"] for r in peer.hook_responses] == ["success", "success"]
    assert grants == []  # Notifications never invoke or manufacture a permission decision.
    assert {"task_started", "task_progress", "task_notification"} <= {
        row["sessionUpdate"] for row in updates
    }


@pytest.mark.parametrize(
    "damage",
    [
        "background",
        "missing_background",
        "subagent",
        "wrong_session",
        "wrong_tool",
        "wrong_task",
        "wrong_run",
        "progress_without_start",
        "unknown_status",
        "background_patch",
        "unknown_task",
        "unsettled",
        "missing_tool",
        "child_context",
        "invalid_background_flag",
        "duplicate_event",
    ],
)
async def test_unqualified_task_lifecycle_cannot_return_a_successful_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    with pytest.raises(BridgeError):
        await run_case(tmp_path, monkeypatch, damage)


@pytest.mark.parametrize("shape", ["failed", "stopped", "optional_run_omitted"])
async def test_task_observation_does_not_decide_the_model_turn_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    result, peer, _, grants = await run_case(tmp_path, monkeypatch, shape)
    assert result.status == "completed"
    assert peer.after_task_hook.is_set() and grants == []


async def test_cancel_during_foreground_bash_settles_cancelled_with_native_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-212: the stopped lifecycle our own interrupt causes is cancel evidence, not a fault."""
    result, peer, updates, grants = await run_case(tmp_path, monkeypatch, "cancel")
    assert result.status == "cancelled" and result.text is None
    assert result.evidence["terminal_reason"] == "aborted_tools"
    assert result.evidence["native_idle"] is True
    assert result.evidence["cancel_requested"] is True
    (stopped,) = result.evidence["cancel_lifecycle"]
    assert stopped["task_id"] == "foreground-task"
    assert stopped["tool_use_id"] == "foreground-bash"
    assert stopped["subtype"] == "task_notification"
    assert stopped["status"] == "stopped"
    assert isinstance(stopped["time"], float)
    assert grants == []
    observed = [row["sessionUpdate"] for row in updates if row["sessionUpdate"].startswith("task_")]
    assert observed == ["task_started", "task_progress", "task_notification"]


async def test_turn_without_cancel_reports_no_cancel_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _, _ = await run_case(tmp_path, monkeypatch)
    assert result.evidence["cancel_requested"] is False
    assert result.evidence["cancel_lifecycle"] == []


async def test_cancel_while_blocked_on_permission_settles_cancelled_without_a_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No task ran, so ``aborted_tools`` alone is the acknowledgement (the Linux live case)."""
    result, peer, updates, grants = await run_case(tmp_path, monkeypatch, "cancel_blocked")
    assert result.status == "cancelled" and result.text is None
    assert result.evidence["terminal_reason"] == "aborted_tools"
    assert result.evidence["cancel_requested"] is True
    assert result.evidence["cancel_lifecycle"] == []
    assert len(grants) == 1 and grants[0]["tool_call"]["toolCallId"] == "foreground-bash"
    assert not any(row["sessionUpdate"].startswith("task_") for row in updates)
    assert not any(frame.get("type") == "system" and "task_id" in frame for frame in peer.frames)


async def test_cancel_lifecycle_keeps_at_most_sixteen_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MAX_CANCEL_LIFECYCLE`` bounds the evidence: 15 progress frames plus the stop fit."""
    assert native_claude.MAX_CANCEL_LIFECYCLE == 16
    result, _, _, _ = await run_case(tmp_path, monkeypatch, "cancel_flood_15")
    assert result.status == "cancelled"
    entries = result.evidence["cancel_lifecycle"]
    assert len(entries) == 16
    assert [entry["subtype"] for entry in entries[:15]] == ["task_progress"] * 15
    assert entries[15]["subtype"] == "task_notification" and entries[15]["status"] == "stopped"


async def test_seventeenth_cancel_lifecycle_frame_is_dropped_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 17th qualified frame is processed but not recorded.

    Here it is the stop witness itself, so the bound never widens the evidence:
    the turn settles uncertain for the missing witness rather than raising a
    bound error or settling cancelled on sixteen progress entries.
    """
    with pytest.raises(BridgeError, match="stop witness for foreground task"):
        await run_case(tmp_path, monkeypatch, "cancel_flood_16")


async def test_cancel_lifecycle_resets_for_the_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, peer, updates, grants = await run_case(
        tmp_path, monkeypatch, "cancel", second_prompt=True
    )
    assert peer.interrupts == 1
    assert result.status == "completed" and result.text == "Native completed answer"
    assert result.evidence["cancel_requested"] is False
    assert result.evidence["cancel_lifecycle"] == []
    assert grants == []
    observed = [row["sessionUpdate"] for row in updates if row["sessionUpdate"].startswith("task_")]
    assert observed == ["task_started", "task_progress", "task_notification"] * 2


async def test_cancel_of_a_running_task_without_its_stop_witness_is_not_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aborted_tools`` alone cannot settle ``cancelled`` for a Bash we saw start.

    The turn raises, which ``native_workers`` records as ``uncertain`` with the
    detail below; nothing is fabricated into ``cancel_lifecycle``.
    """
    with pytest.raises(BridgeError, match="stop witness for foreground task 'foreground-task'"):
        await run_case(tmp_path, monkeypatch, "cancel_unwitnessed")


@pytest.mark.parametrize("damage", list(CANCEL_DAMAGE))
async def test_foreign_lifecycle_after_cancel_still_fails_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """A cancel does not widen the lifecycle the adapter accepts beyond its own Bash."""
    with pytest.raises(BridgeError, match="lifecycle"):
        await run_case(tmp_path, monkeypatch, damage)


@pytest.mark.parametrize(("damage", "match"), sorted(CANCEL_GUARDS.items()))
async def test_post_cancel_frames_keep_identity_and_admission_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, match: str
) -> None:
    """After our own cancel, the UUID and admitted-Bash checks still fail the turn.

    A malformed or reused event UUID on the stopped frame, or a post-cancel
    ``task_started`` for a tool use that was never admitted, each raise, so the
    worker records ``uncertain`` rather than a cancellation built on that frame.
    """
    with pytest.raises(BridgeError, match=match):
        await run_case(tmp_path, monkeypatch, damage)


@pytest.mark.parametrize("decision", ["pending", "deny", "allow", "allow_other_tool"])
async def test_task_event_cannot_supply_a_missing_tool_permission(
    tmp_path: Path, decision: str
) -> None:
    entered = asyncio.Event()
    selected = asyncio.get_running_loop().create_future()

    async def permission(_: dict) -> str:
        entered.set()
        return await selected

    async def update(_: dict) -> None:
        pass

    adapter = ClaudeCLI("unused", str(tmp_path), update, permission)
    session, prompt, call = str(uuid4()), str(uuid4()), "foreground-bash"
    adapter._sdk = sdk
    adapter._entered = True
    adapter._session_id, adapter._active_prompt = session, prompt
    adapter._failure = asyncio.get_running_loop().create_future()
    payload = {"command": "uv run --offline pytest -q"}
    adapter._observe_wire(
        {
            "type": "assistant",
            "session_id": session,
            "parent_tool_use_id": None,
            "user_message_uuid": prompt,
            "user_message_uuids": [prompt],
            "message": {
                "id": "native-message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call, "name": "Bash", "input": payload}],
            },
        }
    )
    await adapter._pre_tool(
        {
            "session_id": session,
            "tool_use_id": call,
            "tool_name": "Bash",
            "tool_input": payload,
            "prompt_id": prompt,
        },
        call,
        {},
    )
    pending = asyncio.create_task(
        adapter._permission(
            "Read" if decision == "allow_other_tool" else "Bash",
            payload,
            SimpleNamespace(tool_use_id=call, agent_id=None),
        )
    )
    try:
        await entered.wait()
        if decision != "pending":
            selected.set_result("allow" if decision == "allow_other_tool" else decision)
            await pending
        if decision == "allow":
            adapter._observe_wire(lifecycle(session, call)[0])
            assert len(adapter._foreground_tasks) == 1
        else:
            with pytest.raises(BridgeError, match="admitted Bash"):
                adapter._observe_wire(lifecycle(session, call)[0])
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("subtype", ["task_started", "task_progress", "task_notification"])
def test_lifecycle_names_do_not_authorize_control_requests(tmp_path: Path, subtype: str) -> None:
    async def update(_: dict[str, Any]) -> None:
        pass

    async def permission(_: dict[str, Any]) -> str:
        pytest.fail("A lifecycle frame must not request or supply a grant")

    adapter = ClaudeCLI("unused", str(tmp_path), update, permission)
    with pytest.raises(BridgeError, match="interactive request"):
        adapter._observe_wire(
            {"type": "control_request", "request_id": "control", "request": {"subtype": subtype}}
        )
