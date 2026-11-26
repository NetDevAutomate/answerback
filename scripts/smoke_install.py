"""Opt-in packaging smoke using disposable providers and isolated uv tool directories."""

import argparse
import asyncio
import json
import os
import runpy
import shutil
import socket
import subprocess
import tarfile
import tomllib
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from package_smoke_helpers import (
    broker_config_argument,
    importable,
    isolated_environment,
    search_then_submit,
    tool_answer,
    wheel_identity,
)
from playwright.sync_api import expect, sync_playwright

PROJECT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]["version"]
options = argparse.ArgumentParser()
options.add_argument(
    "--upgrade-from", type=Path, help="Also verify a prior installed wheel upgrade"
)
options.add_argument(
    "--config-only",
    action="store_true",
    help="Load the fixture configuration, print its folder, remove it and exit",
)
arguments = options.parse_args()
# tests/test_runtime.py imports its sibling conftest, which only pytest puts on the path.
with importable(PROJECT / "tests"):
    fixtures = runpy.run_path(str(PROJECT / "tests/test_runtime.py"))
fixture = fixtures["runtime_config"].__wrapped__()
config = next(fixture)
root = config.parent
if arguments.config_only:
    try:
        print(root)
    finally:
        fixture.close()
    raise SystemExit(0)
env = {
    **isolated_environment(root, dict(os.environ)),
    "UV_TOOL_DIR": str(root / "uv-tools"),
    "UV_TOOL_BIN_DIR": str(root / "bin"),
    "CODEX_HOME": str(root / "codex"),
}


def command(*args: str) -> str:
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=120, cwd=root)
    if result.returncode:
        raise AssertionError(result.stderr + result.stdout)
    return result.stdout


def installed_web_roundtrip(executable: str) -> None:
    process = subprocess.Popen(
        [executable, "--config", str(config), "bus", "monitor", "--web", "--no-open"],
        env=env,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        import selectors

        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(10), "Installed dashboard did not print its URL"
        line = process.stdout.readline()
        assert line.startswith("Answerback monitor: "), line
        url = line.removeprefix("Answerback monitor: ").strip()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                failures = []
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.goto(url)
                expect(page.get_by_role("heading", name="Answerback", exact=True)).to_be_visible()
                expect(page.get_by_test_id("commands-count")).not_to_have_text("—")
                expect(page.get_by_test_id("event-row").first).to_be_visible()
                page.get_by_role("button", name="Debug").click()
                expect(page.locator("#diagnostics")).to_contain_text("ledger_available")
                icon = page.request.get(url.partition("/#")[0] + "/favicon.ico")
                assert icon.status == 200 and icon.body().startswith(b"\x00\x00\x01\x00")
                assert not failures, failures
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def mcp_roundtrip(executable: str) -> None:
    params = StdioServerParameters(
        command=executable, args=["--config", str(config), "mcp"], env=env
    )
    async with Client(params) as client:
        # A Herdr peer carries a discovery binding, and the bridge refuses a submit without it.
        sent = await search_then_submit(
            client,
            search={"provider": "herdr", "query": "worker"},
            chosen=lambda found: found.get("name") == "worker",
            submit={
                "selector": "worker",
                "message": "Packaging round trip",
                "request_id": "package-test",
                "provider": "herdr",
            },
        )
        task_id = sent["id"]
        command(executable, "--config", str(config), "complete", task_id, "--result", "PACKAGE_OK")
        read = tool_answer("task_get", await client.call_tool("task_get", {"task_id": task_id}))
        assert read["result"] == "PACKAGE_OK", read
        # A discovered Desktop chat has no binding; it is selected by the peer_id of the search.
        sent = await search_then_submit(
            client,
            search={"provider": "desktop", "query": "Fixture Desktop"},
            chosen=lambda found: found.get("kind") == "discovered_codex",
            submit={
                "message": "Discovered packaging round trip",
                "request_id": "package-desktop",
            },
        )
        receipt = json.loads(
            command(
                executable,
                "--config",
                str(config),
                "task",
                sent["id"],
                "--wait",
                "2",
            )
        )
        assert receipt["result"] == "DESKTOP_RESULT"


async def installed_bus_roundtrip(executable: str) -> None:
    """Installed MCP → private NATS → coordinator → native fixture → correlated result."""
    binary = shutil.which("nats-server")
    assert binary, "Install nats-server for the installed end-to-end check"
    broker = await asyncio.create_subprocess_exec(
        binary,
        "-c",
        broker_config_argument(config),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    params = StdioServerParameters(
        command=executable, args=["--config", str(config), "mcp"], env=env
    )
    before = (root / "sends").read_text().splitlines()
    turn_state = root / "turn-state.json"
    turn_state.write_text(json.dumps({"status": "inProgress"}))
    try:
        async with Client(params) as client:
            async with asyncio.timeout(20):
                while True:
                    status = tool_answer("bus_status", await client.call_tool("bus_status", {}))
                    if status["coordinator"]["connected"]:
                        break
                    await asyncio.sleep(0.1)
            arguments = {
                "peer_id": "desktop:desk",
                "project": str(root),
                "message": "Installed adapter end-to-end check",
                "request_id": "installed-adapter-e2e",
            }
            sent = tool_answer("bus_submit", await client.call_tool("bus_submit", arguments))
            async with asyncio.timeout(20):
                while True:
                    current = tool_answer(
                        "bus_result",
                        await client.call_tool("bus_result", {"command_id": sent["command_id"]}),
                    )
                    if current["task"] is not None:
                        break
                    assert current["delivery"] not in ("rejected", "uncertain"), current
                    await asyncio.sleep(0.1)
            task_id = current["task_id"]
            assert current["task"]["status"] == "queued", current
            await asyncio.to_thread(
                command, executable, "--config", str(config), "restart", "--standalone"
            )
            turn_state.unlink()
            async with asyncio.timeout(25):
                while True:
                    current = tool_answer(
                        "bus_result",
                        await client.call_tool("bus_result", {"command_id": sent["command_id"]}),
                    )
                    if current["task"]["status"] == "completed":
                        break
                    await asyncio.sleep(0.1)
            assert current["task_id"] == task_id
            assert current["task"]["result"] == "DESKTOP_RESULT"
            retried = tool_answer("bus_submit", await client.call_tool("bus_submit", arguments))
            assert retried["command_id"] == sent["command_id"]
            assert (root / "sends").read_text().splitlines() == [*before, "desktop"]
    finally:
        turn_state.unlink(missing_ok=True)
        if broker.returncode is None:
            broker.terminate()
            try:
                await asyncio.wait_for(broker.wait(), 5)
            except TimeoutError:
                broker.kill()
                await broker.wait()


try:
    previous_receipt = None
    if arguments.upgrade_from:
        previous_package = wheel_identity(arguments.upgrade_from.resolve())
        command("uv", "tool", "install", previous_package["path"])
        distribution = previous_package["name"].lower().replace("_", "-")
        old_python = str(root / "uv-tools" / distribution / "bin/python")
        previous_receipt = command(
            old_python,
            "-c",
            "import json,sys; from pathlib import Path; "
            "from codex_herdr_bridge.tasks import TaskStore; "
            "s=TaskStore(Path(sys.argv[1])); "
            "t,_=s.reserve('kiro:historical','Retained work','upgrade-sentinel',{'kind':'kiro'}); "
            "print(json.dumps(s.update(t['id'],'completed',result='Retained answer'),"
            "sort_keys=True))",
            str(root / "tasks.sqlite"),
        )
        command("uv", "tool", "uninstall", distribution)
        assert not (root / "bin/herdr-bridge").exists()
    archive_path = PROJECT / f"dist/answerback-{VERSION}.tar.gz"
    with tarfile.open(archive_path) as archive:
        archive.extractall(root / "source", filter="data")
    source = root / f"source/answerback-{VERSION}"
    codex_config = root / "codex/config.toml"
    codex_config.parent.mkdir()
    original = (
        '# Preserved user comment\nmodel = "existing"\n[mcp_servers.other]\ncommand="other"\n'
    )
    codex_config.write_text(original)
    install = [
        str(source / "install.sh"),
        "--session",
        "fixture",
        "--herdr",
        str(root / "provider"),
        "--codex",
        str(root / "provider"),
        "--bridge-config",
        str(config),
        "--codex-home",
        str(codex_config.parent),
    ]
    command(*install)
    first_config = codex_config.read_text()
    command(*install)
    assert codex_config.read_text() == first_config
    assert "# Preserved user comment" in first_config
    assert "[mcp_servers.other]" in first_config
    assert "[mcp_servers.answerback_bridge]" in first_config, first_config
    executable = str(root / "bin/answerback")
    assert "start" in command(str(root / "bin/herdr-bridge"), "--help")
    assert "start" in command(executable, "--help")
    assert "start" in command(str(root / "bin/answerback-bridge"), "--help")
    assert "start" in command(str(root / "bin/codex-herdr"), "--help")
    command(executable, "--config", str(config), "start", "--standalone")
    asyncio.run(mcp_roundtrip(executable))
    command(executable, "--config", str(config), "uninstall", "--remove-tool")
    assert not Path(executable).exists()
    assert "answerback_bridge" not in codex_config.read_text()
    assert "codex_herdr_bridge" not in codex_config.read_text()
    assert "existing" in codex_config.read_text()
    assert (root / "tasks.sqlite").exists()
    # Separately prove the wheel works, without an editable source install.
    # The bus round trip below needs the optional `bus` extra (the NATS client and psutil):
    # without it the installed bridge's coordinator never connects to the smoke's broker.
    wheel = PROJECT / f"dist/answerback-{VERSION}-py3-none-any.whl"
    command("uv", "tool", "install", f"{wheel}[bus]")
    neutral = str(root / "bin/agent-bus")
    assert "submit" in command(neutral, "--help")
    assert json.loads(command(neutral, "--config", str(config), "status")) == {"configured": False}
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        bus_port = reserved.getsockname()[1]
    command(neutral, "--config", str(config), "configure", "--port", str(bus_port))
    queued = json.loads(
        command(
            neutral,
            "--config",
            str(config),
            "submit",
            "desktop:desk",
            "--project",
            str(root),
            "--message",
            "Installed bus packaging check",
            "--request-id",
            "package-bus",
            "--offline",
        )
    )
    assert queued["stored_locally"] and queued["delivery"] == "queued"
    receipt = json.loads(command(neutral, "--config", str(config), "result", queued["command_id"]))
    assert receipt["task"] is None
    cancelled = json.loads(
        command(neutral, "--config", str(config), "cancel", queued["command_id"])
    )
    assert cancelled["delivery"] == "cancelled"
    assert "start" in command(executable, "--help")
    command(executable, "--config", str(config), "start", "--standalone")
    assert json.loads(command(executable, "--config", str(config), "status"))["running"]
    asyncio.run(installed_bus_roundtrip(executable))
    command(executable, "--config", str(config), "stop")
    assert "live-test" in command(executable, "--help")
    # The wheel must carry the full skill and install non-Codex clients without a checkout.
    installed_python = str(root / "uv-tools/answerback/bin/python")
    namespace_check = """
import importlib
from importlib.resources import files
for suffix in ['', '.tasks', '.errors', '.process', '.bus.cli', '.adapters.contracts']:
    assert importlib.import_module('answerback_bridge' + suffix) is (
        importlib.import_module('codex_herdr_bridge' + suffix)
    ), suffix
for package in ['answerback_bridge', 'codex_herdr_bridge']:
    resource = files(package)
    icon = resource.joinpath('bus/assets/favicon.ico').read_bytes()
    assert icon.startswith(b'\\x00\\x00\\x01\\x00')
    assert resource.joinpath('data/skills/herdr-collaboration/SKILL.md').is_file()
"""
    command(installed_python, "-c", namespace_check)
    assert VERSION in command(installed_python, "-m", "answerback_bridge", "--version")
    assert VERSION in command(installed_python, "-m", "codex_herdr_bridge", "--version")
    assert "submit" in command(installed_python, "-m", "answerback_bridge.bus.cli", "--help")
    assert "submit" in command(installed_python, "-m", "codex_herdr_bridge.bus.cli", "--help")
    if previous_receipt:
        retained = command(
            installed_python,
            "-c",
            "import json,sys; from pathlib import Path; "
            "from codex_herdr_bridge.tasks import TaskStore; "
            "print(json.dumps(TaskStore(Path(sys.argv[1])).by_request('upgrade-sentinel'),sort_keys=True))",
            str(root / "tasks.sqlite"),
        )
        assert retained == previous_receipt
    installed_web_roundtrip(executable)
    setup_code = """
import asyncio, json, sys
from pathlib import Path
from codex_herdr_bridge.registration import install_clients, install_skill
async def main():
    home = Path(sys.argv[1]).resolve()
    clients = await install_clients(Path(sys.argv[3]), ['claude','grok','kiro','crew','codex'],
                                   home=home, executable=sys.argv[2], apply=True)
    assert clients['success'], clients
    skill = await install_skill(['claude','grok','kiro','crew','codex'], home=home, apply=True)
    assert skill['success'], skill
    assert 'site-packages' in skill['source'], skill
    assert (home/'.agents/skills/answerback-collaboration/references/bridge.md').is_file()
    setup = home/'.agents/skills/answerback-fleet'
    for name in ('SKILL.md', 'scripts/fleet.py', '.answerback-fleet.json'):
        assert (setup/name).is_file(), name
    assert sorted(skill['sources']) == ['answerback-collaboration', 'answerback-fleet'], skill
    assert 'site-packages' in skill['sources']['answerback-fleet'], skill
    assert (home/'.claude.json').is_file()
    print(json.dumps({'packaged_skill': True, 'client_registrations': True}))
asyncio.run(main())
"""
    installed_setup = json.loads(
        command(
            installed_python, "-c", setup_code, str(root / "client-home"), executable, str(config)
        )
    )
    assert installed_setup == {"packaged_skill": True, "client_registrations": True}
    simulation = json.loads(command(executable, "self-test"))
    assert simulation["success"], simulation
    command("uv", "tool", "uninstall", "answerback")
    print(
        json.dumps(
            {
                "source_installer_twice": "passed",
                "configuration_preservation": "passed",
                "installed_stdio_mcp_to_service_to_cli_result": "passed",
                "uninstall_retains_receipts": "passed",
                "wheel_install_and_service_lifecycle": "passed",
                "installed_wheel_bidirectional_harness": "passed",
                "packaged_skill_and_cross_client_installation": "passed",
                "neutral_bus_cli_offline_receipts_and_cancel": "passed",
                "installed_mcp_nats_adapter_native_result_and_restart": "passed",
                "installed_web_assets_browser_and_favicon": "passed",
                "canonical_and_legacy_namespace_resource_and_module_entries": "passed",
                "prior_distribution_upgrade_retains_receipt": (
                    "passed" if previous_receipt else "not requested"
                ),
                "temporary_resources": "removed",
            },
            indent=2,
        )
    )
finally:
    fixtures["cli"](config, "stop", check=False)
    fixture.close()
