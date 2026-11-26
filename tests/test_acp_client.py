import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from acp.connection import StreamDirection, StreamEvent

from codex_herdr_bridge import acp_client
from codex_herdr_bridge.acp_client import KiroACP, PromptResult
from codex_herdr_bridge.process import BridgeError, CommandCleanupError


@pytest.fixture
def agent(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "acp_agent.py"
    binary = tmp_path / "fake-kiro"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    (tmp_path / "agent-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


def configure(agent: tuple[str, str], **settings: Any) -> None:
    (Path(agent[1]) / "agent-settings.json").write_text(json.dumps(settings))


def received(agent: tuple[str, str]) -> list[dict[str, Any]]:
    path = Path(agent[1]) / "agent-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def ignore_update(update: dict[str, Any]) -> None:
    pass


async def cancel_permission(request: dict[str, Any]) -> None:
    return None


async def wait_file(path: Path) -> None:
    async with asyncio.timeout(3):
        for _ in range(300):
            if await asyncio.to_thread(path.exists):
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"The ACP peer did not write {path.name}")


async def wait_jsonl(path: Path, count: int) -> list[dict[str, Any]]:
    async with asyncio.timeout(3):
        while True:
            if await asyncio.to_thread(path.exists):
                lines = await asyncio.to_thread(path.read_text)
                records = [json.loads(line) for line in lines.splitlines()]
                if len(records) >= count:
                    return records
            await asyncio.sleep(0.01)


async def test_protocol_launch_and_streamed_prompt_are_correlated(agent) -> None:
    updates = []

    async def on_update(update):
        updates.append(update)

    async with KiroACP(*agent, on_update, cancel_permission) as client:
        session_id = await client.open_session()
        result = await client.prompt(session_id, "hello")
        assert result == PromptResult("Hello world", "end_turn", "new-session")
        assert client.capabilities["loadSession"] is True
    requests = received(agent)
    assert requests[0]["params"]["protocolVersion"] == 1
    capabilities = requests[0]["params"]["clientCapabilities"]
    assert not capabilities.get("terminal")
    assert not any(capabilities.get("fs", {}).values())
    assert requests[1]["params"] == {"cwd": agent[1], "mcpServers": []}
    assert requests[2]["params"]["prompt"] == [{"type": "text", "text": "hello"}]
    assert json.loads((Path(agent[1]) / "agent-argv.json").read_text()) == [
        "acp",
        "--agent-engine",
        "v3",
        "--auth-method",
        "cli",
    ]
    assert all(update["session_id"] == session_id for update in updates)
    assert any(update["sessionUpdate"] == "agent_thought_chunk" for update in updates)


async def test_load_replay_does_not_pollute_prompt_and_mcp_definitions_are_resent(agent) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        definitions = [{"name": "local", "command": "/bin/true", "args": [], "env": []}]
        assert await client.open_session("saved", definitions) == "saved"
        assert (await client.prompt("saved", "hello")).text == "Hello world"
        assert (await client.prompt("saved", "hello-again")).text == "Hello world"
        page = await client.list_sessions(cwd=agent[1], cursor="page-1")
        assert page["sessions"][0]["sessionId"] == "saved"
        assert page["nextCursor"] == "page-2"
    requests = received(agent)
    assert requests[1]["params"] == {
        "sessionId": "saved",
        "cwd": agent[1],
        "mcpServers": definitions,
    }
    assert requests[-1]["params"] == {"cwd": agent[1], "cursor": "page-1"}


async def test_optional_session_methods_require_advertised_capabilities(agent) -> None:
    configure(agent, capabilities={"loadSession": False, "sessionCapabilities": {"list": None}})
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="load"):
            await client.open_session("saved")
        with pytest.raises(BridgeError, match="list"):
            await client.list_sessions()
    assert [message["method"] for message in received(agent)] == ["initialize"]


def mcp_server(status: str = "connected", **extra: Any) -> dict[str, Any]:
    return {
        "name": "herdr_bridge",
        "status": status,
        "tools": [{"name": name, "disabled": False} for name in ("peer_submit", "task_wait")],
        **extra,
    }


REQUIRED_MCP_TOOLS = {"herdr_bridge": {"peer_submit", "task_wait"}}
MCP_DEFINITIONS = [
    {
        "name": "herdr_bridge",
        "command": "/bin/true",
        "args": [],
        "env": [],
        "_meta": {"kiro": {"waitForReady": True, "timeout": 60000}},
    }
]

SCOPED_PROFILE = {
    "id": "scoped-worker",
    "prompt": "Perform only the assigned task.",
    "tools": ["read", "@herdr_bridge"],
    "includeMcpJson": False,
}


@pytest.mark.parametrize("session_id", [None, "saved"])
async def test_profile_metadata_reaches_new_and_load_requests(agent, session_id) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        await client.open_session(session_id, agent_profile=SCOPED_PROFILE)
    request = received(agent)[1]
    assert request["params"]["_meta"] == {
        "kiro": {"modeId": "scoped-worker", "customAgents": [SCOPED_PROFILE]}
    }


@pytest.mark.parametrize("session_id", [None, "saved"])
@pytest.mark.parametrize("reported_mode", ["vibe", None])
async def test_required_profile_must_be_confirmed_before_prompt(
    agent, session_id, reported_mode
) -> None:
    configure(agent, reported_mode=reported_mode)
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="required agent profile"):
            await client.open_session(session_id, agent_profile=SCOPED_PROFILE)
    assert not any(request["method"] == "session/prompt" for request in received(agent))


@pytest.mark.parametrize("session_id", [None, "saved"])
async def test_required_mcp_waits_for_ready_tools_after_session_response(agent, session_id) -> None:
    configure(
        agent,
        mcp_open_plans=[
            {
                "before": [{"servers": [mcp_server("connecting")]}],
                "after": [
                    {"delay": 0.08, "servers": [mcp_server()], "label": "ready"},
                ],
            }
        ],
    )
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        result = await client.open_session(
            session_id, MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
        )
        assert result == (session_id or "new-session")
        assert (Path(agent[1]) / "mcp-event-1-ready").exists()
    requests = received(agent)
    assert requests[-1]["params"]["mcpServers"] == MCP_DEFINITIONS
    assert not any(row.get("method") == "session/prompt" for row in requests)


@pytest.mark.parametrize("session_id", [None, "saved"])
async def test_required_mcp_accepts_ready_notification_before_session_response(
    agent, session_id
) -> None:
    configure(agent, mcp_open_plans=[{"before": [{"servers": [mcp_server()]}]}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        assert await client.open_session(
            session_id, MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
        ) == (session_id or "new-session")


@pytest.mark.parametrize("status", ["failed", "disabled"])
async def test_required_mcp_rejects_startup_failure_without_leaking_provider_details(
    agent, status, caplog
) -> None:
    sensitive = "secret-token-never-retain"
    server = mcp_server(
        status,
        errorMessage=sensitive,
        authorizationUrl=f"https://example.invalid/auth?token={sensitive}",
        headers={"Authorization": sensitive},
    )
    configure(agent, mcp_open_plans=[{"before": [{"servers": [server]}]}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="MCP.*herdr_bridge") as error:
            await client.open_session(
                "saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
            )
    assert sensitive not in str(error.value)
    assert sensitive not in caplog.text


@pytest.mark.parametrize(
    "tools",
    [
        [{"name": "peer_submit", "disabled": False}],
        [
            {"name": "peer_submit", "disabled": False},
            {"name": "task_wait", "disabled": True},
        ],
        [{"name": name} for name in ("peer_submit", "task_wait")],
        None,
    ],
)
async def test_required_mcp_rejects_connected_server_without_verified_enabled_tools(
    agent, tools
) -> None:
    configure(agent, mcp_open_plans=[{"before": [{"servers": [mcp_server(tools=tools)]}]}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="required tools"):
            await client.open_session(
                "saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
            )


@pytest.mark.parametrize("session_id", [None, "saved"])
async def test_required_mcp_ignores_other_session_status_and_times_out(
    agent, session_id, monkeypatch
) -> None:
    monkeypatch.setattr(acp_client, "MCP_READINESS_TIMEOUT", 0.08)
    configure(
        agent,
        mcp_open_plans=[{"before": [{"session_id": "wrong-session", "servers": [mcp_server()]}]}],
    )
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="MCP readiness.*timed out"):
            await client.open_session(
                session_id, MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
            )


async def test_required_mcp_does_not_reuse_status_from_previous_open(agent, monkeypatch) -> None:
    monkeypatch.setattr(acp_client, "MCP_READINESS_TIMEOUT", 0.1)
    configure(agent, mcp_open_plans=[{"before": [{"servers": [mcp_server()]}]}, {}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        await client.open_session("saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS)
        with pytest.raises(BridgeError, match="MCP readiness.*timed out"):
            await client.open_session(
                "saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
            )


async def test_required_mcp_uses_whole_snapshot_instead_of_merging_old_servers(agent) -> None:
    other = mcp_server(name="other_bridge")
    configure(
        agent,
        mcp_open_plans=[
            {
                "before": [
                    {"servers": [mcp_server(), {**other, "status": "connecting"}]},
                    {"servers": [other]},
                ],
                "after": [{"delay": 0.08, "servers": [mcp_server(), other], "label": "ready"}],
            }
        ],
    )
    required = {**REQUIRED_MCP_TOOLS, "other_bridge": {"task_wait"}}
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        await client.open_session("saved", MCP_DEFINITIONS, required_mcp_tools=required)
        assert (Path(agent[1]) / "mcp-event-1-ready").exists()


async def test_required_mcp_session_timeout_allows_native_readiness_wait(
    agent, monkeypatch
) -> None:
    monkeypatch.setattr(acp_client, "MCP_SESSION_TIMEOUT", 0.4)
    configure(
        agent,
        mcp_open_plans=[{"before": [{"servers": [mcp_server()]}], "response_delay": 0.15}],
    )
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        # Only session operations are under test; process startup has its ordinary deadline.
        monkeypatch.setattr(acp_client, "REQUEST_TIMEOUT", 0.1)
        await client.open_session("saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS)


async def test_no_readiness_requirement_preserves_short_session_timeout(agent, monkeypatch) -> None:
    configure(agent, mcp_open_plans=[{"response_delay": 0.15}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        # Only session operations are under test; process startup has its ordinary deadline.
        monkeypatch.setattr(acp_client, "REQUEST_TIMEOUT", 0.1)
        with pytest.raises(BridgeError, match="session/load.*timed out"):
            await client.open_session("saved", MCP_DEFINITIONS)


async def test_unknown_readiness_protocol_times_out_explicitly(agent, monkeypatch) -> None:
    monkeypatch.setattr(acp_client, "MCP_READINESS_TIMEOUT", 0.05)
    configure(agent, mcp_open_plans=[{"before": [{"servers": [mcp_server("new-state")]}]}])
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="MCP readiness.*timed out"):
            await client.open_session(
                "saved", MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS
            )


async def test_required_mcp_bounds_unbound_new_session_candidates(agent) -> None:
    configure(
        agent,
        mcp_open_plans=[
            {
                "before": [
                    {"session_id": f"other-{index}", "servers": [mcp_server()]}
                    for index in range(9)
                ],
            }
        ],
    )
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="MCP.*session.*bound"):
            await client.open_session(None, MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS)


@pytest.mark.parametrize("choice", ["yes", "no", None])
async def test_reverse_permission_uses_only_supplied_one_time_choices(agent, choice) -> None:
    observed = []

    async def choose(request):
        observed.append(request)
        return choice

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        result = await client.prompt(session, "permission")
        assert result.stop_reason == "end_turn"
    response = json.loads((Path(agent[1]) / "permission-response.json").read_text())
    expected = (
        {"outcome": "cancelled"} if choice is None else {"outcome": "selected", "optionId": choice}
    )
    assert response["result"] == {"outcome": expected}
    assert observed[0]["session_id"] == session
    assert observed[0]["tool_call"]["toolCallId"] == "tool-1"
    assert {option["optionId"] for option in observed[0]["options"]} == {"yes", "no"}


async def test_repeated_tool_call_id_preserves_permission_rounds_and_cancels_only_second(
    agent,
) -> None:
    observed: list[dict[str, Any]] = []
    second_requested = asyncio.Event()

    async def choose(request: dict[str, Any]) -> str:
        observed.append(request)
        if len(observed) == 1:
            return "yes"
        second_requested.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        prompt = asyncio.create_task(client.prompt(session, "permission-rounds-same"))
        await asyncio.wait_for(second_requested.wait(), 3)
        await client.cancel(session)
        result = await asyncio.wait_for(prompt, 3)
        responses = await wait_jsonl(Path(agent[1]) / "permission-round-responses.jsonl", 2)

    assert result.stop_reason == "cancelled"
    assert [request["tool_call"]["toolCallId"] for request in observed] == [
        "tool-shared",
        "tool-shared",
    ]
    assert [request["tool_call"]["rawInput"] for request in observed] == [
        {"path": "round-1.py"},
        {"path": "round-2.py"},
    ]
    assert [request["tool_call"]["locations"] for request in observed] == [
        [{"path": "round-1.py", "line": 1}],
        [{"path": "round-2.py", "line": 2}],
    ]
    assert [response["id"] for response in responses] == [
        "permission-same-1",
        "permission-same-2",
    ]
    assert [response["result"]["outcome"] for response in responses] == [
        {"outcome": "selected", "optionId": "yes"},
        {"outcome": "cancelled"},
    ]


async def test_interleaved_tool_calls_preserve_permission_request_order(agent) -> None:
    observed: list[dict[str, Any]] = []
    answers = {"A1": "yes", "B1": "no", "A2": "yes"}

    async def choose(request: dict[str, Any]) -> str:
        observed.append(request)
        return answers[request["tool_call"]["rawInput"]["round"]]

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        result = await client.prompt(session, "permission-rounds-interleaved")
        responses = await wait_jsonl(Path(agent[1]) / "permission-round-responses.jsonl", 3)

    assert result.stop_reason == "end_turn"
    assert [request["tool_call"]["toolCallId"] for request in observed] == [
        "tool-a",
        "tool-b",
        "tool-a",
    ]
    assert [request["tool_call"]["rawInput"]["round"] for request in observed] == [
        "A1",
        "B1",
        "A2",
    ]
    assert [response["id"] for response in responses] == [
        "permission-a-1",
        "permission-b-1",
        "permission-a-2",
    ]
    assert [response["result"]["outcome"] for response in responses] == [
        {"outcome": "selected", "optionId": "yes"},
        {"outcome": "selected", "optionId": "no"},
        {"outcome": "selected", "optionId": "yes"},
    ]


async def test_request_permission_forwards_locations_and_raw_input_unchanged(agent) -> None:
    """Finding LT-12: ACPClient.request_permission's self.serialize(tool_call)
    (acp_client.py:569-599, 294; inherited by Kiro, Grok and a plugin's client alike)
    already forwards a populated locations/rawInput unchanged -- this pins that
    forwarding directly against the real ACP wire reply, in both directions
    (present and absent), so a future change to serialize cannot silently
    start stripping path data again. It builds only a KiroACP against the
    fixture subprocess; it does not construct an ACPWorkerRuntime/Workers and
    does not exercise permission()'s own 32,000-character bound in
    workers.py. Whether Kiro's own agent ever populates these fields for a
    'Read File' tool call is an open question only a live Kiro session can
    settle (probe P0-7, status/handoff/T-48.md)."""
    observed = []

    async def choose(request):
        observed.append(request)
        return "yes"

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        await client.prompt(session, "permission")
    absent_tool_call = observed[0]["tool_call"]
    assert "locations" not in absent_tool_call
    assert "rawInput" not in absent_tool_call

    locations = [{"path": "/workspace/example.py", "line": 12}]
    raw_input = {"path": "/workspace/example.py"}
    configure(agent, permission_locations=locations, permission_raw_input=raw_input)
    observed.clear()
    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        await client.prompt(session, "permission")
    populated_tool_call = observed[0]["tool_call"]
    assert populated_tool_call["locations"] == locations
    assert populated_tool_call["rawInput"] == raw_input


@pytest.mark.parametrize("choice", ["invented", "forever"])
async def test_invalid_or_persistent_permission_choice_fails_closed(agent, choice) -> None:
    async def choose(request):
        return choice

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="permission"):
            await client.prompt(session, "permission")


async def test_permission_callback_may_return_any_awaitable(agent) -> None:
    answer = asyncio.get_running_loop().create_future()
    answer.set_result("no")

    def choose(request):
        return answer

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        await client.prompt(session, "permission")
    response = json.loads((Path(agent[1]) / "permission-response.json").read_text())
    assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}


async def test_permission_callback_exception_fails_prompt(agent) -> None:
    def choose(request):
        raise ValueError("approval broker unavailable")

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="permission callback"):
            await client.prompt(session, "permission")


async def test_cancel_resolves_pending_permission_and_sends_session_cancel(agent) -> None:
    requested = asyncio.Event()
    released = asyncio.Event()

    async def choose(request):
        requested.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        task = asyncio.create_task(client.prompt(session, "permission"))
        await asyncio.wait_for(requested.wait(), 3)
        await client.cancel(session)
        result = await asyncio.wait_for(task, 3)
        assert result.stop_reason == "cancelled"
        await asyncio.wait_for(released.wait(), 3)
        await wait_file(Path(agent[1]) / "permission-response.json")
    response = json.loads((Path(agent[1]) / "permission-response.json").read_text())
    assert response["result"]["outcome"] == {"outcome": "cancelled"}
    assert any(request.get("method") == "session/cancel" for request in received(agent))


async def test_prompt_requires_open_session_and_disallows_overlapping_turns(agent) -> None:
    started = asyncio.Event()

    async def on_update(update):
        started.set()

    async with KiroACP(*agent, on_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="session"):
            await client.prompt("unopened", "hello")
        session = await client.open_session()
        pending = asyncio.create_task(client.prompt(session, "wait"))
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(BridgeError, match="prompt|turn"):
            await client.prompt(session, "hello")
        await client.cancel(session)
        await asyncio.wait_for(pending, 3)


@pytest.mark.parametrize("message", ["mismatch", "too-much-text", "bad-response", "exit", "error"])
async def test_invalid_or_failed_prompt_is_reported_as_bridge_error(agent, message) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError):
            await asyncio.wait_for(client.prompt(session, message), 3)


async def test_permission_from_wrong_session_never_reaches_approver(agent) -> None:
    configure(agent, permission_session="wrong-session")
    called = False

    async def choose(request):
        nonlocal called
        called = True
        return "yes"

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="session"):
            await asyncio.wait_for(client.prompt(session, "permission"), 3)
    assert not called


async def test_oversized_unterminated_frame_fails_without_waiting_for_newline(agent) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="frame|bound"):
            await asyncio.wait_for(client.prompt(session, "oversize"), 3)


async def test_sdk_rejects_malformed_frame_without_treating_it_as_completion(agent) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        assert (await asyncio.wait_for(client.prompt(session, "malformed"), 3)).text == ""
    response = json.loads((Path(agent[1]) / "malformed-response.json").read_text())
    assert response["error"]["code"] == -32700


@pytest.mark.parametrize(
    "method",
    [
        "_kiro/auth/getAccessToken",
        "fs/read_text_file",
        "fs/write_text_file",
        "terminal/create",
        "terminal/output",
        "terminal/release",
        "terminal/wait_for_exit",
        "terminal/kill",
    ],
)
async def test_unimplemented_client_requests_get_method_not_found(agent, method) -> None:
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        await asyncio.wait_for(client.prompt(session, f"unsupported:{method}"), 3)
    response = json.loads((Path(agent[1]) / "unsupported-response.json").read_text())
    assert response["error"]["code"] == -32601
    assert not (Path(agent[1]) / "must-not-exist").exists()


@pytest.mark.parametrize(
    ("settings", "error", "last_method"),
    [
        ({"protocol": 2}, "protocol version 1", "initialize"),
        ({"initialize": "hang"}, "initialize timed out", "initialize"),
        ({"new": "hang"}, "session/new timed out", "session/new"),
    ],
)
async def test_setup_failure_closes_owned_process(
    agent, monkeypatch, settings, error, last_method
) -> None:
    configure(agent, **settings)
    # The initialize deadline includes interpreter startup. Allow that startup
    # before testing a protocol refusal or a deliberately unanswered request.
    monkeypatch.setattr(acp_client, "REQUEST_TIMEOUT", 3)
    with pytest.raises(BridgeError, match=error):
        async with KiroACP(*agent, ignore_update, cancel_permission) as client:
            await client.open_session()
    assert received(agent)[-1]["method"] == last_method
    pid = int((Path(agent[1]) / "agent-pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_connect_failure_does_not_leak_the_underlying_exception_text(
    tmp_path: Path,
) -> None:
    """Finding S5: a connect failure in __aenter__'s except BaseException branch
    must report only the cause's class, mirroring the sanitize-before-raising
    behaviour of a plugin's native export for this unrelated failure site."""
    # pragma: allowlist nextline secret -- a canary substring, not a credential.
    unsanitized = "canary-leak-9f3c1-do-not-surface"
    missing_binary = str(tmp_path / unsanitized / "fake-kiro")
    with pytest.raises(BridgeError) as excinfo:
        async with KiroACP(missing_binary, str(tmp_path), ignore_update, cancel_permission):
            pass
    message = str(excinfo.value)
    assert unsanitized not in message
    assert "FileNotFoundError" in message
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)


async def test_call_failure_and_timeout_do_not_leak_the_underlying_exception_text(
    agent, monkeypatch
) -> None:
    """ACPClient._call's except Exception and except TimeoutError branches alike
    must never place the underlying cause's own text into the BridgeError."""
    monkeypatch.setattr(acp_client, "REQUEST_TIMEOUT", 3)
    configure(agent, initialize="hang")
    with pytest.raises(BridgeError, match="initialize timed out") as timeout_error:
        async with KiroACP(*agent, ignore_update, cancel_permission) as client:
            await client.open_session()
    assert "hang" not in str(timeout_error.value)

    # pragma: allowlist nextline secret -- a canary substring, not a credential.
    unsanitized = "canary-leak-call-failure-9f1a"
    configure(agent, error_message=unsanitized)
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError) as call_error:
            await client.prompt(session, "error")
    message = str(call_error.value)
    assert unsanitized not in message
    assert "RequestError" in message


async def test_failure_messages_keep_the_class_of_the_cause_and_not_its_text(agent) -> None:
    """Finding S5, the repro: a coroutine that raises acp.RequestError(-32603,
    <free-form provider text>, data) must surface only the operation label, the
    class RequestError and the code -32603 -- never the text or the data. A
    FileNotFoundError in __aenter__ must surface only its class, not the path.
    A BridgeError raised by the bridge's own permission callback keeps its own
    reviewed text, because that text is not an external cause."""
    # pragma: allowlist nextline secret -- canary substrings, not credentials.
    unsanitized_text = "canary-leak-request-error-9c21"
    unsanitized_data = {"path": "/Users/example/.ssh/private-canary-9c21"}
    configure(agent, error_code=-32603, error_message=unsanitized_text, error_data=unsanitized_data)
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError) as request_error:
            await client.prompt(session, "error")
    message = str(request_error.value)
    assert "session/prompt" in message
    assert "RequestError" in message
    assert "-32603" in message
    assert unsanitized_text not in message
    assert "private-canary-9c21" not in message
    cause = request_error.value.__cause__
    assert isinstance(cause, RequestError)
    assert cause.code == -32603
    # the cause itself still carries the provider's text; chained, never shown.
    assert str(cause) == unsanitized_text

    # pragma: allowlist nextline secret -- a canary substring, not a credential.
    unsanitized_path = str(Path(agent[1]) / "canary-leak-connect-path")
    with pytest.raises(BridgeError) as connect_error:
        async with KiroACP(unsanitized_path, agent[1], ignore_update, cancel_permission):
            pass
    connect_message = str(connect_error.value)
    assert "FileNotFoundError" in connect_message
    assert unsanitized_path not in connect_message

    async def choose_with_bridge_error(request: dict[str, Any]) -> str | None:
        raise BridgeError("Permission request exceeds the supported bounds")

    async with KiroACP(*agent, ignore_update, choose_with_bridge_error) as client:
        session = await client.open_session()
        with pytest.raises(
            BridgeError, match="Permission request exceeds the supported bounds"
        ) as permission_error:
            await client.prompt(session, "permission")
    assert "ACP permission callback failed" in str(permission_error.value)


def _process_alive(pid: int) -> bool:
    """True while ``pid`` can still run.

    A killed grandchild is reparented to PID 1. On a Mac, launchd reaps it at once, so
    ``os.kill(pid, 0)`` fails. In a container (GitHub runners, ``act``) PID 1 is a plain
    shell that never reaps, so the zombie stays in the table and ``os.kill`` keeps
    succeeding although the process is dead. Read its state when ``/proc`` exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[-1].split()
        return fields[0] not in ("Z", "X")
    except (FileNotFoundError, IndexError, OSError):
        return True


async def test_close_is_idempotent_and_stops_owned_process_group(agent) -> None:
    configure(agent, descendant=True)
    client = KiroACP(*agent, ignore_update, cancel_permission)
    async with client:
        await client.open_session()
        child_pid = int((Path(agent[1]) / "descendant-pid").read_text())
        pid = int((Path(agent[1]) / "agent-pid").read_text())
        assert os.getpgid(child_pid) == pid
    await client.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    async with asyncio.timeout(3):
        while True:
            if not _process_alive(child_pid):
                break
            await asyncio.sleep(0.02)
    with pytest.raises(BridgeError, match="closed|connected"):
        await client.open_session()


async def test_cancelling_prompt_task_stops_process_before_reuse(agent) -> None:
    started = asyncio.Event()

    async def on_update(update):
        started.set()

    async with KiroACP(*agent, on_update, cancel_permission) as client:
        session = await client.open_session()
        pending = asyncio.create_task(client.prompt(session, "wait"))
        await asyncio.wait_for(started.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        pid = int((Path(agent[1]) / "agent-pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(BridgeError):
            await client.prompt(session, "hello")


async def test_prompt_completion_cancels_unresolved_permission(agent) -> None:
    released = asyncio.Event()

    async def choose(request):
        try:
            await asyncio.Future()
        finally:
            released.set()

    async with KiroACP(*agent, ignore_update, choose) as client:
        session = await client.open_session()
        await client.prompt(session, "permission-finishes")
        await asyncio.wait_for(released.wait(), 1)
        await wait_file(Path(agent[1]) / "permission-response.json")
        response = json.loads((Path(agent[1]) / "permission-response.json").read_text())
        assert response["result"]["outcome"] == {"outcome": "cancelled"}


async def test_delayed_replay_callback_cannot_become_new_prompt_output(agent) -> None:
    replay_started = asyncio.Event()
    replay_release = asyncio.Event()
    prompt_started = asyncio.Event()

    async def on_update(update):
        text = update.get("content", {}).get("text")
        if text == "Historical answer":
            replay_started.set()
            await replay_release.wait()
        elif text == "Hello ":
            prompt_started.set()

    async with KiroACP(*agent, on_update, cancel_permission) as client:
        await client.open_session("saved")
        await asyncio.wait_for(replay_started.wait(), 3)
        prompt = asyncio.create_task(client.prompt("saved", "hello"))
        await asyncio.wait_for(prompt_started.wait(), 3)
        replay_release.set()
        assert (await prompt).text == "Hello world"


async def test_cleanup_failure_leaves_native_stop_witness_unknown(agent, monkeypatch) -> None:
    real_stop = acp_client.stop_child

    async def failed_stop(process):
        raise CommandCleanupError("test cleanup failure")

    client = await KiroACP(*agent, ignore_update, cancel_permission).__aenter__()
    try:
        await client.open_session()
        assert client._process is not None
        process = client._process
        monkeypatch.setattr(acp_client, "stop_child", failed_stop)
        with pytest.raises(CommandCleanupError, match="test cleanup failure"):
            await client.close()
        assert client.native_stopped is None
    finally:
        monkeypatch.setattr(acp_client, "stop_child", real_stop)
        await real_stop(process)


async def test_concurrent_close_callers_wait_for_process_cleanup(agent, monkeypatch) -> None:
    stopping = asyncio.Event()
    release = asyncio.Event()
    real_stop = acp_client.stop_child

    async def delayed_stop(process):
        stopping.set()
        await release.wait()
        await real_stop(process)

    monkeypatch.setattr(acp_client, "stop_child", delayed_stop)
    client = await KiroACP(*agent, ignore_update, cancel_permission).__aenter__()
    first = asyncio.create_task(client.close())
    await asyncio.wait_for(stopping.wait(), 3)
    second = asyncio.create_task(client.close())
    try:
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)


async def test_fast_agent_reply_before_writer_drain_preserves_response_window(
    agent, monkeypatch
) -> None:
    update_arrived = asyncio.Event()

    async def on_update(update):
        update_arrived.set()

    async with KiroACP(*agent, on_update, cancel_permission) as client:
        session = await client.open_session()
        original_drain = asyncio.StreamWriter.drain

        async def delayed_drain(writer):
            await original_drain(writer)
            await update_arrived.wait()

        monkeypatch.setattr(asyncio.StreamWriter, "drain", delayed_drain)
        result = await asyncio.wait_for(client.prompt(session, "hello"), 3)
        assert result.text == "Hello world"


def test_early_unencodable_update_is_buffered_and_replayed_like_a_late_one(agent) -> None:
    """Finding F3: a session/update holding a lone UTF-16 surrogate must survive the
    early-response buffer exactly like any other early message, not vanish.

    The message dict mirrors the StreamEvent.message shape _observe/_capture_message
    already consume elsewhere in this file (acp_client.py:470-533), with content.text
    set to a lone surrogate -- the exact repro finding F3 describes for
    acp_client.py:490-491's size count.
    """
    early = {
        "method": "session/update",
        "params": {
            "sessionId": "sid",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "\ud800"},
            },
        },
    }

    early_client = KiroACP(*agent, ignore_update, cancel_permission)
    early_client._active_session = "sid"
    # Before the fix, this raises UnicodeEncodeError directly (today the ACP SDK's
    # observer dispatch swallows it with a bare `except Exception: continue`, which
    # is why the message vanishes instead of surfacing here).
    early_client._observe(StreamEvent(direction=StreamDirection.INCOMING, message=early))
    assert early_client._early_messages == [early]
    assert not early_client._turn_cancelled.is_set()

    prompt_message = {"method": "session/prompt", "id": "p1", "params": {}}
    early_client._observe(StreamEvent(direction=StreamDirection.OUTGOING, message=prompt_message))
    assert not early_client._turn_cancelled.is_set()
    assert early_client._chunks == ["\ud800"]

    late_client = KiroACP(*agent, ignore_update, cancel_permission)
    late_client._active_session = "sid"
    late_client._prompt_request_id = "p1"
    late_client._collecting = True
    late_client._observe(StreamEvent(direction=StreamDirection.INCOMING, message=early))
    assert not late_client._turn_cancelled.is_set()
    assert late_client._chunks == early_client._chunks


@pytest.mark.parametrize("data", [b"x" * 33 + b"\n", b"x" * 33])
async def test_reader_caps_complete_and_partial_eof_frames(data) -> None:
    reader = asyncio.StreamReader(limit=32)
    reader.feed_data(data)
    reader.feed_eof()
    wrapped = acp_client.BoundedACPReader(reader, 32)
    with pytest.raises(ConnectionError, match="frame"):
        await wrapped.readuntil()


async def test_reader_handles_exact_bound_and_eof() -> None:
    reader = asyncio.StreamReader(limit=32)
    reader.feed_data(b"x" * 31 + b"\n")
    reader.feed_eof()
    wrapped = acp_client.BoundedACPReader(reader, 32)
    assert len(await wrapped.readuntil()) == 32
    with pytest.raises(asyncio.IncompleteReadError):
        await wrapped.readuntil()
    with pytest.raises(ConnectionError, match="frame"):
        await wrapped.readexactly(33)


# --- T-50 fix round 1 (B4): the dispatch signal's place in ACPClient._call ---
# Direct calls of _call, as tests/test_acp_cause_text.py makes them. These cannot run at
# f1f39c1 (no dispatches_prompt keyword there); they pass at b680c8d and pin its placement.


async def _no_permission(request: dict[str, Any]) -> None:
    return None


@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="asyncio.eager_task_factory exists from Python 3.12 on; the package allows 3.11",
)
@pytest.mark.parametrize("dispatches_prompt", [True, False])
async def test_dispatch_signal_is_set_before_the_request_can_run(dispatches_prompt) -> None:
    """With the standard eager task factory, the operation's first step runs inside
    create_task. It must already read the signal for a session/prompt call, and must
    read it false for any other call. Kills the mutant that sets it after create_task."""
    client = acp_client.ACPClient("fixture", ".", ignore_update, _no_permission)
    loop = asyncio.get_running_loop()
    client._failure = loop.create_future()
    observed: list[bool] = []

    async def operation() -> int:
        observed.append(client.prompt_dispatched)
        return 1

    factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        assert (
            await client._call(
                operation(),
                "session/prompt" if dispatches_prompt else "session/new",
                max_seconds=None,
                dispatches_prompt=dispatches_prompt,
            )
            == 1
        )
    finally:
        loop.set_task_factory(factory)
        await client.close()
    assert observed == [dispatches_prompt]
    assert client.prompt_dispatched is dispatches_prompt


@pytest.mark.skipif(not __debug__, reason="the guarded assertion is removed under python -O")
async def test_dispatch_signal_stays_false_when_the_guard_before_it_fails(monkeypatch) -> None:
    """The assertion that guards _call (no _failure future: _call reached before
    __aenter__) fires before the signal: the signal stays false and no task is made.
    Kills the mutant that sets the signal above the assertion."""
    client = acp_client.ACPClient("fixture", ".", ignore_update, _no_permission)
    made: list[Any] = []
    original = asyncio.create_task

    def recording(*args: Any, **kwargs: Any) -> Any:
        made.append(args)
        return original(*args, **kwargs)

    async def operation() -> int:
        return 1

    pending = operation()
    monkeypatch.setattr(asyncio, "create_task", recording)
    try:
        with pytest.raises(AssertionError):
            await client._call(pending, "session/prompt", max_seconds=None, dispatches_prompt=True)
    finally:
        monkeypatch.undo()
        pending.close()
    assert client.prompt_dispatched is False
    assert made == []
