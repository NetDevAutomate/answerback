"""Opt-in native core smoke: isolated core, scripted agent, real MCP and receipts."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from answerback_bridge.config import Config
from answerback_bridge.process import run

WORKER = r"""
import asyncio, re, sys
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters
config = sys.argv[1]
count = Path(sys.argv[2])
def status(value):
    sys.stdout.write("\x1b]7501;state=" + value + ":app=claude-code\x1b\\")
    sys.stdout.flush()
async def answer(task_id, message):
    async with Client(StdioServerParameters(command=sys.executable,
            args=["-m","answerback_bridge","--config",config,"mcp"])) as client:
        peers = await client.call_tool("peer_search", {"provider":"hc"})
        assert peers.structured_content["peers"]
        result = "CORE_REPLY " + message
        receipt = await client.call_tool("task_complete", {"task_id":task_id,"result":result})
        assert receipt.structured_content["status"] in ("pending", "completed")
    with count.open("a") as stream:
        stream.write(task_id + "\n")
status("idle")
for line in sys.stdin:
    found = re.search(r"Bridge task: ([0-9a-f-]{36})", line)
    if found:
        message = sys.stdin.readline().strip()
        status("working")
        asyncio.run(answer(found.group(1), message))
        status("idle")
"""


async def smoke(root: Path) -> dict[str, object]:
    hc = shutil.which("hc")
    if not hc:
        raise RuntimeError("Install hc before running this opt-in native test")
    session = "smoke-" + uuid.uuid4().hex[:8]
    env = {
        **os.environ,
        "HC_SESSION": session,
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    for key in ("HC_SOCKET", "HC_TERMINAL_ID"):
        env.pop(key, None)
    config = root / "bridge" / "config.json"
    Config(hc_enabled=True, hc_binary=hc, hc_session=session, codex_enabled=False).save(config)
    worker = root / "claude"
    worker.write_text(f"#!{sys.executable}\n" + WORKER)
    worker.chmod(0o700)
    counter = root / "received.txt"
    address = None
    prefix = [sys.executable, "-m", "answerback_bridge", "--config", str(config)]
    core_started = False
    try:
        # These explicit XDG roots + session keep the test away from the user's core.
        core_started = True
        launched = json.loads(
            (
                await run(
                    [
                        hc,
                        "run",
                        "--bg",
                        "--json",
                        "--no-parent",
                        "--no-notify",
                        "--cwd",
                        str(root),
                        str(worker),
                        str(config),
                        str(counter),
                    ],
                    env=env,
                    max_seconds=30,
                )
            ).stdout
        )
        address = launched["address"]
        print("Native fixture started", flush=True)
        await run([hc, "wait", address, "--timeout", "10"], env=env, max_seconds=15)
        await run([*prefix, "start", "--standalone"], env=env, max_seconds=30)
        nonce = "probe-" + uuid.uuid4().hex
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "answerback_bridge", "--config", str(config), "mcp"],
                env=env,
            )
        ) as client:
            found = (await client.call_tool("peer_search", {"provider": "hc"})).structured_content
            peer = next(p for p in found["peers"] if p["target"] == address)
            assert peer["agent_kind"] == "claude" and peer["send_ready"], peer
            sent = (
                await client.call_tool(
                    "peer_submit",
                    {"selector": peer["peer_id"], "message": nonce, "request_id": nonce},
                )
            ).structured_content
            result = (
                await client.call_tool("task_wait", {"task_id": sent["id"], "seconds": 30})
            ).structured_content
            assert result["status"] == "completed", result
            assert result["result"] == "CORE_REPLY " + nonce, result
            again = (
                await client.call_tool(
                    "peer_submit",
                    {"selector": peer["peer_id"], "message": nonce, "request_id": nonce},
                )
            ).structured_content
            assert again["id"] == sent["id"]
        print("Native MCP round trip and deduplication passed", flush=True)
        await run([*prefix, "restart", "--standalone"], env=env, max_seconds=30)
        restored = json.loads((await run([*prefix, "task", sent["id"]], env=env)).stdout)
        assert restored["status"] == "completed" and restored["result"] == result["result"]
        assert counter.read_text().splitlines() == [sent["id"]]
        return {
            "native_core_roundtrip": "passed",
            "worker": "scripted Claude-shaped fixture",
            "mcp_callback": "passed",
            "duplicate_suppression": "passed",
            "restart_receipt": "passed",
            "live_models_used": False,
        }
    finally:
        cleanup_errors: list[str] = []

        async def cleanup(command: list[str]) -> None:
            try:
                await run(command, env=env, max_seconds=30)
            except Exception as error:
                cleanup_errors.append(f"{command[0]} {command[1]}: {type(error).__name__}")

        await cleanup([*prefix, "stop"])
        if address:
            await cleanup([hc, "kill", address])
            await cleanup([hc, "rm", address])
        if core_started:
            await cleanup([hc, "server", "stop"])
        if cleanup_errors:
            raise RuntimeError(
                "Isolated test cleanup needs inspection: " + "; ".join(cleanup_errors)
            )
        print("Isolated brokers stopped; no user terminals selected", flush=True)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hbc-", dir="/tmp") as directory:
        result = asyncio.run(smoke(Path(directory)))
    print(json.dumps({**result, "temporary_resources": "removed"}, indent=2))


if __name__ == "__main__":
    main()
