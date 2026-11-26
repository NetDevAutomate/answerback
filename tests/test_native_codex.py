"""Behavioral protocol tests against an owned subprocess, with no real Codex calls."""

import asyncio
import importlib
import importlib.util
import json
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.process import BridgeError, CommandCleanupError


def transport():
    assert importlib.util.find_spec("answerback_bridge.native_codex") is not None, (
        "The native Codex transport is not implemented"
    )
    return importlib.import_module("answerback_bridge.native_codex").CodexCLI


@pytest.fixture
def agent(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "native_codex_agent.py"
    binary = tmp_path / "codex-fixture"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    (tmp_path / "codex-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


@pytest.fixture
def reap_test_descendants() -> Iterator[bool]:
    """Adopt this test's orphan on Linux rather than depending on container PID 1."""
    if sys.platform != "linux":
        yield False
        return
    import ctypes

    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.restype = ctypes.c_int
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    previous = ctypes.c_int()
    # PR_GET_CHILD_SUBREAPER / PR_SET_CHILD_SUBREAPER affect adoption, not wait/reap.
    assert prctl(37, ctypes.addressof(previous), 0, 0, 0) == 0, os.strerror(ctypes.get_errno())
    assert prctl(36, 1, 0, 0, 0) == 0, os.strerror(ctypes.get_errno())
    try:
        yield True
    finally:
        assert prctl(36, previous.value, 0, 0, 0) == 0, os.strerror(ctypes.get_errno())


def configure(agent, **settings):
    (Path(agent[1]) / "codex-settings.json").write_text(json.dumps(settings))


def received(agent):
    path = Path(agent[1]) / "codex-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def ignore(update: dict[str, Any]) -> None:
    pass


async def reject(request: dict[str, Any]) -> None:
    return None


async def wait_file(path):
    async with asyncio.timeout(3):
        for _ in range(300):
            if path.exists():
                return
            await asyncio.sleep(0.01)
    raise AssertionError("Native fixture did not write its observation")


async def test_native_turn_and_final_item_are_correlated_with_explicit_config(agent):
    updates = []

    async def update(event):
        updates.append(event)

    home = str(Path(agent[1]) / "selected-codex-home")
    async with transport()(*agent, update, reject, home=home) as client:
        session = await client.open_session()
        result = await client.prompt(session, "authorized work", prompt_id="task-1")
        assert isinstance(result, NativeResult)
        assert (result.session_id, result.turn_id, result.status, result.text) == (
            "owned-thread",
            "turn-1",
            "completed",
            "native final answer",
        )
    requests = received(agent)
    opened = next(row for row in requests if row.get("method") == "thread/start")["params"]
    assert opened["cwd"] == agent[1]
    assert opened["approvalPolicy"] == "on-request"
    assert opened["approvalsReviewer"] == "user"
    assert opened["sandbox"] == "workspace-write"
    assert opened["config"]["mcp_servers"] == {}
    assert opened["config"]["features.apps"] is False
    assert opened["config"]["features.plugins"] is False
    prompt = next(row for row in requests if row.get("method") == "turn/start")["params"]
    assert prompt["clientUserMessageId"] == "task-1"
    assert "authorized work" in prompt["input"][0]["text"]
    assert prompt["sandboxPolicy"]["networkAccess"] is False
    assert (Path(agent[1]) / "codex-home.txt").read_text() == home
    argv = json.loads((Path(agent[1]) / "codex-argv.json").read_text())
    assert argv[:2] == ["app-server", "--stdio"]
    assert not any("bypass" in flag or flag == "--trust" for flag in argv)
    assert any(event["sessionUpdate"] == "turn/completed" for event in updates)
    pid = int((Path(agent[1]) / "codex-pid.txt").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("phase", [None, "commentary", "partial_answer"])
async def test_missing_final_phase_is_not_promoted_to_successful_reply(agent, phase):
    configure(agent, phase=phase)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        result = await client.prompt(session, "work", prompt_id="task")
        assert result.status == "completed"
        assert result.text is None


@pytest.mark.parametrize("status,expected", [("failed", "failed"), ("interrupted", "cancelled")])
async def test_native_failure_and_interruption_remain_distinct(agent, status, expected):
    configure(agent, status=status, error={"message": "native failure"})
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        result = await client.prompt(session, "work", prompt_id="task")
        assert result.status == expected
        assert result.evidence["error"]["message"] == "native failure"


async def test_eof_after_submission_never_becomes_terminal_success(agent):
    configure(agent, eof=True)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="EOF|closed|uncertain"):
            await client.prompt(session, "work", prompt_id="task")


def notification(method, **params):
    return {"method": method, "params": {"threadId": "$thread", **params}}


def terminal(status="completed", items=None, turn_id="$turn"):
    return notification(
        "turn/completed", turn={"id": turn_id, "status": status, "items": items or []}
    )


def approval(request_id="permission-1", *, method="item/commandExecution/requestApproval", **extra):
    return {
        "id": request_id,
        "method": method,
        "params": {
            "threadId": "$thread",
            "turnId": "$turn",
            "itemId": "command-1",
            "startedAtMs": 123,
            "command": "echo authorized",
            "cwd": "/project",
            **extra,
        },
    }


async def test_mcp_definitions_are_translated_and_reapplied_on_resume(agent):
    definitions = [
        {
            "name": "scoped",
            "command": "/bin/true",
            "args": ["--flag"],
            "env": [{"name": "TASK_SCOPE", "value": "only-this"}],
        }
    ]
    async with transport()(*agent, ignore, reject) as client:
        assert await client.open_session("saved-owned-thread", definitions) == "saved-owned-thread"
    requests = received(agent)
    resume = next(row for row in requests if row.get("method") == "thread/resume")["params"]
    assert resume["excludeTurns"] is True
    native = resume["config"]["mcp_servers"]["scoped"]
    assert native["command"] == "/bin/true"
    assert native["args"] == ["--flag"]
    assert native["env"] == {"TASK_SCOPE": "only-this"}
    assert native["required"] is True
    assert any(row.get("method") == "mcpServerStatus/list" for row in requests)


@pytest.mark.parametrize(
    "settings",
    [
        {"reviewer": "auto_review"},
        {"approval": "never"},
        {"sandbox": {"type": "dangerFullAccess"}},
        {"open_cwd": "/different"},
        {"open_status": "active"},
        {"sandbox": {"type": "workspaceWrite", "writableRoots": ["/"], "networkAccess": False}},
    ],
)
async def test_changed_policy_or_scope_is_rejected_before_prompt(agent, settings):
    configure(agent, **settings)
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="policy|scope|MCP|idle|sandbox|directory"):
            await client.open_session()
    assert not any(row.get("method") == "turn/start" for row in received(agent))


@pytest.mark.parametrize(
    "definitions",
    [
        [{"name": "x", "command": "/bin/true", "args": [7], "env": []}],
        [{"name": "x", "type": "sse", "url": "https://example.invalid", "headers": []}],
        [{"name": "x", "command": "/bin/true", "args": [], "env": []}] * 2,
    ],
)
async def test_invalid_or_unsupported_mcp_definitions_are_not_silently_relaxed(agent, definitions):
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="MCP"):
            await client.open_session(mcp_servers=definitions)


async def test_early_final_events_wait_for_matching_turn_start_response(agent):
    item = {"type": "agentMessage", "id": "answer", "text": "early final", "phase": "final_answer"}
    configure(
        agent,
        before_response=[notification("item/completed", turnId="$turn", item=item), terminal()],
        hold=True,
    )
    async with transport()(*agent, ignore, reject) as client:
        result = await client.prompt(await client.open_session(), "work", prompt_id="task")
        assert result.text == "early final"
        assert result.turn_id == "turn-1"


async def test_foreign_turns_and_commentary_cannot_supply_reply(agent):
    wrong = {
        "type": "agentMessage",
        "id": "foreign",
        "text": "wrong answer",
        "phase": "final_answer",
    }
    configure(
        agent,
        before_response=[
            notification("item/completed", turnId="foreign-turn", item=wrong),
            terminal(turn_id="foreign-turn"),
        ],
    )
    async with transport()(*agent, ignore, reject) as client:
        result = await client.prompt(await client.open_session(), "work", prompt_id="task")
        assert result.text == "native final answer"


async def test_duplicate_terminal_event_does_not_pollute_next_prompt(agent):
    configure(agent, duplicate_terminal=True)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        first = await client.prompt(session, "first", prompt_id="first")
        second = await client.prompt(session, "second", prompt_id="second")
        assert first.turn_id != second.turn_id
        assert second.text == "native final answer"


async def test_reused_native_turn_id_is_refused(agent):
    configure(agent, turn_id="same-turn")
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        await client.prompt(session, "first", prompt_id="first")
        with pytest.raises(BridgeError, match="reused"):
            await client.prompt(session, "second", prompt_id="second")


async def test_active_thread_is_not_steered(agent):
    configure(agent, read_status="active")
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="idle"):
            await client.prompt(session, "work", prompt_id="task")
    assert not any(row.get("method") == "turn/start" for row in received(agent))


@pytest.mark.parametrize("request_id", [5, "5"])
@pytest.mark.parametrize("choice", ["accept", "decline", None])
async def test_one_time_approval_preserves_native_request_id(agent, request_id, choice):
    configure(agent, permissions=[approval(request_id)])
    seen = []

    async def decide(request):
        seen.append(request)
        return choice

    async with transport()(*agent, ignore, decide) as client:
        result = await client.prompt(await client.open_session(), "work", prompt_id="task")
    assert result.status == ("cancelled" if choice is None else "completed")
    assert len(seen) == 1
    assert {opt["kind"] for opt in seen[0]["options"]} <= {"allow_once", "reject_once"}
    reply = json.loads((Path(agent[1]) / "codex-permission-reply.json").read_text())
    assert type(reply["id"]) is type(request_id)
    assert reply == {"id": request_id, "result": {"decision": choice or "cancel"}}


async def test_multiple_permissions_do_not_block_protocol_reading(agent):
    configure(agent, permissions=[approval("one"), approval("two")])
    both = asyncio.Event()
    decisions = asyncio.Event()
    count = 0

    async def decide(request):
        nonlocal count
        count += 1
        if count == 2:
            both.set()
        await decisions.wait()
        return "decline"

    async with transport()(*agent, ignore, decide) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "work", prompt_id="task"))
        try:
            await asyncio.wait_for(both.wait(), 2)
            decisions.set()
            assert (await task).status == "completed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cleared_permission_cancels_human_wait_and_rejects_stale_answer(agent):
    configure(
        agent,
        permissions=[approval()],
        after_permissions=[notification("serverRequest/resolved", requestId="permission-1")],
        finish_while_permission=True,
    )
    cancelled = asyncio.Event()

    async def decide(request):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with transport()(*agent, ignore, decide) as client:
        result = await client.prompt(await client.open_session(), "work", prompt_id="task")
        assert result.status == "completed"
    assert not (Path(agent[1]) / "codex-permission-reply.json").exists()
    # A request cleared before its callback starts also needs no human wait.
    assert cancelled.is_set() or not any(
        row.get("result", {}).get("decision") for row in received(agent)
    )


@pytest.mark.parametrize(
    "native_request",
    [
        approval(
            method="item/permissions/requestApproval", permissions={"network": {"enabled": True}}
        ),
        approval(method="mcpServer/elicitation/request", mode="form", requestedSchema={}),
        approval(method="item/tool/requestUserInput", questions=[]),
        approval(networkApprovalContext={"host": "example.invalid", "protocol": "https"}),
        approval(kind="humanCommand"),
        approval(method="item/fileChange/requestApproval", grantRoot="/"),
    ],
)
async def test_unsupported_or_broad_permission_requests_fail_explicitly(agent, native_request):
    configure(agent, permissions=[native_request], hold=True)
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="Unsupported|unsupported|broad"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")
    assert any(row.get("error", {}).get("code") == -32601 for row in received(agent))


async def test_persistent_approval_choice_is_rejected(agent):
    configure(agent, permissions=[approval()])

    async def decide(request):
        return "acceptForSession"

    async with transport()(*agent, ignore, decide) as client:
        with pytest.raises(BridgeError, match="permission|choice"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")
    assert not any(
        row.get("result", {}).get("decision") == "acceptForSession" for row in received(agent)
    )


async def test_cancel_acknowledgement_alone_does_not_complete_prompt(agent):
    configure(agent, hold=True, cancel_ack_only=True)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "work", prompt_id="task"))
        try:
            await wait_file(Path(agent[1]) / "codex-prompt-seen")
            await client.cancel(session)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "native_status,expected", [("interrupted", "cancelled"), ("completed", "completed")]
)
async def test_cancel_preserves_correlated_native_outcome_including_completion_race(
    agent, native_status, expected
):
    configure(agent, hold=True, cancel_status=native_status)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "work", prompt_id="task"))
        try:
            await wait_file(Path(agent[1]) / "codex-prompt-seen")
            await client.cancel(session)
            assert (await task).status == expected
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "malformed",
    [
        "not-json",
        "[]",
        '{"id":true,"result":{}}',
        '{"method":"notice","params":{},"method":"other"}',
    ],
)
async def test_invalid_frames_fail_without_waiting_for_inference(agent, malformed):
    configure(agent, malformed=malformed)
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="protocol|malformed|invalid|duplicate"):
            async with asyncio.timeout(2):
                await client.prompt(await client.open_session(), "work", prompt_id="task")


async def test_oversized_frame_is_bounded(agent, monkeypatch):
    module = importlib.import_module("answerback_bridge.native_codex")
    monkeypatch.setattr(module, "MAX_FRAME_BYTES", 2048)
    configure(agent, oversized=4096)
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="bound|limit|chunk"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")


async def test_permission_replay_is_not_approved_twice(agent):
    configure(agent, permissions=[approval()], repeat_permission=True, hold=True)
    count = 0

    async def decide(request):
        nonlocal count
        count += 1
        return "accept"

    async with transport()(*agent, ignore, decide) as client:
        with pytest.raises(BridgeError, match="duplicate|reused"):
            async with asyncio.timeout(2):
                await client.prompt(await client.open_session(), "work", prompt_id="task")
    assert count == 1


async def test_cancelled_prompt_poisons_transport_against_silent_replay(agent):
    configure(agent, hold=True)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "first", prompt_id="first"))
        await wait_file(Path(agent[1]) / "codex-prompt-seen")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(BridgeError, match="uncertain|unsettled"):
            async with asyncio.timeout(2):
                await client.prompt(session, "second", prompt_id="second")
    assert sum(row.get("method") == "turn/start" for row in received(agent)) == 1


@pytest.mark.skipif(os.name != "posix", reason="Owned process group cleanup is POSIX-specific")
async def test_cleanup_reaps_owned_descendant_after_server_exits(
    agent: tuple[str, str], reap_test_descendants: bool
) -> None:
    configure(agent, descendant=True)
    pid = None
    reaped = False
    try:
        async with transport()(*agent, ignore, reject) as client:
            await client.open_session()
            pid = int((Path(agent[1]) / "codex-descendant.txt").read_text())
        try:
            async with asyncio.timeout(3):
                while True:
                    if reap_test_descendants and not reaped:
                        # A killed orphan remains visible to kill(pid, 0) as a zombie
                        # if a container's PID 1 does not reap it. We own adoption in
                        # this test; require a real SIGKILL exit and reap it ourselves.
                        waited, status = os.waitpid(pid, os.WNOHANG)  # noqa: ASYNC222 — nonblocking
                        if waited:
                            reaped = True
                            assert waited == pid
                            assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pytest.fail("Owned native child outlived transport cleanup")
        if reap_test_descendants:
            assert reaped, "The adopted descendant must be reaped after its SIGKILL exit"
    finally:
        # Fixture ownership is explicit; never signal a discovered live process.
        if pid is not None and not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if reap_test_descendants:
                await asyncio.to_thread(os.waitpid, pid, 0)


async def test_file_approval_includes_exact_proposed_diff(agent):
    item = {
        "type": "fileChange",
        "id": "command-1",
        "status": "inProgress",
        "changes": [
            {
                "path": "/project/file.py",
                "kind": {"type": "add"},
                "diff": "+print('visible change')",
            }
        ],
    }
    configure(
        agent,
        after_response=[notification("item/started", turnId="$turn", item=item)],
        permissions=[approval(method="item/fileChange/requestApproval")],
    )
    seen = []

    async def decide(request):
        seen.append(request)
        return "decline"

    async with transport()(*agent, ignore, decide) as client:
        assert (
            await client.prompt(await client.open_session(), "work", prompt_id="task")
        ).status == "completed"
    assert seen[0]["tool_call"]["rawInput"]["changes"] == item["changes"]


async def test_permission_callback_failure_is_explicit(agent):
    configure(agent, permissions=[approval()])

    async def broken(request):
        raise RuntimeError("callback failed")

    async with transport()(*agent, ignore, broken) as client:
        with pytest.raises(BridgeError, match="callback"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")


async def test_foreign_permission_never_reaches_human_callback(agent):
    configure(agent, permissions=[approval(turnId="foreign-turn")], hold=True)
    seen = []

    async def decide(request):
        seen.append(request)
        return "accept"

    async with transport()(*agent, ignore, decide) as client:
        with pytest.raises(BridgeError, match="stale"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")
    assert seen == []


async def test_excessive_early_events_are_bounded(agent):
    configure(
        agent,
        before_response=[
            notification("item/agentMessage/delta", turnId="$turn", itemId="x", delta="x")
        ]
        * 65,
    )
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="buffer"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")


async def test_excessive_final_answer_is_bounded(agent, monkeypatch):
    module = importlib.import_module("answerback_bridge.native_codex")
    monkeypatch.setattr(module, "MAX_TEXT_CHARS", 20)
    configure(agent, answer="x" * 21)
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="answer exceeded"):
            await client.prompt(await client.open_session(), "work", prompt_id="task")


async def test_concurrent_prompt_is_refused_before_native_dispatch(agent):
    configure(agent, hold=True)
    async with transport()(*agent, ignore, reject) as client:
        session = await client.open_session()
        first = asyncio.create_task(client.prompt(session, "first", prompt_id="first"))
        try:
            await wait_file(Path(agent[1]) / "codex-prompt-seen")
            with pytest.raises(BridgeError, match="active"):
                await client.prompt(session, "second", prompt_id="second")
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert sum(row.get("method") == "turn/start" for row in received(agent)) == 1


async def test_clean_eof_after_correlated_terminal_keeps_native_result(agent):
    configure(agent, exit_after_terminal=True)
    async with transport()(*agent, ignore, reject) as client:
        result = await client.prompt(await client.open_session(), "work", prompt_id="task")
        assert result.status == "completed"
        assert result.text == "native final answer"


async def test_cancelled_callback_cannot_restore_approval_by_suppressing_cancellation(agent):
    configure(agent, hold=True, cancel_ack_only=True, permissions=[approval()])
    waiting = asyncio.Event()

    async def decide(request):
        waiting.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return "accept"

    async with transport()(*agent, ignore, decide) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "work", prompt_id="task"))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            await client.cancel(session)
            assert not task.done()
            assert not (Path(agent[1]) / "codex-permission-reply.json").exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cleanup_failure_leaves_native_stop_witness_unknown(agent, monkeypatch) -> None:
    module = importlib.import_module("answerback_bridge.native_codex")
    real_stop = module.stop_child

    async def failed_stop(process, *, cleanup_seconds):
        raise CommandCleanupError("test cleanup failure")

    client = await transport()(*agent, ignore, reject).__aenter__()
    try:
        await client.open_session()
        assert client._process is not None
        process = client._process
        monkeypatch.setattr(module, "stop_child", failed_stop)
        with pytest.raises(CommandCleanupError, match="test cleanup failure"):
            await client.close()
        assert client.native_stopped is None
    finally:
        monkeypatch.setattr(module, "stop_child", real_stop)
        await real_stop(process, cleanup_seconds=module.SHUTDOWN_TIMEOUT)


async def test_empty_scoped_mcp_does_not_trigger_global_discovery(agent):
    configure(agent, global_inventory_trap=True, extra_mcp=["unrelated-global-server"])
    async with transport()(*agent, ignore, reject) as client:
        assert await client.open_session(mcp_servers=[]) == "owned-thread"
    requests = received(agent)
    assert not any(row.get("method") == "mcpServerStatus/list" for row in requests)
    opened = next(row for row in requests if row.get("method") == "thread/start")
    assert opened["params"]["config"]["mcp_servers"] == {}


async def test_readiness_queries_only_explicit_thread_server_names(agent):
    configure(agent, global_inventory_trap=True)
    servers = [
        {"name": name, "command": "/bin/true", "args": [], "env": []}
        for name in ("first", "second")
    ]
    async with transport()(*agent, ignore, reject) as client:
        assert await client.open_session(mcp_servers=servers) == "owned-thread"
    inspected = [
        row["params"] for row in received(agent) if row.get("method") == "mcpServerStatus/list"
    ]
    assert {row["serverName"] for row in inspected} == {"first", "second"}
    assert all(row["threadId"] == "owned-thread" for row in inspected)


async def test_native_originator_does_not_inherit_desktop_session_metadata(agent, monkeypatch):
    for key in ("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.setenv(key, "parent-desktop-value")
    async with transport()(*agent, ignore, reject) as client:
        await client.open_session()
    observed = json.loads((Path(agent[1]) / "codex-origin-env.json").read_text())
    assert set(observed.values()) == {None}
    assert received(agent)[0]["params"]["clientInfo"]["name"] == "answerback-native-cli"


@pytest.mark.parametrize(
    "settings",
    [
        {"mcp_status": "connecting"},
        {"extra_mcp": ["wrong-selected-server"]},
    ],
)
async def test_selected_server_readiness_does_not_admit_unready_or_wrong_server(agent, settings):
    configure(agent, **settings)
    servers = [{"name": "selected", "command": "/bin/true", "args": [], "env": []}]
    async with transport()(*agent, ignore, reject) as client:
        with pytest.raises(BridgeError, match="MCP|ready|scope"):
            await client.open_session(mcp_servers=servers)
