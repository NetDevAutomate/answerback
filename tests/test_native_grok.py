import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.native_grok import GrokCLI, _GrokACP
from answerback_bridge.process import BridgeError


@pytest.fixture
def grok_agent(tmp_path: Path) -> tuple[str, str]:
    source = Path(__file__).parent / "fixtures" / "grok_acp_agent.py"
    binary = tmp_path / "fixture-grok"
    binary.write_text(f"#!{sys.executable}\n" + source.read_text())
    binary.chmod(0o700)
    (tmp_path / "grok-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


def own_home(agent: tuple[str, str]) -> Path:
    """A Grok home inside the test's own folder, so that neither GROK_HOME nor
    the account's ~/.grok is resolved. The folder need not exist.
    """
    return Path(agent[1]) / "native-home"


def configure(agent: tuple[str, str], **settings: Any) -> None:
    (Path(agent[1]) / "grok-settings.json").write_text(json.dumps(settings))


def requests(agent: tuple[str, str]) -> list[dict[str, Any]]:
    path = Path(agent[1]) / "grok-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def ignore_update(update: dict[str, Any]) -> None:
    pass


async def reject_permission(request: dict[str, Any]) -> None:
    return None


def test_grok_environment_excludes_unrelated_ambient_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """red_at_base: only Grok's declared inputs reach its child environment.

    pins_existing at reviewed tip: base-URL, unlisted proxy/certificate and Codex names stay out.
    """
    excluded = {
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_OTHER_SERVICE_TOKEN",
        "OPENAI_API_KEY",
        "HERDR_SESSION",
        "PYTHONPATH",
        "GROK_UNRECOGNIZED_SETTING",
        "OPENAI_BASE_URL",
        "FTP_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CODEX_CA_CERTIFICATE",
    }
    monkeypatch.setattr(os, "environ", dict.fromkeys(excluded, "dummy"))
    client = _GrokACP("unused", str(tmp_path), ignore_update, reject_permission, str(tmp_path))
    assert excluded.isdisjoint(client.environment())


def test_grok_environment_preserves_launch_authentication_and_forced_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pins_existing: retained inputs and explicit policy win over ambient overrides."""
    required = {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "XAI_API_KEY",
    }
    flags = {
        f"GROK_{vendor}_{feature}_ENABLED"
        for vendor in ("CURSOR", "CLAUDE")
        for feature in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS")
    }
    policy = flags | {"GROK_HOME", "GROK_DISABLE_AUTOUPDATER"}
    monkeypatch.setattr(os, "environ", dict.fromkeys(required | policy, "dummy"))
    client = _GrokACP("unused", str(tmp_path), ignore_update, reject_permission, str(tmp_path))
    environment = client.environment()
    assert set(environment) == required | policy
    assert environment["GROK_HOME"] == str(tmp_path)
    assert environment["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert all(environment[name] == "0" for name in flags)
    assert os.environ["GROK_HOME"] == "dummy"


def test_grok_environment_preserves_proxy_and_certificate_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pins_existing: BASE retains these inputs; their presence is red at the reviewed tip."""
    network_names = {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
    monkeypatch.setattr(os, "environ", {})
    client = _GrokACP("unused", str(tmp_path), ignore_update, reject_permission, str(tmp_path))
    policy_names = set(client.environment())
    assert network_names.isdisjoint(policy_names)
    monkeypatch.setattr(os, "environ", dict.fromkeys(network_names, "dummy"))
    environment = client.environment()
    assert set(environment) == network_names | policy_names
    assert environment["HTTPS_PROXY"] == "dummy"


def test_grok_environment_does_not_invent_absent_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pins_existing: a cached-profile launch does not require an ambient API credential."""
    monkeypatch.setattr(os, "environ", {})
    client = _GrokACP("unused", str(tmp_path), ignore_update, reject_permission, str(tmp_path))
    environment = client.environment()
    assert "XAI_API_KEY" not in environment
    assert "PATH" not in environment
    assert environment["GROK_HOME"] == str(tmp_path)


async def test_owned_launch_and_correlated_terminal_result(grok_agent, tmp_path) -> None:
    home = tmp_path / "native-home"
    async with GrokCLI(*grok_agent, ignore_update, reject_permission, home=home) as client:
        assert client.home == str(home.resolve())
        session = await client.open_session()
        result = await client.prompt(session, "hello", prompt_id="task-1")
        assert result.status == "completed"
        assert result.text == "Current answer"
        assert result.session_id == "admitted-session"
        assert result.evidence["resumable"] is False
        assert result.evidence["prompt_id"] == "task-1"
        assert result.evidence["profile_home"] == str(home.resolve())
        assert result.evidence["stop_reason"] == "end_turn"
        assert result.turn_id == str(result.evidence["rpc_request_id"])
    argv = json.loads((tmp_path / "grok-argv.json").read_text())
    assert argv[:4] == ["agent", "--no-leader", "stdio", "--leader-socket"]
    assert not Path(argv[4]).parent.exists()
    environment = json.loads((tmp_path / "grok-environment.json").read_text())
    assert environment["GROK_HOME"] == str(home.resolve())
    assert environment["GROK_DISABLE_AUTOUPDATER"] == "1"
    for vendor in ("CURSOR", "CLAUDE"):
        for feature in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
            assert environment[f"GROK_{vendor}_{feature}_ENABLED"] == "0"
    opened = next(item for item in requests(grok_agent) if item.get("method") == "session/new")
    assert opened["params"]["_meta"] == {"yoloMode": False, "autoMode": False}
    assert result.evidence["rpc_request_id"] == requests(grok_agent)[-1]["id"]
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "grok-pid").read_text()), 0)


def test_home_precedence_preserves_existing_native_auth_location(
    grok_agent, tmp_path, monkeypatch
) -> None:
    inherited = tmp_path / "inherited"
    monkeypatch.setenv("GROK_HOME", str(inherited))
    assert GrokCLI(*grok_agent, ignore_update, reject_permission).home == str(inherited.resolve())
    explicit = tmp_path / "explicit"
    assert GrokCLI(*grok_agent, ignore_update, reject_permission, home=explicit).home == str(
        explicit.resolve()
    )


async def test_provisional_session_id_cannot_become_admission(grok_agent) -> None:
    # T-51 X2 (Codex F2 / Grok major): the provider's own free-text message
    # ("Authentication required") is sanitized away, but a bare code alone gives
    # no instruction -- the bridge's own authentication sentence replaces it, and
    # neither the provider's message nor its "no auth method id provided" data
    # survives into the BridgeError.
    configure(grok_agent, auth_error=True)
    # Hermetic: an explicit home, so neither GROK_HOME nor ~/.grok is resolved.
    home = Path(grok_agent[1]) / "native-home"
    async with GrokCLI(*grok_agent, ignore_update, reject_permission, home=home) as client:
        with pytest.raises(
            BridgeError,
            match=(
                "^ACP session/new failed: RequestError -32000 \\(in ACP this code means "
                "that authentication is required: sign in with the provider's own CLI, "
                "then check this receipt before you retry\\)$"
            ),
        ) as excinfo:
            await client.open_session()
        assert "Authentication required" not in str(excinfo.value)
        assert "no auth method id provided" not in str(excinfo.value)
        with pytest.raises(BridgeError):
            await client.prompt("provisional", "hello", prompt_id="task-1")
    assert not any(item.get("method") == "session/prompt" for item in requests(grok_agent))


async def test_advertised_resume_does_not_prove_saved_policy_preservation(grok_agent) -> None:
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        with pytest.raises(BridgeError, match="resume.*policy|policy.*resume"):
            await client.open_session("saved")
    assert [item.get("method") for item in requests(grok_agent)] == ["initialize"]


async def test_setup_replay_and_post_terminal_text_are_excluded(grok_agent) -> None:
    configure(grok_agent, setup_replay=True)
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        result = await client.prompt(session, "hello", prompt_id="task-1")
        assert result.text == "Current answer"


async def test_exact_scoped_mcp_definitions_are_passed(grok_agent) -> None:
    servers = [{"name": "allowed", "command": "/bin/true", "args": [], "env": []}]
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        await client.open_session(mcp_servers=servers)
    opened = next(item for item in requests(grok_agent) if item.get("method") == "session/new")
    assert opened["params"]["mcpServers"] == servers


async def test_empty_startup_manifest_does_not_poison_later_session_admission(grok_agent) -> None:
    configure(grok_agent, initial_mcp_notification=[])
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session(
            mcp_servers=[{"name": "allowed", "command": "/bin/true", "args": [], "env": []}]
        )
        result = await client.prompt(session, "hello", prompt_id="startup-notification")
        assert result.status == "completed"


@pytest.mark.parametrize(
    "manifest",
    [
        [{"name": "unexpected", "command": "/bin/true", "args": [], "env": []}],
        None,
        [{"name": "malformed"}],
    ],
)
async def test_startup_manifest_still_refuses_inherited_or_malformed_scope(
    grok_agent, manifest
) -> None:
    configure(grok_agent, initial_mcp_notification=manifest)
    with pytest.raises(BridgeError, match="MCP"):
        async with GrokCLI(
            *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
        ) as client:
            await client.open_session()
    assert not any(item.get("method") == "session/new" for item in requests(grok_agent))


async def test_connection_manifest_does_not_require_a_per_session_repeat(grok_agent) -> None:
    configure(grok_agent, initial_mcp_notification=[], missing_mcp_snapshot=True)
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        assert await client.open_session() == "admitted-session"
    assert not any(item.get("method") == "session/prompt" for item in requests(grok_agent))


async def test_connection_manifest_cannot_hide_later_inherited_mcp_drift(grok_agent) -> None:
    configure(
        grok_agent,
        initial_mcp_notification=[],
        merged_mcp=[{"name": "unexpected", "command": "/bin/true", "args": [], "env": []}],
    )
    with pytest.raises(BridgeError, match="MCP"):
        async with GrokCLI(
            *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
        ) as client:
            await client.open_session()
    assert not any(item.get("method") == "session/prompt" for item in requests(grok_agent))


@pytest.mark.parametrize(
    "settings",
    [
        {"inherited_mcp": [{"name": "unrelated"}]},
        {"merged_mcp": [{"name": "unexpected"}]},
        {"missing_mcp_snapshot": True},
    ],
)
async def test_unverified_or_unexpected_mcp_scope_fails_closed(grok_agent, settings) -> None:
    configure(grok_agent, **settings)
    with pytest.raises(BridgeError, match="MCP"):
        async with GrokCLI(
            *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
        ) as client:
            await client.open_session()
    assert not any(item.get("method") == "session/prompt" for item in requests(grok_agent))


@pytest.mark.parametrize(
    "metadata",
    [
        {"grokShell": False},
        {"agentVersion": None},
        {"currentWorkingDirectory": "/somewhere-else"},
    ],
)
async def test_grok_identity_and_workspace_are_validated(grok_agent, metadata) -> None:
    configure(grok_agent, initialization_meta=metadata)
    with pytest.raises(BridgeError):
        async with GrokCLI(
            *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
        ):
            pass


async def test_interactive_authentication_is_not_started_automatically(grok_agent) -> None:
    configure(
        grok_agent,
        auth_methods=[{"id": "grok.com", "name": "Grok"}],
        auth_error=True,
    )
    # T-51 X2 (Codex F2 / Grok major): the provider's own free-text message
    # ("Authentication required") is sanitized away, but a bare code alone gives
    # no instruction -- the bridge's own authentication sentence replaces it, and
    # neither the provider's message nor its "no auth method id provided" data
    # survives into the BridgeError.
    # Hermetic: an explicit home, so neither GROK_HOME nor ~/.grok is resolved.
    home = Path(grok_agent[1]) / "native-home"
    async with GrokCLI(*grok_agent, ignore_update, reject_permission, home=home) as client:
        with pytest.raises(
            BridgeError,
            match=(
                "^ACP session/new failed: RequestError -32000 \\(in ACP this code means "
                "that authentication is required: sign in with the provider's own CLI, "
                "then check this receipt before you retry\\)$"
            ),
        ) as excinfo:
            await client.open_session()
    assert "Authentication required" not in str(excinfo.value)
    assert "no auth method id provided" not in str(excinfo.value)
    assert not any(item.get("method") == "authenticate" for item in requests(grok_agent))


async def test_advertised_cached_auth_uses_headless_flow(grok_agent) -> None:
    configure(grok_agent, auth_methods=[{"id": "cached_token", "name": "Cached token"}])
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        await client.open_session()
    auth = next(item for item in requests(grok_agent) if item.get("method") == "authenticate")
    assert auth["params"] == {"methodId": "cached_token", "_meta": {"headless": True}}


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("end_turn", "completed"), ("max_tokens", "failed"), ("refusal", "failed")],
)
async def test_stop_reasons_remain_distinct(grok_agent, reason, expected) -> None:
    configure(grok_agent, stop_reason=reason)
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        result = await client.prompt(session, "hello", prompt_id="task-1")
        assert result.status == expected
        assert result.evidence["stop_reason"] == reason


async def test_missing_final_text_preserves_native_terminal_status(grok_agent) -> None:
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        result = await client.prompt(session, "empty", prompt_id="task-1")
        assert result.status == "completed"
        assert result.text is None
        assert result.evidence["final_answer_available"] is False


@pytest.mark.parametrize(
    "message",
    [
        "wrong-session",
        "duplicate",
        "unknown-stop",
        "exit",
        "error",
        "malformed",
        "duplicate-key",
        "scalar",
        "oversized",
        "unsupported",
        "mcp-drift",
    ],
)
async def test_invalid_or_uncorrelated_protocol_never_completes(grok_agent, message) -> None:
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError):
            await asyncio.wait_for(client.prompt(session, message, prompt_id="task-1"), 3)


async def test_permission_only_offers_one_time_choices(grok_agent) -> None:
    offered = []

    async def choose(request):
        offered.extend(request["options"])
        return "no"

    async with GrokCLI(*grok_agent, ignore_update, choose, home=own_home(grok_agent)) as client:
        session = await client.open_session()
        result = await client.prompt(session, "permission", prompt_id="task-1")
        assert result.status == "completed"
    assert {option["kind"] for option in offered} == {"allow_once", "reject_once"}
    answers = [
        item
        for item in requests(grok_agent)
        if item.get("id") == "permission-1" and "result" in item
    ]
    assert answers[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}


async def test_persistent_permission_response_is_rejected(grok_agent) -> None:
    async def choose(request):
        return "forever"

    async with GrokCLI(*grok_agent, ignore_update, choose, home=own_home(grok_agent)) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="permission"):
            await client.prompt(session, "permission", prompt_id="task-1")


async def test_cancellation_resolves_all_pending_permissions_without_approval(grok_agent) -> None:
    waiting = asyncio.Event()
    requested = 0
    released = 0

    async def choose(request):
        nonlocal requested, released
        requested += 1
        if requested == 2:
            waiting.set()
        try:
            await asyncio.Future()
        finally:
            released += 1

    async with GrokCLI(*grok_agent, ignore_update, choose, home=own_home(grok_agent)) as client:
        session = await client.open_session()
        prompt = asyncio.create_task(client.prompt(session, "two-permissions", prompt_id="task-1"))
        await asyncio.wait_for(waiting.wait(), 3)
        await client.cancel(session)
        result = await asyncio.wait_for(prompt, 3)
        assert result.status == "cancelled"
        assert result.evidence["stop_reason"] == "cancelled"
        async with asyncio.timeout(3):
            while True:
                answers = [
                    item
                    for item in requests(grok_agent)
                    if item.get("id") in {"permission-1", "permission-2"} and "result" in item
                ]
                if len(answers) == 2:
                    break
                await asyncio.sleep(0.01)
    assert released == 2
    answers = [
        item
        for item in requests(grok_agent)
        if item.get("id") in {"permission-1", "permission-2"} and "result" in item
    ]
    assert len(answers) == 2
    assert all(item["result"]["outcome"] == {"outcome": "cancelled"} for item in answers)


async def test_cancel_notification_does_not_synthesize_terminal_completion(grok_agent) -> None:
    configure(grok_agent, ignore_cancel=True)
    started = asyncio.Event()

    async def on_update(update):
        started.set()

    async with GrokCLI(
        *grok_agent, on_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        prompt = asyncio.create_task(client.prompt(session, "wait", prompt_id="task-1"))
        await asyncio.wait_for(started.wait(), 3)
        await client.cancel(session)
        await asyncio.sleep(0)
        assert not prompt.done()
        prompt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prompt


async def test_duplicate_prompt_identity_and_overlapping_turns_are_refused(grok_agent) -> None:
    started = asyncio.Event()

    async def on_update(update):
        started.set()

    async with GrokCLI(
        *grok_agent, on_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        await client.prompt(session, "hello", prompt_id="task-1")
        with pytest.raises(BridgeError, match="prompt"):
            await client.prompt(session, "hello", prompt_id="task-1")
        started.clear()
        pending = asyncio.create_task(client.prompt(session, "wait", prompt_id="task-2"))
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(BridgeError, match="prompt"):
            await client.prompt(session, "hello", prompt_id="task-3")
        await client.cancel(session)
        assert (await pending).status == "cancelled"


async def test_mcp_validation_does_not_allow_sdk_to_discard_caller_scope(grok_agent) -> None:
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        with pytest.raises(BridgeError, match="MCP"):
            await client.open_session(mcp_servers=[{"name": "missing-launch-config"}])
    assert not any(item.get("method") == "session/new" for item in requests(grok_agent))


async def test_second_session_cannot_replace_the_worker_scope(grok_agent) -> None:
    async with GrokCLI(
        *grok_agent, ignore_update, reject_permission, home=own_home(grok_agent)
    ) as client:
        await client.open_session()
        with pytest.raises(BridgeError, match="one owned session"):
            await client.open_session()
    assert sum(item.get("method") == "session/new" for item in requests(grok_agent)) == 1


async def test_wrong_session_permission_never_reaches_human(grok_agent) -> None:
    configure(grok_agent, permission_session="unowned-session")
    called = False

    async def choose(request):
        nonlocal called
        called = True
        return "yes"

    async with GrokCLI(*grok_agent, ignore_update, choose, home=own_home(grok_agent)) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError, match="session"):
            await client.prompt(session, "permission", prompt_id="task-1")
    assert not called


async def test_terminal_identity_is_reported_before_permission_drain_failure(
    grok_agent, monkeypatch
) -> None:
    updates = []

    async def observe(update):
        updates.append(update)

    async def fail_drain():
        raise BridgeError("fixture permission response drain failed")

    async with GrokCLI(
        *grok_agent, observe, reject_permission, home=own_home(grok_agent)
    ) as client:
        session = await client.open_session()
        monkeypatch.setattr(client._client, "drain_permissions", fail_drain)
        with pytest.raises(BridgeError, match="drain failed"):
            await client.prompt(session, "hello", prompt_id="task-1")
    sent = next(item for item in requests(grok_agent) if item.get("method") == "session/prompt")
    assert [
        update for update in updates if update.get("sessionUpdate") == "native_terminal_observed"
    ] == [
        {
            "sessionUpdate": "native_terminal_observed",
            "session_id": session,
            "turn_id": str(sent["id"]),
        }
    ]
