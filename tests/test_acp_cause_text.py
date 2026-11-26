"""T-51 fix round: the real wire forms of RequestError.code, and the real
authentication sentence, through the real client classes and (where named)
the real worker/receipt layer (plan rule 6; T1, T2(a), T2(c) of the
specification). None of these build a test double: the fixture subprocess,
the real acp SDK, and the real Workers/NativeWorkers receipt owners produce
every input.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from composition import contributed

from answerback_bridge.acp_client import ACPClient, KiroACP
from answerback_bridge.config import Config
from answerback_bridge.native_grok import _GrokACP
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.process import BridgeError
from answerback_bridge.tasks import TaskStore
from answerback_bridge.workers import Workers

# The installed plugin's ACP client runs the same case (tests/composition.py).
PLUGIN_CLIENTS = contributed("ACP_CLIENTS", {})
ACP_CLIENTS = ["KiroACP", *PLUGIN_CLIENTS, "_GrokACP"]

# Written out, not imported from acp_client, so this file independently pins
# the bridge's own words (T-51 X2 / spec T2).
AUTH_SENTENCE = (
    "RequestError -32000 (in ACP this code means that authentication is "
    "required: sign in with the provider's own CLI, then check this receipt "
    "before you retry)"
)
UNUSABLE_CODE_TEXT = "RequestError (unusable error code)"


@pytest.fixture
def agent(tmp_path: Path) -> tuple[str, str]:
    fixture = Path(__file__).parent / "fixtures" / "acp_agent.py"
    binary = tmp_path / "fake-kiro"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    (tmp_path / "agent-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


def configure(agent: tuple[str, str], **settings: Any) -> None:
    (Path(agent[1]) / "agent-settings.json").write_text(json.dumps(settings))


async def ignore_update(update: dict[str, Any]) -> None:
    pass


async def cancel_permission(request: dict[str, Any]) -> None:
    return None


# --- T1: every wire form of `code`, through the real fixture and the real SDK ---


@pytest.mark.parametrize(
    "code",
    [
        "/private/canary-leak-code-path-7a2e",
        "-32000",
        {"path": "/private/canary-leak-code-data-7a2e"},
        ["canary-leak-code-list-7a2e"],
        None,
        True,
        False,
        -32000.0,
        2_147_483_648,
        -2_147_483_649,
        int("9" * 4000),
    ],
    ids=[
        "string-with-path",
        "numeric-string",
        "object",
        "array",
        "null",
        "true",
        "false",
        "float",
        "one-above-32-bit-max",
        "one-below-32-bit-min",
        "4000-digit-integer",
    ],
)
async def test_unusable_error_code_forms_disclose_nothing_of_the_value(
    agent: tuple[str, str], code: Any
) -> None:
    """red_at_base: at 33e7b69, error.code is interpolated with no type or range
    check (f"{name} {error.code}"), so every one of these shapes leaked straight
    into the BridgeError. T-51 X1 / Codex F1: none of them is an int JSON-RPC
    could have meant (a bool is a subclass of int and is excluded too), so each
    must give exactly the fixed "unusable error code" marker, with no part of
    the value, its type or its length anywhere in the message.
    """
    configure(agent, error_code=code, error_message="ordinary provider message")
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError) as excinfo:
            await client.prompt(session, "error")
    assert str(excinfo.value) == f"ACP session/prompt failed: {UNUSABLE_CODE_TEXT}"


@pytest.mark.parametrize("code", [-32603, 0, -1, 2147483647])
async def test_in_range_integer_codes_still_pass_through_unchanged(
    agent: tuple[str, str], code: int
) -> None:
    """pins_existing: a genuine 32-bit signed integer code that is not the
    authentication code keeps today's "RequestError <code>" shape -- the new
    type/range check must not reject a real code, only a fabricated one.
    """
    configure(agent, error_code=code, error_message="ordinary provider message")
    async with KiroACP(*agent, ignore_update, cancel_permission) as client:
        session = await client.open_session()
        with pytest.raises(BridgeError) as excinfo:
            await client.prompt(session, "error")
    assert str(excinfo.value) == f"ACP session/prompt failed: RequestError {code}"


@pytest.mark.parametrize(
    "code,case_id",
    [
        ("/private/canary-leak-receipt-path-c3d9", "string"),
        ({"path": "/private/canary-leak-receipt-data-c3d9"}, "object"),
        (True, "true"),
    ],
    ids=["string", "object", "true"],
)
async def test_worker_receipt_never_stores_any_part_of_an_unusable_code(
    agent: tuple[str, str], code: Any, case_id: str
) -> None:
    """red_at_base: exercises one of the handlers that store str(error) in a
    receipt's detail (providers/kiro/workers.py:151,174, Kiro's). The installed
    plugin's workers and providers/native/workers.py:397,450 format the
    identical, already-sanitized BridgeError the identical way
    (f"...{type(error).__name__}...: {error}"); the sanitize itself lives
    entirely in ACPClient._cause_text, shared by every ACPClient subclass
    (Kiro, a plugin's and the native CLI's inner Grok client alike), so a
    canary that cannot leak through this handler cannot leak through the
    others either -- they would have to receive a different BridgeError text
    to disagree, and _cause_text gives every caller the same one.
    """
    configure(
        agent,
        error_code=code,
        error_message="ordinary provider message",
        # Without a readiness plan, open_session waits the full MCP readiness
        # timeout for a notification the fixture never sends by default.
        mcp_open_plans=[
            {
                "before": [
                    {
                        "servers": [
                            {
                                "name": "answerback_bridge",
                                "status": "connected",
                                "tools": [
                                    {"name": name, "disabled": False}
                                    for name in ("peer_submit", "task_wait")
                                ],
                            }
                        ]
                    }
                ]
            }
        ],
    )
    config = Config(kiro_binary=agent[0], kiro_workspaces={"project": agent[1]})
    config_path = Path(agent[1]) / "config.json"
    config.save(config_path)
    store = TaskStore(Path(agent[1]) / "tasks.sqlite")
    workers = Workers(config, config_path, store)
    try:
        receipt = await workers.submit("project", "error", f"T1-receipt-{case_id}")
        await asyncio.wait_for(workers.jobs[receipt["id"]].task, 5)
        final = store.get(receipt["id"])
        assert final["status"] == "uncertain"
        assert final["detail"] == f"BridgeError: ACP session/prompt failed: {UNUSABLE_CODE_TEXT}"
    finally:
        await workers.close()


# --- T2(a): the authentication sentence, through _call, on all three client classes ---


@pytest.mark.parametrize("provider", ACP_CLIENTS)
async def test_authentication_sentence_replaces_every_providers_free_text(
    tmp_path: Path, provider: str
) -> None:
    """red_at_base: the sanitize lives entirely in ACPClient._cause_text, shared
    by every subclass, so _call can be exercised directly without a subprocess.
    A provider's own RequestError(-32000, <canary message>, {"path": <canary>})
    must give exactly the bridge's own authentication sentence (T-51 X2), with
    neither the provider's free-text message nor its path data anywhere in the
    result.
    """
    # pragma: allowlist nextline secret -- a canary substring, not a credential.
    canary = "canary-leak-auth-9e41"
    clients: dict[str, Callable[[], ACPClient]] = {
        "KiroACP": lambda: KiroACP("unused", str(tmp_path), ignore_update, cancel_permission),
        **{
            name: lambda build=build: build(tmp_path, ignore_update, cancel_permission)
            for name, build in PLUGIN_CLIENTS.items()
        },
        "_GrokACP": lambda: _GrokACP(
            "unused", str(tmp_path), ignore_update, cancel_permission, str(tmp_path)
        ),
    }
    client = clients[provider]()
    client._failure = asyncio.get_running_loop().create_future()

    async def denied() -> None:
        raise RequestError(-32000, canary, {"path": canary})

    with pytest.raises(BridgeError) as excinfo:
        await client._call(denied(), "session/new", max_seconds=2)
    message = str(excinfo.value)
    assert message == f"ACP session/new failed: {AUTH_SENTENCE}"
    assert canary not in message


# --- T2(c): the same failure, through the real NativeWorkers + real GrokCLI ---


async def test_authentication_receipt_through_native_workers_ends_failed(
    tmp_path: Path,
) -> None:
    """red_at_base: the real NativeWorkers, the real GrokCLI and the real
    tests/fixtures/grok_acp_agent.py fixture. The failure happens at
    open_session, before session/prompt, so nothing was submitted and the
    receipt ends failed (not uncertain); its detail carries the bridge's own
    sentence and neither the provider's message nor its data.
    """
    source = Path(__file__).parent / "fixtures" / "grok_acp_agent.py"
    binary = tmp_path / "fixture-grok"
    binary.write_text(f"#!{sys.executable}\n" + source.read_text())
    binary.chmod(0o700)
    (tmp_path / "grok-settings.json").write_text(
        json.dumps({"auth_error": True, "auth_methods": [{"id": "grok.com", "name": "Grok"}]})
    )
    config = Config(
        grok_binary=str(binary),
        grok_workspaces={"project": str(tmp_path)},
        grok_home=str(tmp_path),
    )
    config.select_provider("grok", True)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    store = TaskStore(tmp_path / "tasks.sqlite")
    workers = NativeWorkers("grok", config, config_path, store)
    try:
        receipt = await workers.submit("project", "hello", "T2c-auth-receipt")
        await asyncio.wait_for(workers.jobs[receipt["id"]].task, 5)
        final = store.get(receipt["id"])
        assert final["status"] == "failed"
        assert AUTH_SENTENCE in final["detail"]
        assert "Authentication required" not in final["detail"]
        assert "no auth method id provided" not in final["detail"]
    finally:
        await workers.close()
