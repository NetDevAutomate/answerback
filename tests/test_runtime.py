"""Exercise installed command boundaries with real IPC and controlled providers."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import launchd_live_opt_in

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.tasks import TaskStore
from codex_herdr_bridge.transport import RemoteBridge, TransportUnavailable


@pytest.fixture
def runtime_config() -> Iterator[Path]:
    # macOS Unix sockets have a short pathname limit, independent of PATH_MAX.
    with tempfile.TemporaryDirectory(prefix="hb-", dir="/tmp") as directory:
        root = Path(directory)
        fake = root / "provider"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json,sys,pathlib\n"
            "root=pathlib.Path(__file__).parent\n"
            "args=sys.argv[1:]\n"
            "if '--version' in args: print('test-provider 1.0')\n"
            "elif '--help' in args: print('agent prompt queue --thread')\n"
            "elif 'app-server' in args:\n"
            "    for line in sys.stdin:\n"
            "        req=json.loads(line); method=req['method']\n"
            "        assert method in ['initialize','initialized','thread/read',"
            "'thread/turns/list','thread/items/list','thread/list']\n"
            "        if 'id' not in req: continue\n"
            "        result={}\n"
            "        summary={'id':req.get('params',{}).get('threadId',"
            "'11111111-2222-3333-4444-555555555555'), 'name':'Fixture Desktop',"
            " 'cwd':str(root), 'source':'vscode', 'ephemeral':False}\n"
            "        if method == 'thread/list': result={'data':[summary],'nextCursor':None}\n"
            "        if method == 'thread/read': result={'thread':summary}\n"
            "        if method == 'thread/turns/list':\n"
            "            q=root/'queue.json'\n"
            "            data=[]\n"
            "            if q.exists():\n"
            "                data=[{'id':'turn-fixture','status':'completed',"
            "'startedAt':4000000000,'completedAt':4000000001,'items':[\n"
            "                    {'type':'userMessage','content':["
            "{'type':'text','text':q.read_text()}]},\n"
            "                    {'type':'agentMessage','phase':'final_answer',"
            "'text':'DESKTOP_RESULT'}]}]\n"
            "                state=root/'turn-state.json'\n"
            "                if state.exists():\n"
            "                    data[0].update(json.loads(state.read_text()))\n"
            "                    if data[0]['status'] != 'completed':\n"
            "                        data[0]['items']=data[0]['items'][:1]\n"
            "            result={'data':data,'nextCursor':None}\n"
            "        print(json.dumps({'id':req['id'],'result':result}),flush=True)\n"
            "elif 'queue' in args:\n"
            "    (root/'queue.json').write_text(args[args.index('--message')+1])\n"
            "    with (root/'sends').open('a') as f: f.write('desktop\\n')\n"
            "    print('Queued message fixture for thread '+args[args.index('--thread')+1])\n"
            "elif 'prompt' in args:\n"
            "    with (root/'sends').open('a') as f: f.write('herdr\\n')\n"
            "    print(json.dumps({'result':{'agent':{'terminal_id':'terminal-fixture'}}}))\n"
            "elif 'get' in args:\n"
            "    occupant=root/'occupant'\n"
            "    terminal=occupant.read_text() if occupant.exists() else 'terminal-fixture'\n"
            "    print(json.dumps({'result':{'agent':{"
            "'terminal_id':terminal,'agent_status':'idle'}}}))\n"
            "elif 'list' in args: print(json.dumps({'result':{'agents':[{'name':'worker',"
            "'terminal_id':'terminal-fixture','agent_status':'idle'}]}}))\n"
            "else: raise SystemExit(2)\n"
        )
        fake.chmod(0o700)
        config_path = root / "config.json"
        Config(
            "fixture",
            str(fake),
            str(fake),
            {"desk": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            codex_enabled=True,
        ).save(config_path)
        yield config_path
        cli(config_path, "stop", check=False)


def cli(config: Path, *args: str, check: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "codex_herdr_bridge", "--config", str(config), *args],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if check:
        assert result.returncode == 0, result.stderr
    try:
        return json.loads(result.stdout or result.stderr)
    except json.JSONDecodeError:
        if check:
            raise
        return {"error": result.stderr}


def test_start_is_idempotent_and_socket_is_private(runtime_config: Path) -> None:
    first = cli(runtime_config, "start", "--standalone")
    second = cli(runtime_config, "start", "--standalone")
    assert first["instance_id"] == second["instance_id"]
    status = cli(runtime_config, "status", "--json")
    assert status["running"] is True
    assert Path(status["socket"]).stat().st_mode & 0o777 == 0o600
    assert Path(status["socket"]).parent.stat().st_mode & 0o777 == 0o700
    # The loop-lag guard runs from the start and has seen no block on an idle service.
    assert status["loop"] == {
        "lag_events": 0,
        "max_lag_seconds": 0.0,
        "last_lag_seconds": None,
        "last_lag_at": None,
    }
    stopped = cli(runtime_config, "stop")
    assert stopped["running"] is False
    assert cli(runtime_config, "stop")["running"] is False


def test_receipts_survive_reset_restart_and_duplicate_clients(runtime_config: Path) -> None:
    cli(runtime_config, "start", "--standalone")
    binding = cli(runtime_config, "find", "worker", "--provider", "herdr")["peers"][0]["binding"]
    receipt = cli(
        runtime_config,
        "send-herdr",
        "worker",
        "--message",
        "PRIVATE_PROMPT",
        "--request-id",
        "request-one",
        "--binding",
        binding,
    )
    reset = cli(runtime_config, "reset", "--connections")
    assert reset["receipts_retained"] is True
    cli(runtime_config, "restart", "--standalone")
    duplicate = cli(
        runtime_config,
        "send-herdr",
        "worker",
        "--message",
        "PRIVATE_PROMPT",
        "--request-id",
        "request-one",
        "--binding",
        binding,
    )
    assert duplicate["id"] == receipt["id"]
    assert (runtime_config.parent / "sends").read_text() == "herdr\n"
    cli(runtime_config, "complete", receipt["id"], "--result", "PRIVATE_RESULT")
    cli(runtime_config, "restart", "--standalone")
    assert cli(runtime_config, "task", receipt["id"])["result"] == "PRIVATE_RESULT"
    logs = cli(runtime_config, "logs", "--json")
    assert logs["entries"]
    assert "PRIVATE_PROMPT" not in json.dumps(logs)
    assert "PRIVATE_RESULT" not in json.dumps(logs)


def test_crash_recovery_marks_reservations_uncertain_without_resending(
    runtime_config: Path,
) -> None:
    store = TaskStore(runtime_config.parent / "tasks.sqlite")
    task, _ = store.reserve("herdr:fixture:orphan", "Possibly sent", "orphan", {"kind": "herdr"})
    first = cli(runtime_config, "start", "--standalone")
    assert cli(runtime_config, "task", task["id"], check=False)["status"] == "uncertain"
    os.kill(first["pid"], signal.SIGKILL)
    for _ in range(50):
        if cli(runtime_config, "status", "--json")["running"] is False:
            break
        time.sleep(0.05)
    second = cli(runtime_config, "start", "--standalone")
    assert second["instance_id"] != first["instance_id"]
    assert cli(runtime_config, "task", task["id"], check=False)["status"] == "uncertain"
    assert not (runtime_config.parent / "sends").exists()


def test_desktop_submission_is_correlated_through_the_service(runtime_config: Path) -> None:
    cli(runtime_config, "start", "--standalone")
    receipt = cli(
        runtime_config,
        "send-desktop",
        "desk",
        "--message",
        "Return a result",
        "--request-id",
        "desktop-one",
    )
    completed = cli(runtime_config, "task", receipt["id"])
    assert completed["status"] == "completed"
    assert completed["result"] == "DESKTOP_RESULT"
    assert completed["turn_id"] == "turn-fixture"
    duplicate = cli(
        runtime_config,
        "send-desktop",
        "desk",
        "--message",
        "Return a result",
        "--request-id",
        "desktop-one",
    )
    assert duplicate["id"] == receipt["id"]
    assert (runtime_config.parent / "sends").read_text() == "desktop\n"


def test_desktop_cold_history_interruption_then_completion_over_real_ipc(
    runtime_config: Path,
) -> None:
    state = runtime_config.parent / "turn-state.json"
    state.write_text(
        json.dumps({"status": "interrupted", "startedAt": 4_000_000_000, "completedAt": None})
    )
    cli(runtime_config, "start", "--standalone")
    receipt = cli(
        runtime_config, "send-desktop", "desk", "--message", "Ping", "--request-id", "cold-history"
    )
    first = cli(runtime_config, "task", receipt["id"])
    assert first["status"] == "queued"
    assert first["result"] is None
    state.write_text(
        json.dumps(
            {"status": "completed", "startedAt": 4_000_000_000, "completedAt": 4_000_000_005}
        )
    )
    final = cli(runtime_config, "task", receipt["id"])
    assert final["status"] == "completed"
    assert final["result"] == "DESKTOP_RESULT"
    assert (runtime_config.parent / "sends").read_text() == "desktop\n"


def test_explicit_desktop_reconciliation_over_real_ipc_never_resends(
    runtime_config: Path,
) -> None:
    store = TaskStore(runtime_config.parent / "tasks.sqlite")
    task, _ = store.reserve(
        "desktop:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "Ping",
        "old-failure",
        {
            "kind": "desktop",
            "binding": "discovered",
            "thread_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
    )
    before = store.update(
        task["id"], "failed", result="Codex turn interrupted", turn_id="turn-fixture"
    )
    (runtime_config.parent / "queue.json").write_text(f"[codex-herdr-task:{task['id']}]\nPing")
    (runtime_config.parent / "turn-state.json").write_text(
        json.dumps(
            {"status": "completed", "startedAt": 4_000_000_000, "completedAt": 4_000_000_005}
        )
    )
    cli(runtime_config, "start", "--standalone")
    final = cli(runtime_config, "task", task["id"], "--reconcile")
    assert final["status"] == "completed"
    assert final["result"] == "DESKTOP_RESULT"
    assert final["metadata"]["desktop_reconciliation"]["previous_updated"] == before["updated"]
    assert cli(runtime_config, "task", task["id"], "--reconcile") == final
    assert not (runtime_config.parent / "sends").exists()


def test_bare_reset_cannot_delete_history(runtime_config: Path) -> None:
    cli(runtime_config, "start", "--standalone")
    result = subprocess.run(
        [sys.executable, "-m", "codex_herdr_bridge", "--config", str(runtime_config), "reset"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--connections" in result.stderr
    assert cli(runtime_config, "status", "--json")["running"] is True


def test_simultaneous_starts_and_sends_have_one_owner(runtime_config: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = list(pool.map(lambda _: cli(runtime_config, "start", "--standalone"), range(2)))
        assert starts[0]["instance_id"] == starts[1]["instance_id"]
        binding = cli(runtime_config, "find", "worker", "--provider", "herdr")["peers"][0][
            "binding"
        ]
        receipts = list(
            pool.map(
                lambda _: cli(
                    runtime_config,
                    "send-herdr",
                    "worker",
                    "--message",
                    "Once",
                    "--request-id",
                    "concurrent",
                    "--binding",
                    binding,
                ),
                range(2),
            )
        )
    assert receipts[0]["id"] == receipts[1]["id"]
    assert (runtime_config.parent / "sends").read_text() == "herdr\n"


def test_stop_during_submission_preserves_uncertainty(runtime_config: Path) -> None:
    provider = runtime_config.parent / "provider"
    provider.write_text(
        provider.read_text().replace(
            "elif 'prompt' in args:",
            "elif 'prompt' in args:\n    import time\n"
            "    (root/'started').write_text('yes')\n    time.sleep(60)",
        )
    )
    cli(runtime_config, "start", "--standalone")
    binding = cli(runtime_config, "find", "worker", "--provider", "herdr")["peers"][0]["binding"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            cli,
            runtime_config,
            "send-herdr",
            "worker",
            "--message",
            "Possibly delivered",
            "--request-id",
            "in-flight",
            "--binding",
            binding,
            check=False,
        )
        for _ in range(100):
            if (runtime_config.parent / "started").exists():
                break
            time.sleep(0.05)
        assert (runtime_config.parent / "started").exists()
        cli(runtime_config, "stop")
        assert "error" in request.result()
    cli(runtime_config, "start", "--standalone")
    receipt = cli(
        runtime_config,
        "send-herdr",
        "worker",
        "--message",
        "Possibly delivered",
        "--request-id",
        "in-flight",
        "--binding",
        binding,
        check=False,
    )
    assert receipt["status"] == "uncertain"
    assert not (runtime_config.parent / "sends").exists()


async def test_mcp_and_cli_share_the_same_service_receipt(runtime_config: Path) -> None:
    from mcp import Client

    from codex_herdr_bridge.mcp_server import create_server
    from codex_herdr_bridge.transport import RemoteBridge

    cli(runtime_config, "start", "--standalone")
    async with Client(create_server(RemoteBridge(runtime_config))) as client:
        tools = await client.list_tools()
        assert "herdr_submit" not in {tool.name for tool in tools.tools}
        found = await client.call_tool("peer_search", {"query": "worker", "provider": "herdr"})
        receipt = await client.call_tool(
            "peer_submit",
            {
                "selector": "worker",
                "message": "Review",
                "request_id": "from-mcp",
                "provider": "herdr",
                "binding": found.structured_content["peers"][0]["binding"],
            },
        )
        task = receipt.structured_content
        cli(runtime_config, "complete", task["id"], "--result", "CLI_RESULT")
        read = await client.call_tool("task_get", {"task_id": task["id"]})
        assert read.structured_content["result"] == "CLI_RESULT"
        duplicate = cli(
            runtime_config,
            "send-herdr",
            "worker",
            "--message",
            "Review",
            "--request-id",
            "from-mcp",
            "--binding",
            found.structured_content["peers"][0]["binding"],
        )
        assert duplicate["id"] == task["id"]
    assert (runtime_config.parent / "sends").read_text() == "herdr\n"


def test_send_herdr_is_refused_when_the_target_terminal_changed_before_delivery(
    runtime_config: Path,
) -> None:
    cli(runtime_config, "start", "--standalone")
    binding = cli(runtime_config, "find", "worker", "--provider", "herdr")["peers"][0]["binding"]
    delivered = cli(
        runtime_config,
        "send-herdr",
        "worker",
        "--message",
        "Review",
        "--request-id",
        "same-terminal",
        "--binding",
        binding,
    )
    assert delivered["status"] == "submitted"
    # The agent list (discovery) still names terminal-fixture, but the live occupant that
    # answers `agent get` is now another terminal: refused before the prompt, nothing sent.
    (runtime_config.parent / "occupant").write_text("terminal-replaced")
    refused = cli(
        runtime_config,
        "send-herdr",
        "worker",
        "--message",
        "Review",
        "--request-id",
        "replaced-terminal",
        "--binding",
        binding,
        check=False,
    )
    assert "error" in refused
    assert (runtime_config.parent / "sends").read_text() == "herdr\n"
    submissions = [
        entry
        for entry in cli(runtime_config, "logs", "--json")["entries"]
        if entry.get("event") == "request" and entry.get("operation", "").endswith("_submit")
    ]
    # Both sends travelled the discovery-bound path, not a provider-specific submit method.
    assert [entry["operation"] for entry in submissions] == ["peer_submit", "peer_submit"]
    assert [entry["outcome"] for entry in submissions] == ["ok", "error"]


async def test_invalid_requests_cannot_dispatch_commands(runtime_config: Path) -> None:
    import asyncio

    cli(runtime_config, "start", "--standalone")
    status = cli(runtime_config, "status")
    config_identity = str(await asyncio.to_thread(runtime_config.resolve))
    for request in [
        {"version": 1, "method": "shell", "params": {"command": "private-body"}},
        {"version": 99, "method": "stop"},
        {"version": 1, "method": "stop", "params": {"extra": True}},
        {"version": 1, "method": "task_complete", "params": []},
    ]:
        request["config_path"] = config_identity
        reader, writer = await asyncio.open_unix_connection(status["socket"])
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert reply["ok"] is False
    assert cli(runtime_config, "status")["running"] is True
    assert not (runtime_config.parent / "sends").exists()
    assert "private-body" not in json.dumps(cli(runtime_config, "logs"))


def test_sibling_config_cannot_route_to_another_session(runtime_config: Path) -> None:
    cli(runtime_config, "start", "--standalone")
    other = runtime_config.with_name("other.json")
    Config("other-session").save(other)
    for command in ("status", "stop", "start"):
        result = cli(other, command, check=False)
        assert "error" in result
        assert "configuration directory" in result["error"]
    assert cli(runtime_config, "status")["running"] is True
    cli(runtime_config, "stop")
    result = cli(other, "start", "--standalone", check=False)
    assert "error" in result
    assert "configuration directory" in result["error"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual launchd requires macOS")
@pytest.mark.skipif(
    not launchd_live_opt_in(os.environ),
    reason="runs a real launchctl bootstrap; set ANSWERBACK_LIVE_LAUNCHD=1 to opt in "
    "(see docs/TESTING-HARNESSES.md)",
)
def test_native_launchd_start_restart_and_stop(runtime_config: Path) -> None:
    first = cli(runtime_config, "start")
    assert cli(runtime_config, "start")["instance_id"] == first["instance_id"]
    restarted = cli(runtime_config, "restart")
    assert restarted["instance_id"] != first["instance_id"]
    assert cli(runtime_config, "stop")["running"] is False


def test_discovered_desktop_queue_and_result_survive_restart_without_pairing(
    runtime_config: Path,
) -> None:
    cli(runtime_config, "start", "--standalone")
    found = cli(runtime_config, "find", "Fixture Desktop", "--provider", "desktop")
    peer = next(p for p in found["peers"] if p["kind"] == "discovered_codex")
    sent = cli(
        runtime_config,
        "send",
        peer["peer_id"],
        "--message",
        "Discover me",
        "--request-id",
        "discovered",
    )
    assert sent["status"] == "queued"
    assert cli(runtime_config, "task", sent["id"], "--wait", "2")["result"] == "DESKTOP_RESULT"
    cli(runtime_config, "restart", "--standalone")
    assert cli(runtime_config, "task", "--peer", peer["peer_id"])["id"] == sent["id"]
    assert list(Config.load(runtime_config).desktop_threads) == ["desk"]


async def test_remote_bridge_unavailable_error_carries_the_resolved_config_path() -> None:
    # No server is started for this config path: open_unix_connection against
    # the nonexistent socket raises FileNotFoundError, the exact case
    # transport.py's "Bridge is stopped" branch exists to translate.
    # macOS Unix sockets have a short pathname limit (same reason
    # runtime_config above roots under /tmp, not pytest's own tmp_path).
    with tempfile.TemporaryDirectory(prefix="hb-stopped-", dir="/tmp") as folder:
        config_path = Path(folder) / "config.json"
        remote = RemoteBridge(config_path)
        with pytest.raises(TransportUnavailable) as raised:
            await remote.call("status", {})
        assert str(raised.value) == "Bridge is stopped. Run answerback start first."
        assert raised.value.config_path == remote.config_path == config_path.resolve()
