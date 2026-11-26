import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.core import Core


def terminal(address: str = "t1", **changes: Any) -> dict[str, Any]:
    return {
        "address": address,
        "argv": ["/bin/zsh"],
        "pid": 100,
        "created_ms": 1794570000000,
        "cwd": "/work/launch",
        "foreground": {
            "pid": 101,
            "name": "2.1.322",
            "argv": ["claude", "--dangerously-skip-permissions"],
            "cwd": "/work/Analytics",
        },
        "identity": "ghostty",
        "state": "live",
        "status": None,
        "turn": 0,
        "labels": {"herdr.space": "Projects", "herdr.tab": "Review", "where": "Desk"},
        "reported": {},
        **changes,
    }


@dataclass
class CoreCLI:
    binary: Path
    settings_path: Path
    log_path: Path

    def update(self, **changes: Any) -> None:
        current = json.loads(self.settings_path.read_text())
        self.settings_path.write_text(json.dumps(current | changes))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


@pytest.fixture
def core_cli(tmp_path: Path) -> CoreCLI:
    cli = CoreCLI(tmp_path / "hc", tmp_path / "settings.json", tmp_path / "calls.jsonl")
    cli.settings_path.write_text(
        json.dumps(
            {
                "inventory": {
                    "terminals": [{"address": "t1", "agent": None, "cwd": "/work/launch"}],
                    "machines": [],
                    "hidden_finished": 0,
                },
                "terminals": {"t1": terminal()},
                "read": {"complete": False, "how": "screen", "text": "Last visible lines\n"},
                "send": {"turn": 1, "started": True, "status": "working"},
            }
        )
    )
    cli.binary.write_text(f"""#!{sys.executable}
import json
import os
import sys
import time
from pathlib import Path

settings_path = Path({str(cli.settings_path)!r})
settings = json.loads(settings_path.read_text())
args = sys.argv[1:]
with Path({str(cli.log_path)!r}).open("a") as log:
    environment = {{k: v for k, v in os.environ.items() if k.startswith(("HC_", "XDG_"))}}
    log.write(json.dumps({{"args": args, "env": environment}}) + "\\n")
if settings.get("fail") == args[0]:
    print("private child stderr sk-secret-example", file=sys.stderr)
    sys.exit(3)
if args[0] == "get" and settings.get("observe_activity"):
    with settings_path.with_name("activity.jsonl").open("a") as log:
        log.write(json.dumps({{"event": "start", "target": args[1]}}) + "\\n")
if settings.get("delay", {{}}).get(args[0]):
    time.sleep(settings["delay"][args[0]])
if args[0] == "get":
    time.sleep(settings.get("target_delay", {{}}).get(args[1], 0))
if args == ["ls", "--json"]:
    value = settings["inventory"]
elif args[0] == "get" and args[-1] == "--json":
    value = settings["terminals"].get(args[1])
    if value is None:
        sys.exit(4)
elif args[:2] == ["read", "--lines"] and args[3] == "--json":
    value = settings["read"]
elif args[:5] == ["send", "--if", "idle", "--json", "--"]:
    value = settings["send"]
    if settings.get("after_send"):
        settings["terminals"][args[5]].update(settings["after_send"])
        settings_path.write_text(json.dumps(settings))
else:
    sys.exit(2)
if args[0] == "get" and settings.get("observe_activity"):
    with settings_path.with_name("activity.jsonl").open("a") as log:
        log.write(json.dumps({{"event": "end", "target": args[1]}}) + "\\n")
if settings.get("raw", {{}}).get(args[0]) is not None:
    print(settings["raw"][args[0]])
else:
    print(json.dumps(value))
""")
    cli.binary.chmod(0o700)
    return cli


async def test_discovers_unreported_claude_using_foreground_argv_and_current_directory(
    core_cli: CoreCLI,
) -> None:
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is True
    assert result["errors"] == {}
    peer = result["peers"][0]
    assert peer["peer_id"] == "hc:t1"
    assert peer["provider"] == "hc"
    assert peer["target"] == "t1"
    assert peer["agent_kind"] == "claude"
    assert peer["project"] == "/work/Analytics"
    assert peer["project_known"] is True
    assert peer["placement"] == {"herdr.space": "Projects", "herdr.tab": "Review", "where": "Desk"}
    assert peer["state"] == "unknown"
    assert peer["send_ready"] is False
    assert peer["capabilities"] == ["read"]
    assert len(peer["binding"]) == 64
    assert not {"argv", "foreground_argv"} & peer.keys()
    assert peer["agent_identity_source"] == "foreground_argv"
    assert peer["foreground_known"] is True
    assert "dangerously-skip-permissions" not in json.dumps(result)


async def test_read_preserves_native_coverage_without_claiming_completion(
    core_cli: CoreCLI,
) -> None:
    result = await Core(str(core_cli.binary)).read("t1", lines=23)
    assert result == {
        "target": "t1",
        "complete": False,
        "how": "screen",
        "text": "Last visible lines\n",
    }
    assert core_cli.calls()[-1]["args"] == ["read", "--lines", "23", "--json", "t1"]


async def test_sends_literal_message_only_to_selected_idle_identity(core_cli: CoreCLI) -> None:
    core_cli.update(
        terminals={"t1": terminal(status={"app": "claude", "state": "idle"})},
        after_send={"status": {"app": "claude", "state": "working"}, "turn": 1},
    )
    core = Core(str(core_cli.binary))
    selected = await core.peer("t1")
    assert selected["send_ready"] is True
    message = "Review `literal` $(still-literal); 'quotes'\nThen report."
    receipt = await core.send("t1", message, expected=selected)
    assert receipt == {
        "target": "t1",
        "turn": 1,
        "started": True,
        "status": "working",
        "identity_guard": "observed_before_and_after",
        "identity_observer": "core:local",
    }
    sent = [call for call in core_cli.calls() if call["args"][0] == "send"]
    assert [call["args"] for call in sent] == [
        ["send", "--if", "idle", "--json", "--", "t1", message]
    ]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": None}, "unreported"),
        ({"status": {"app": "claude", "state": "working"}}, "working"),
        ({"status": {"app": "claude", "state": "blocked"}}, "blocked"),
        # `done` is covered in test_core_send_policy.py.
        ({"status": {"app": "claude", "state": "error"}}, "error"),
        ({"state": "finished"}, "not_live"),
        ({"status": {"app": "kiro-cli", "state": "idle"}}, "status_mismatch"),
    ],
)
async def test_readable_agents_explain_why_send_is_unavailable(
    core_cli: CoreCLI, changes: dict[str, Any], reason: str
) -> None:
    from codex_herdr_bridge.core import CoreNotReady

    row = terminal(status={"app": "claude", "state": "idle"}) | changes
    core_cli.update(terminals={"t1": row})
    core = Core(str(core_cli.binary))
    peer = await core.peer("t1")
    assert peer["capabilities"] == ["read"]
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == reason
    with pytest.raises(CoreNotReady):
        await core.send("t1", "Review", expected=peer)
    assert not any(call["args"][0] == "send" for call in core_cli.calls())


async def test_foreground_directory_unknown_never_falls_back_to_launch_or_placement(
    core_cli: CoreCLI,
) -> None:
    row = terminal(status={"app": "claude", "state": "idle"})
    row["foreground"]["cwd"] = None
    core_cli.update(terminals={"t1": row})
    peer = await Core(str(core_cli.binary)).peer("t1")
    assert peer["name"] == "Desk"
    assert peer["project"] == ""
    assert peer["project_known"] is False
    assert peer["send_readiness_reason"] == "current_directory_unknown"


async def test_shell_is_excluded_even_with_old_agent_status(core_cli: CoreCLI) -> None:
    row = terminal(status={"app": "claude", "state": "idle"})
    row["foreground"] = {"argv": ["/bin/zsh"], "pid": 102, "cwd": "/work"}
    core_cli.update(terminals={"t1": row})
    inventory = await Core(str(core_cli.binary)).inventory()
    assert inventory["peers"] == []
    assert inventory["complete"] is True
    assert inventory["excluded_non_agents"] == 1


async def test_remote_agent_can_be_read_but_never_sent_to(core_cli: CoreCLI) -> None:
    from codex_herdr_bridge.core import CoreNotReady

    core_cli.update(
        terminals={
            "example-host/t7": terminal(
                "example-host/t7", status={"app": "claude", "state": "idle"}
            )
        }
    )
    core = Core(str(core_cli.binary))
    peer = await core.peer("example-host/t7")
    assert peer["send_readiness_reason"] == "remote_read_only"
    assert (await core.read("example-host/t7"))["text"] == "Last visible lines\n"
    with pytest.raises(CoreNotReady):
        await core.send("example-host/t7", "Review", expected=peer)
    assert not any(call["args"][0] == "send" for call in core_cli.calls())


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    "change",
    [
        {"pid": 300},
        {"created_ms": 1794570001000},
        {"argv": ["/bin/zsh", "--new-instance"]},
        {"foreground": {"pid": 301, "argv": ["claude"], "cwd": "/work/Analytics"}},
        {"foreground": {"pid": 101, "argv": ["claude", "--different"], "cwd": "/work/Analytics"}},
        {
            "foreground": {
                "pid": 101,
                "argv": ["claude", "--dangerously-skip-permissions"],
                "cwd": "/work/other",
            }
        },
        {"foreground": {"pid": 101, "argv": ["/bin/zsh"], "cwd": "/work/Analytics"}},
        {"address": "t2"},
    ],
)
async def test_process_and_directory_changes_invalidate_selected_identity(
    core_cli: CoreCLI, when: str, change: dict[str, Any]
) -> None:
    from codex_herdr_bridge.core import CoreIdentityChanged

    original = terminal(status={"app": "claude", "state": "idle"})
    core_cli.update(terminals={"t1": original})
    core = Core(str(core_cli.binary))
    selected = await core.peer("t1")
    if when == "before":
        core_cli.update(terminals={"t1": original | change})
    else:
        core_cli.update(after_send=change)
    with pytest.raises(CoreIdentityChanged):
        await core.send("t1", "Review", expected=selected)
    sends = [call for call in core_cli.calls() if call["args"][0] == "send"]
    assert len(sends) == (1 if when == "after" else 0)


async def test_placement_and_launch_directory_changes_do_not_rebind_current_agent(
    core_cli: CoreCLI,
) -> None:
    original = terminal(status={"app": "claude", "state": "idle"})
    core_cli.update(terminals={"t1": original})
    core = Core(str(core_cli.binary))
    selected = await core.peer("t1")
    core_cli.update(
        terminals={"t1": original | {"cwd": "/work/stale", "labels": {"where": "Moved"}}}
    )
    assert (await core.send("t1", "Review", expected=selected))["turn"] == 1


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        [],
        {},
        {"turn": True, "started": True, "status": "working"},
        {"turn": -1, "started": True, "status": "working"},
        {"turn": 1.2, "started": True, "status": "working"},
        {"turn": 1, "status": "working"},
        {"turn": 1, "started": True},
        {"turn": 1, "started": "true", "status": "working"},
        {
            "turn": 1,
            "started": True,
            "status": {"secret": "sk-secret-example"},  # pragma: allowlist secret
        },
        {"turn": 1, "started": True, "status": "working", "address": "t2"},
        {"error": "sk-secret-example"},
    ],
)
async def test_malformed_send_receipt_is_uncertain_and_never_retried(
    core_cli: CoreCLI, receipt: Any
) -> None:
    from codex_herdr_bridge.process import BridgeError

    core_cli.update(
        terminals={"t1": terminal(status={"app": "claude", "state": "idle"})},
        send=receipt,
    )
    core = Core(str(core_cli.binary))
    selected = await core.peer("t1")
    with pytest.raises(BridgeError, match="uncertain") as error:
        await core.send("t1", "Review", expected=selected)
    assert "sk-secret-example" not in str(error.value)
    assert len([call for call in core_cli.calls() if call["args"][0] == "send"]) == 1


@pytest.mark.parametrize("started", [False, None])
async def test_nullable_native_receipt_does_not_claim_completion(
    core_cli: CoreCLI, started: bool | None
) -> None:
    core_cli.update(
        terminals={"t1": terminal(status={"app": "claude", "state": "idle"})},
        send={"turn": 0, "started": started, "status": None},
    )
    core = Core(str(core_cli.binary))
    receipt = await core.send("t1", "Review", expected=await core.peer("t1"))
    assert receipt["started"] is started
    assert receipt["status"] is None
    assert "completed" not in receipt.values()


@pytest.mark.parametrize(
    ("started", "status", "exit_code"),
    [(None, "uncertain", 1), (False, "uncertain", 1), (True, "submitted", 0)],
    ids=["null-red_at_base", "false-pins_existing", "true-pins_existing"],
)
async def test_native_delivery_requires_affirmative_start(
    core_cli: CoreCLI, tmp_path: Path, started: bool | None, status: str, exit_code: int
) -> None:
    """Exercise the native fixture, async delivery, durable receipt and CLI status."""
    from codex_herdr_bridge.cli import exit_code_for, parser
    from codex_herdr_bridge.core_tasks import CoreTasks
    from codex_herdr_bridge.tasks import TaskStore

    core_cli.update(
        terminals={"t1": terminal(status={"app": "claude", "state": "idle"})},
        send={"turn": 0, "started": started, "status": None},
    )
    store = TaskStore(tmp_path / "tasks.sqlite")
    tasks = CoreTasks(store, tmp_path / "config.json")
    try:
        receipt = await tasks.submit(Core(str(core_cli.binary)), "t1", "Review", "review")
        await asyncio.gather(*tasks.jobs.values())
        saved = TaskStore(store.path).get(receipt["id"])
        assert saved["status"] == status
        assert saved["result"] is None
        assert saved["metadata"]["delivery"]["started"] is started
        assert saved["metadata"]["delivery_verified"] is True
        assert saved["detail"] == (
            None if started is True else "Core accepted text but did not observe the turn start"
        )
        assert exit_code_for(parser().parse_args(["task", saved["id"]]), saved) == exit_code
        assert len([call for call in core_cli.calls() if call["args"][0] == "send"]) == 1
    finally:
        await tasks.close()


@pytest.mark.parametrize("started", [None, False, True])
def test_provisional_result_precedes_start_confirmation(
    tmp_path: Path, started: bool | None
) -> None:
    """pins_existing: an early result still wins once delivery identity is verified."""
    from codex_herdr_bridge.tasks import TaskStore

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:t1", "Review", "review", {"kind": "hc"})
    provisional = store.complete_worker(task["id"], "Reviewed")
    assert provisional["status"] == "pending"
    assert provisional["result"] is None
    saved = store.confirm_core_delivery(task["id"], {"turn": 0, "started": started, "status": None})
    assert saved["status"] == "completed"
    assert saved["result"] == "Reviewed"
    assert "provisional_result" not in saved["metadata"]


@pytest.mark.parametrize("result", [["Reviewed"], {"text": "Reviewed"}, 7, None])
def test_run6_a_non_string_result_cannot_wedge_a_provisional_receipt(
    tmp_path: Path, result: Any
) -> None:
    """red_at_base (run-6 hardening h8): a non-string provisional result blocked completion."""
    from codex_herdr_bridge.tasks import TaskStore

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:t1", "Review", "review", {"kind": "hc"})
    with pytest.raises(ValueError, match="Result must be a string"):
        store.complete_worker(task["id"], result)
    assert "provisional_result" not in store.get(task["id"])["metadata"]
    store.complete_worker(task["id"], "Reviewed")
    saved = store.confirm_core_delivery(task["id"], {"turn": 0, "started": True, "status": None})
    assert saved["status"] == "completed" and saved["result"] == "Reviewed"


async def test_interpreter_script_path_identifies_known_agent(core_cli: CoreCLI) -> None:
    row = terminal(status={"app": "kiro-cli", "state": "idle"})
    row["foreground"] = {
        "pid": 101,
        "argv": ["/usr/bin/python3.13", "/installed/kiro-cli"],
        "cwd": "/work/Analytics",
    }
    core_cli.update(terminals={"t1": row})
    peer = await Core(str(core_cli.binary)).peer("t1")
    assert peer["agent_kind"] == "kiro"
    assert peer["state"] == "idle"
    assert peer["send_ready"] is True


@pytest.mark.parametrize(
    "target",
    [
        "",
        "reviewer",
        "--force",
        "hc:t1",
        "t1 --force",
        "../t1",
        "/t1",
        "example host/t1",
        "mac/t1/extra",
        "t1\n",
        "t_1",
        None,
    ],
)
async def test_rejects_names_and_options_before_running_native_commands(
    core_cli: CoreCLI, target: Any
) -> None:
    core = Core(str(core_cli.binary))
    with pytest.raises(ValueError):
        await core.peer(target)
    with pytest.raises(ValueError):
        await core.read(target)
    with pytest.raises(ValueError):
        await core.send(target, "Review", expected={})
    assert core_cli.calls() == []


@pytest.mark.parametrize("lines", [0, -1, 501, 1.5, "10", True, None])
async def test_rejects_unbounded_or_ambiguous_read_limits(core_cli: CoreCLI, lines: Any) -> None:
    with pytest.raises(ValueError):
        await Core(str(core_cli.binary)).read("t1", lines=lines)
    assert core_cli.calls() == []


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"complete": 1, "how": "screen", "text": "visible"},
        {"complete": True, "how": [], "text": "visible"},
        {"complete": True, "how": "screen", "text": None},
    ],
)
async def test_rejects_malformed_native_read_coverage(core_cli: CoreCLI, response: Any) -> None:
    from codex_herdr_bridge.process import BridgeError

    core_cli.update(read=response)
    with pytest.raises(BridgeError):
        await Core(str(core_cli.binary)).read("t1")


async def test_scrubs_inherited_core_route_but_preserves_xdg_environment(
    core_cli: CoreCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HC_TERMINAL_ID", "twrong")
    monkeypatch.setenv("HC_SOCKET", "/wrong/socket")
    monkeypatch.setenv("HC_SESSION", "wrong-session")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/work/xdg-config")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/work/xdg-runtime")
    explicit = Core(str(core_cli.binary), session="review")
    selected = await explicit.peer("t1")
    default = await Core(str(core_cli.binary)).peer("t1")
    assert selected["binding"] != default["binding"]
    first, second = core_cli.calls()
    assert first["env"]["HC_SESSION"] == "review"
    assert "HC_SESSION" not in second["env"]
    for call in (first, second):
        assert "HC_TERMINAL_ID" not in call["env"]
        assert "HC_SOCKET" not in call["env"]
        assert call["env"]["XDG_CONFIG_HOME"] == "/work/xdg-config"
        assert call["env"]["XDG_RUNTIME_DIR"] == "/work/xdg-runtime"


async def test_different_session_snapshot_cannot_authorize_send(core_cli: CoreCLI) -> None:
    from codex_herdr_bridge.core import CoreIdentityChanged

    selected = await Core(str(core_cli.binary), session="first").peer("t1")
    core_cli.log_path.unlink()
    with pytest.raises(CoreIdentityChanged):
        await Core(str(core_cli.binary), session="second").send("t1", "Review", expected=selected)
    assert core_cli.calls() == []


@pytest.mark.parametrize("expected", [{}, None, {"binding": "0" * 64}])
async def test_requires_selected_identity_before_any_native_operation(
    core_cli: CoreCLI, expected: Any
) -> None:
    from codex_herdr_bridge.core import CoreIdentityChanged

    with pytest.raises(CoreIdentityChanged):
        await Core(str(core_cli.binary)).send("t1", "Review", expected=expected)
    assert core_cli.calls() == []


@pytest.mark.parametrize("operation", ["get", "read", "send"])
async def test_child_errors_never_disclose_stderr(core_cli: CoreCLI, operation: str) -> None:
    from codex_herdr_bridge.process import BridgeError

    core = Core(str(core_cli.binary))
    core_cli.update(terminals={"t1": terminal(status={"app": "claude", "state": "idle"})})
    peer = await core.peer("t1")
    core_cli.update(fail=operation)
    with pytest.raises(BridgeError) as error:
        if operation == "get":
            await core.peer("t1")
        elif operation == "read":
            await core.read("t1")
        else:
            await core.send("t1", "Review", expected=peer)
    assert "sk-secret-example" not in str(error.value)
    assert error.value.__suppress_context__


async def test_partial_inventory_retains_agents_and_exact_get_is_independent(
    core_cli: CoreCLI,
) -> None:
    core_cli.update(
        inventory={
            "terminals": [
                {"address": "t1"},
                {"address": "t2"},
                {"address": "--force"},
                None,
                {"address": "t1"},
            ],
            "machines": [{"name": "offline", "state": "unreachable"}],
            "hidden_finished": 2,
        },
    )
    core = Core(str(core_cli.binary))
    result = await core.inventory()
    assert [peer["peer_id"] for peer in result["peers"]] == ["hc:t1"]
    assert result["complete"] is False
    assert set(result["errors"]) == {
        "t2",
        "terminal:2",
        "terminal:3",
        "terminal:4",
        "machine:offline",
    }
    assert result["excluded_finished"] == 2
    assert result["inventory_scope"] == "live"
    core_cli.update(fail="ls")
    assert (await core.peer("t1"))["peer_id"] == "hc:t1"
    assert core_cli.calls()[-1]["args"] == ["get", "t1", "--json"]


@pytest.mark.parametrize("state", ["connecting", "needs_login", "unreachable", "online"])
async def test_unavailable_or_unknown_machine_state_makes_inventory_partial(
    core_cli: CoreCLI, state: str
) -> None:
    core_cli.update(
        inventory={
            "terminals": [],
            "machines": [{"name": "remote", "state": state}],
            "hidden_finished": 0,
        }
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is False
    assert "machine:remote" in result["errors"]


async def test_connected_machine_with_empty_terminal_list_is_complete(core_cli: CoreCLI) -> None:
    core_cli.update(
        inventory={
            "terminals": [],
            "machines": [{"name": "remote", "state": "connected"}],
            "hidden_finished": 0,
        }
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is True
    assert result["peers"] == []


async def test_inventory_never_inspects_terminals_beyond_the_first_hundred(
    core_cli: CoreCLI,
) -> None:
    core_cli.update(
        inventory={
            "terminals": [{"address": "t1"}, *[None] * 99, {"address": "t101"}],
            "machines": [],
            "hidden_finished": 0,
        }
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert [peer["peer_id"] for peer in result["peers"]] == ["hc:t1"]
    assert result["complete"] is False
    assert "100" in result["errors"]["terminals"]
    assert [call["args"][1] for call in core_cli.calls() if call["args"][0] == "get"] == ["t1"]


@pytest.mark.parametrize("raw", ["not JSON", '{"status": NaN}', '{"address":"t1","address":"t2"}'])
async def test_invalid_json_is_not_a_terminal_identity(core_cli: CoreCLI, raw: str) -> None:
    from codex_herdr_bridge.process import BridgeError

    core_cli.update(raw={"get": raw})
    with pytest.raises(BridgeError):
        await Core(str(core_cli.binary)).peer("t1")


async def test_native_read_output_is_bounded(core_cli: CoreCLI) -> None:
    from codex_herdr_bridge.process import BridgeError

    core_cli.update(raw={"read": " " * (4 * 1024 * 1024 + 1)})
    with pytest.raises(BridgeError, match="bound"):
        await Core(str(core_cli.binary)).read("t1")


async def test_inventory_limits_concurrent_native_detail_reads(core_cli: CoreCLI) -> None:
    addresses = [f"t{i}" for i in range(8)]
    core_cli.update(
        inventory={
            "terminals": [{"address": address} for address in addresses],
            "machines": [],
            "hidden_finished": 0,
        },
        terminals={address: terminal(address) for address in addresses},
        delay={"get": 0.15},
        observe_activity=True,
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is True
    assert len(result["peers"]) == 8
    events = [
        json.loads(line)
        for line in core_cli.settings_path.with_name("activity.jsonl").read_text().splitlines()
    ]
    active: set[str] = set()
    high_water = 0
    for event in events:
        if event["event"] == "start":
            active.add(event["target"])
            high_water = max(high_water, len(active))
        else:
            active.remove(event["target"])
    assert 1 < high_water <= 4
    assert not active


async def test_overall_inventory_deadline_retains_completed_details(
    core_cli: CoreCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_herdr_bridge.core as core_module

    monkeypatch.setattr(core_module, "INVENTORY_SECONDS", 0.7)
    core_cli.update(
        inventory={
            "terminals": [{"address": "t1"}, {"address": "t2"}],
            "machines": [],
            "hidden_finished": 0,
        },
        terminals={"t1": terminal(), "t2": terminal("t2")},
        target_delay={"t2": 10},
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is False
    assert "deadline" in result["errors"]["inventory"]
    assert [peer["peer_id"] for peer in result["peers"]] == ["hc:t1"]


@pytest.mark.parametrize(
    "change",
    [
        {"foreground": None},
        {"pid": True},
        {"pid": 0},
        {"created_ms": None},
        {"status": []},
        {"labels": []},
    ],
)
async def test_malformed_details_are_reported_as_partial_inventory(
    core_cli: CoreCLI, change: dict[str, Any]
) -> None:
    core_cli.update(terminals={"t1": terminal() | change})
    result = await Core(str(core_cli.binary)).inventory()
    assert result["peers"] == []
    assert result["complete"] is False
    assert "t1" in result["errors"]


async def test_hidden_finished_terminals_do_not_make_live_inventory_partial(
    core_cli: CoreCLI,
) -> None:
    core_cli.update(
        inventory={"terminals": [{"address": "t1"}], "machines": [], "hidden_finished": 32}
    )
    result = await Core(str(core_cli.binary)).inventory()
    assert result["complete"] is True
    assert result["errors"] == {}
    assert result["inventory_scope"] == "live"
    assert result["excluded_finished"] == 32
    assert [peer["peer_id"] for peer in result["peers"]] == ["hc:t1"]


@pytest.mark.parametrize("target", ["node-two/t3", "t3"])
async def test_reported_agent_without_foreground_details_remains_read_only(
    core_cli: CoreCLI, target: str
) -> None:
    from codex_herdr_bridge.core import CoreNotReady

    row = {
        "address": target,
        "identity": "ghostty",
        "reported": True,
        "state": "live",
        "status": {"app": "kiro-cli", "state": "idle"},
        "cwd": "/home/user",
        "foreground": None,
        "turn": 2,
        "created_ms": 1794568483488,
        "pid": 645531,
    }
    core_cli.update(
        inventory={"terminals": [{"address": target}], "machines": [], "hidden_finished": 0},
        terminals={target: row},
    )
    core = Core(str(core_cli.binary))
    result = await core.inventory()
    assert result["complete"] is True
    assert result["errors"] == {}
    peer = result["peers"][0]
    assert peer["peer_id"] == f"hc:{target}"
    assert peer["agent_kind"] == "kiro"
    assert peer["agent_identity_source"] == "reported_status"
    assert peer["foreground_known"] is False
    assert peer["project"] == ""
    assert peer["project_known"] is False
    assert peer["state"] == "idle"
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "foreground_unknown"
    assert peer["capabilities"] == ["read"]
    assert len(peer["binding"]) == 64
    assert (await core.read(target))["text"] == "Last visible lines\n"
    with pytest.raises(CoreNotReady):
        await core.send(target, "Review", expected=peer)
    assert not any(call["args"][0] == "send" for call in core_cli.calls())


async def test_null_foreground_preserves_reported_done_state_without_completion_claim(
    core_cli: CoreCLI,
) -> None:
    row = terminal(foreground=None, status={"app": "kiro-cli", "state": "done"})
    core_cli.update(terminals={"t1": row})
    peer = await Core(str(core_cli.binary)).peer("t1")
    assert peer["state"] == "done"
    assert peer["foreground_known"] is False
    assert peer["send_ready"] is False
    assert peer["agent_identity_source"] == "reported_status"


@pytest.mark.parametrize("change", [{"pid": 0}, {"created_ms": None}, {"argv": []}])
async def test_reported_agent_still_requires_valid_root_identity(
    core_cli: CoreCLI, change: dict[str, Any]
) -> None:
    from codex_herdr_bridge.process import BridgeError

    row = terminal(foreground=None, status={"app": "kiro-cli", "state": "done"}) | change
    core_cli.update(terminals={"t1": row})
    with pytest.raises(BridgeError):
        await Core(str(core_cli.binary)).peer("t1")


@pytest.mark.parametrize(
    "identity",
    [
        {"address": "t2"},
        {"target": "t2"},
        {"address": "t1", "target": "t2"},
        {"address": "t2", "error": "private-detail"},
    ],
)
async def test_contradictory_receipt_invalidates_identity(
    core_cli: CoreCLI, identity: dict[str, Any]
) -> None:
    from codex_herdr_bridge.core import CoreIdentityChanged

    core_cli.update(
        terminals={"t1": terminal(status={"app": "claude", "state": "idle"})},
        send={"turn": 1, "started": True, "status": "working"} | identity,
    )
    core = Core(str(core_cli.binary))
    with pytest.raises(CoreIdentityChanged, match="uncertain"):
        await core.send("t1", "Review", expected=await core.peer("t1"))
    assert len([call for call in core_cli.calls() if call["args"][0] == "send"]) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["node", "/work/unrelated-server.js"],
        ["python", "/work/unrelated-server.py"],
        ["python3.13", "/work/unrelated-server.py", "claude"],
        ["python3", "-c", "print('claude')"],
        ["node", "--eval", "require('@anthropic-ai/claude-code')"],
        ["node", "/work/server.js", "/known/claude"],
        ["aim", "sandbox", "--client", "claude", "--client", "kiro-cli"],
        ["aim", "shell", "-c", "sandbox --client claude"],
    ],
)
async def test_stale_osc_status_cannot_authorize_unknown_foreground_program(
    core_cli: CoreCLI, argv: list[str]
) -> None:
    from codex_herdr_bridge.core import CoreNotReady

    row = terminal(status={"app": "claude", "state": "idle"})
    row["foreground"]["argv"] = argv
    core_cli.update(terminals={"t1": row})
    core = Core(str(core_cli.binary))
    peer = await core.peer("t1")
    assert peer["agent_kind"] == "claude"
    assert peer["agent_identity_source"] == "reported_status"
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "foreground_agent_unknown"
    assert peer["capabilities"] == ["read"]
    with pytest.raises(CoreNotReady):
        await core.send("t1", "Review", expected=peer)
    assert not any(call["args"][0] == "send" for call in core_cli.calls())


@pytest.mark.parametrize(
    ("argv", "app", "kind"),
    [
        (
            ["/usr/local/bin/aim", "sandbox", "--client", "kiro-cli", "--", "chat"],
            "kiro-cli",
            "kiro",
        ),
        (["/usr/bin/python3", "/tmp/fixture/kiro-cli"], "kiro-cli", "kiro"),
        (
            ["node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
            "claude",
            "claude",
        ),
    ],
)
async def test_known_wrapper_execution_path_supports_guarded_send(
    core_cli: CoreCLI, argv: list[str], app: str, kind: str
) -> None:
    row = terminal(status={"app": app, "state": "idle"})
    row["foreground"]["argv"] = argv
    core_cli.update(terminals={"t1": row})
    core = Core(str(core_cli.binary))
    peer = await core.peer("t1")
    assert peer["agent_kind"] == kind
    assert peer["agent_identity_source"] == "foreground_argv"
    assert peer["send_ready"] is True
    assert (await core.send("t1", "Review", expected=peer))["turn"] == 1


def test_message_validation_counts_utf8_bytes_before_native_operations() -> None:
    from codex_herdr_bridge.core import validate_message

    # Changed in fix round `ans-fix2` (review 2 finding B3), red_at_pretip: the
    # largest accepted text is 131,071 bytes; 131,072 was accepted at `0b4c2707`.
    assert validate_message("é" * (64 * 1024 - 1) + "x") is None
    with pytest.raises(ValueError, match="128 KiB"):
        validate_message("é" * (64 * 1024))
    with pytest.raises(ValueError, match="128 KiB"):
        validate_message("é" * (64 * 1024) + "x")


@pytest.mark.parametrize("text", ["", " \n\t", "text\x00more", "\ud800", None])
def test_shared_message_validator_rejects_invalid_messages(text: Any) -> None:
    from codex_herdr_bridge.core import validate_message

    with pytest.raises(ValueError):
        validate_message(text)
