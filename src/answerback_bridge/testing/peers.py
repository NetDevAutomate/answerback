"""Scripted Herdr and ACP peers; no models, real sessions, or external network."""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..exit_codes import EXIT_BLOCKED, EXIT_WAIT_TIMED_OUT
from ..process import module_launch_argv

PREFIX = "HERDR_BRIDGE_HARNESS "
# `task --wait` exits with these for a receipt that is not yet terminal: a timed-out
# wait is polled again, a blocked task stops the fixture through check_receipt.
WAIT_RECEIPT_EXITS = frozenset({EXIT_WAIT_TIMED_OUT, EXIT_BLOCKED})


def record(root: Path, event: str, **fields: Any) -> None:
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n")


def payload(message: str) -> dict[str, Any]:
    for line in message.splitlines():
        if line.startswith(PREFIX):
            return json.loads(line.removeprefix(PREFIX))
    raise ValueError("Missing deterministic harness assignment")


def reservation(root: Path, request_id: str) -> str:
    with sqlite3.connect(f"file:{root / 'tasks.sqlite'}?mode=ro", uri=True) as database:
        row = database.execute("SELECT id FROM tasks WHERE request_key=?", (request_id,)).fetchone()
    if row is None:
        raise ValueError("Provider received work before its receipt was reserved")
    return str(row[0])


def cli(
    root: Path, deadline: float, *args: str, accepted_exits: frozenset[int] = frozenset()
) -> dict[str, Any]:
    """Run one bridge CLI command and return its JSON output.

    Any non-zero exit is a broken fixture, except the codes in `accepted_exits`: the
    `task --wait` poll passes EXIT_WAIT_TIMED_OUT and EXIT_BLOCKED, whose stdout is a
    receipt to inspect (check_receipt decides what to do with it), not an error.
    """
    remaining = min(25, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Fixture deadline expired")
    result = subprocess.run(
        module_launch_argv(root / "config.json", *args),
        capture_output=True,
        text=True,
        timeout=remaining,
        check=False,
    )
    if result.returncode and result.returncode not in accepted_exits:
        raise RuntimeError("Fixture CLI operation failed")
    return json.loads(result.stdout)


def check_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("status") in ("failed", "blocked", "uncertain", "abandoned", "cancelled"):
        raise RuntimeError(f"Fixture child stopped with {receipt['status']}")


def herdr(root: Path) -> None:
    args = sys.argv[1:]
    agent = {
        "name": "harness-worker",
        "terminal_id": "harness-terminal",
        "agent_kind": "scripted",
        "agent_status": "idle",
        "cwd": str(root),
    }
    if args == ["--version"]:
        print("herdr-harness-fixture 1")
    elif "--help" in args:
        print("agent prompt get list")
    elif len(args) >= 4 and args[:3] == ["--session", "harness", "agent"]:
        operation = args[3]
        if operation == "list":
            print(json.dumps({"result": {"agents": [agent]}}))
        elif operation == "get":
            print(json.dumps({"result": {"agent": agent}}))
        elif operation == "prompt":
            envelope = args[5]
            task = payload(envelope)
            task_id = reservation(root, task["request_id"])
            marker = re.search(r"\[(?:answerback-task|codex-herdr-task):([a-f0-9-]+)\]", envelope)
            if marker is None or marker.group(1) != task_id:
                raise ValueError("Wrong parent receipt marker")
            record(root, "prompt", provider="herdr", task_id=task_id, reserved=True)
            result = task["nonce"]
            if task["action"] == "delegate":
                child = {**task, "action": "reply", "request_id": task["child_request_id"]}
                receipt = cli(
                    root,
                    task["deadline"],
                    "send",
                    task["child_selector"],
                    "--message",
                    PREFIX + json.dumps(child),
                    "--request-id",
                    task["child_request_id"],
                    *(
                        ["--binding", task["child_binding"]]
                        if task.get("child_binding") is not None
                        else []
                    ),
                )
                record(root, "child_submitted", via="cli", task_id=receipt["id"])
                while receipt.get("status") != "completed":
                    check_receipt(receipt)
                    receipt = cli(
                        root,
                        task["deadline"],
                        "task",
                        receipt["id"],
                        "--wait",
                        "1",
                        accepted_exits=WAIT_RECEIPT_EXITS,
                    )
                result = receipt["result"]
            result_file = root / f"result-{task_id}.txt"
            result_file.write_text(result, encoding="utf-8")
            completed = cli(
                root, task["deadline"], "complete", task_id, "--result-file", str(result_file)
            )
            if completed["status"] != "completed":
                raise ValueError("Herdr worker completion was not recorded")
            record(root, "task_complete", provider="herdr", task_id=task_id)
            print(json.dumps({"result": {"agent": agent}}))
        else:
            raise ValueError("Unsupported fixture Herdr operation")
    else:
        raise ValueError("Fixture requires its explicit harness session")


async def callback(root: Path, task: dict[str, Any], server: dict[str, Any]) -> str:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    # Use the exact STDIO frontend supplied by the real ACP session/new or session/load.
    parameters = StdioServerParameters(
        command=server["command"], args=server["args"], cwd=str(root)
    )
    async with asyncio.timeout(min(25, max(0.01, task["deadline"] - time.monotonic()))):
        async with Client(parameters, read_timeout_seconds=25) as client:
            child = {**task, "action": "reply", "request_id": task["child_request_id"]}
            response = await client.call_tool(
                "peer_submit",
                {
                    "selector": task["child_selector"],
                    "message": PREFIX + json.dumps(child),
                    "request_id": task["child_request_id"],
                    "binding": task.get("child_binding"),
                },
            )
            receipt = response.structured_content
            if not isinstance(receipt, dict) or not receipt.get("id"):
                raise ValueError("MCP did not return a correlated receipt")
            record(root, "child_submitted", via="mcp-stdio", task_id=receipt["id"])
            while receipt.get("status") != "completed":
                check_receipt(receipt)
                response = await client.call_tool(
                    "task_wait", {"task_id": receipt["id"], "seconds": 1}
                )
                receipt = response.structured_content
                if not isinstance(receipt, dict):
                    raise ValueError("MCP returned an invalid receipt")
            return str(receipt["result"])


def send(message: dict[str, Any]) -> None:
    print(json.dumps(message), flush=True)


def acp(root: Path) -> None:
    server: dict[str, Any] | None = None
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        params = request.get("params") or {}
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "agentInfo": {"name": "harness-acp-fixture", "version": "1"},
                "authMethods": [],
            }
        elif method in ("session/new", "session/load"):
            server = next(
                item
                for item in params["mcpServers"]
                if item["name"] in ("answerback_bridge", "herdr_bridge")
            )
            record(root, "mcp_definition", frontend=server["args"][-1])
            mode_path = root / "acp-mode"
            if method == "session/new":
                kiro = params.get("_meta", {}).get("kiro", {})
                profiles = {profile["id"] for profile in kiro.get("customAgents", [])}
                requested = kiro.get("modeId")
                mode_path.write_text(requested if requested in profiles else "vibe")
            mode = mode_path.read_text() if mode_path.exists() else "vibe"
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "_kiro/mcp/status",
                    "params": {
                        "sessionId": params.get("sessionId", "harness-acp-session"),
                        "servers": [
                            {
                                "name": server["name"],
                                "status": "connected",
                                "tools": [
                                    {"name": name, "disabled": False}
                                    for name in ("peer_submit", "task_wait")
                                ],
                            }
                        ],
                    },
                }
            )
            result = {"sessionId": "harness-acp-session"} if method == "session/new" else {}
            result["modes"] = {
                "currentModeId": mode,
                "availableModes": [{"id": mode, "name": mode}],
            }
        elif method == "session/prompt":
            task = payload(params["prompt"][0]["text"])
            task_id = reservation(root, task["request_id"])
            record(root, "prompt", provider="acp", task_id=task_id, reserved=True)
            text = task["nonce"]
            if task["action"] == "delegate":
                if server is None:
                    raise ValueError("ACP session supplied no bridge MCP definition")
                text = asyncio.run(callback(root, task, server))
            fault = root / "fault.json"
            if fault.exists() and json.loads(fault.read_text()).get("wrong_nonce"):
                text = "WRONG_NONCE"
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": params["sessionId"],
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )
            result = {"stopReason": "end_turn"}
        else:
            continue
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})


def main(provider: str, root: Path) -> None:
    record(root, "started", provider=provider)
    if provider == "herdr":
        herdr(root)
    elif provider == "acp":
        acp(root)
    else:
        raise ValueError("Unknown deterministic fixture")
