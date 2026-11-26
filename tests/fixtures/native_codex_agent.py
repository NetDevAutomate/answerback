"""Deterministic app-server peer; never invokes a model or an external tool."""

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
settings = json.loads((root / "codex-settings.json").read_text())
(root / "codex-argv.json").write_text(json.dumps(sys.argv[1:]))
(root / "codex-home.txt").write_text(os.environ.get("CODEX_HOME", ""))
(root / "codex-origin-env.json").write_text(
    json.dumps(
        {
            key: os.environ.get(key)
            for key in ("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_SESSION_ID", "CODEX_THREAD_ID")
        }
    )
)
(root / "codex-pid.txt").write_text(str(os.getpid()))
if settings.get("descendant"):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (root / "codex-descendant.txt").write_text(str(child.pid))
thread = "owned-thread"
turn = "turn-1"
turn_count = 0
servers = {}
pending_permissions = set()


def send(value):
    print(json.dumps(value), flush=True)


def replace(value):
    if isinstance(value, str):
        return {"$thread": thread, "$turn": turn}.get(value, value)
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item) for item in value]
    return value


def events(name):
    for value in settings.get(name, []):
        send(replace(value))


def finish(status=None):
    item = {
        "type": "agentMessage",
        "id": "answer-" + turn,
        "text": settings.get("answer", "native final answer"),
        "phase": settings.get("phase", "final_answer"),
    }
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread,
                "turnId": turn,
                "item": item,
            },
        }
    )
    payload = {
        "method": "turn/completed",
        "params": {
            "threadId": thread,
            "turn": {
                "id": turn,
                "status": status or settings.get("status", "completed"),
                "items": [],
                "completedAt": 123,
                "error": settings.get("error"),
            },
        },
    }
    send(payload)
    if settings.get("duplicate_terminal"):
        send(payload)


for line in sys.stdin:
    value = json.loads(line)
    with (root / "codex-requests.jsonl").open("a") as log:
        log.write(json.dumps(value) + "\n")
    method = value.get("method")
    if method == "initialize":
        if settings.get("initialize_eof"):
            break
        send(
            {
                "id": value["id"],
                "result": {
                    "userAgent": "fixture/0.169.0",
                    "codexHome": os.environ.get("CODEX_HOME"),
                    "platformFamily": "unix",
                    "platformOs": "macos",
                },
            }
        )
    elif method == "initialized":
        pass
    elif method in ("thread/start", "thread/resume"):
        params = value["params"]
        thread = params.get("threadId", "owned-thread")
        if settings.get("open_id"):
            thread = settings["open_id"]
        servers = params.get("config", {}).get("mcp_servers", {})
        send(
            {
                "id": value["id"],
                "result": {
                    "thread": {
                        "id": thread,
                        "sessionId": thread,
                        "status": {"type": settings.get("open_status", "idle")},
                        "turns": [],
                        "cwd": str(root),
                    },
                    "cwd": settings.get("open_cwd", str(root)),
                    "approvalPolicy": settings.get("approval", "on-request"),
                    "approvalsReviewer": settings.get("reviewer", "user"),
                    "sandbox": settings.get(
                        "sandbox",
                        {
                            "type": "workspaceWrite",
                            "writableRoots": [str(root)],
                            "networkAccess": False,
                        },
                    ),
                },
            }
        )
    elif method == "thread/read":
        send(
            {
                "id": value["id"],
                "result": {
                    "thread": {
                        "id": value["params"]["threadId"],
                        "cwd": str(root),
                        "status": {"type": settings.get("read_status", "idle")},
                        "turns": [],
                    }
                },
            }
        )
    elif method == "mcpServerStatus/list":
        selected = value["params"].get("serverName")
        if settings.get("global_inventory_trap") and not selected:
            # Refuse at once: a test that sets the trap proves the transport never
            # asks for the global inventory, without a short real-clock deadline.
            send(
                {
                    "id": value["id"],
                    "error": {"code": -32600, "message": "global MCP inventory trap"},
                }
            )
            continue
        send(
            {
                "id": value["id"],
                "result": {
                    "data": [
                        {
                            "name": name,
                            "tools": {},
                            "authStatus": "notLoggedIn",
                            "runtimeStatus": settings.get("mcp_status", "connected"),
                        }
                        for name in [
                            *(name for name in servers if not selected or name == selected),
                            *settings.get("extra_mcp", []),
                        ]
                    ],
                    "nextCursor": settings.get("mcp_cursor"),
                },
            }
        )
    elif method == "turn/start":
        turn_count += 1
        turn = settings.get("turn_id", f"turn-{turn_count}")
        (root / "codex-prompt-seen").write_text(turn)
        events("before_response")
        if settings.get("start_error"):
            send({"id": value["id"], "error": {"code": -32600, "message": "native refusal"}})
            continue
        send(
            {
                "id": value["id"],
                "result": {"turn": {"id": turn, "status": "inProgress", "items": []}},
            }
        )
        if settings.get("eof"):
            break
        if settings.get("malformed"):
            print(settings["malformed"], flush=True)
            continue
        if settings.get("oversized"):
            print("x" * settings["oversized"], flush=True)
            continue
        events("after_response")
        for request in settings.get("permissions", []):
            request = replace(request)
            pending_permissions.add((type(request["id"]).__name__, request["id"]))
            send(request)
        if settings.get("permissions"):
            events("after_permissions")
            if settings.get("finish_while_permission"):
                finish()
        elif not settings.get("hold"):
            finish()
            if settings.get("exit_after_terminal"):
                break
    elif method == "turn/interrupt":
        (root / "codex-cancel-seen").write_text(json.dumps(value))
        send({"id": value["id"], "result": {}})
        if not settings.get("cancel_ack_only"):
            finish(settings.get("cancel_status", "interrupted"))
    elif method is None and "id" in value:
        pending_permissions.discard((type(value["id"]).__name__, value["id"]))
        (root / "codex-permission-reply.json").write_text(json.dumps(value))
        if settings.get("repeat_permission"):
            settings["repeat_permission"] = False
            send(replace(settings["permissions"][0]))
            continue
        if not pending_permissions and settings.get("permissions") and not settings.get("hold"):
            finish("interrupted" if value.get("result", {}).get("decision") == "cancel" else None)
    else:
        send(
            {
                "id": value.get("id"),
                "error": {"code": -32601, "message": "unsupported fixture method"},
            }
        )
