"""A deterministic stdio ACP peer: never starts Kiro or invokes a model."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path.cwd()
settings = json.loads((root / "agent-settings.json").read_text())
(root / "agent-pid").write_text(str(os.getpid()))
(root / "agent-argv.json").write_text(json.dumps(sys.argv[1:]))
if settings.get("descendant"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (root / "descendant-pid").write_text(str(child.pid))


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def reply(message, result):
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})


def update(session, text, kind="agent_message_chunk", **extra):
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session,
                "update": {
                    "sessionUpdate": kind,
                    "content": {"type": "text", "text": text},
                    **extra,
                },
            },
        }
    )


open_count = 0


def finish_open(message, result, session):
    global open_count
    plans = settings.get("mcp_open_plans", [])
    plan = plans[open_count] if open_count < len(plans) else {}
    open_count += 1
    requested_mode = message.get("params", {}).get("_meta", {}).get("kiro", {}).get("modeId")
    mode = settings.get("reported_mode", requested_mode)
    if mode is not None:
        result["modes"] = {
            "currentModeId": mode,
            "availableModes": [{"id": mode, "name": mode}],
        }

    def emit(events):
        for index, event in enumerate(events):
            time.sleep(event.get("delay", 0))
            (root / f"mcp-event-{open_count}-{event.get('label', index)}").touch()
            if "server_initialized" in event:
                # The v2 engine's per-server notification: a name, no tool list.
                params = {
                    "sessionId": event.get("session_id", session),
                    "serverName": event["server_initialized"],
                }
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "_kiro.dev/mcp/server_initialized",
                        "params": params,
                    }
                )
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "_kiro/mcp/status",
                    "params": {
                        "sessionId": event.get("session_id", session),
                        "servers": event.get("servers", []),
                    },
                }
            )

    emit(plan.get("before", []))
    time.sleep(plan.get("response_delay", 0))
    reply(message, result)
    (root / f"open-response-{open_count}").touch()
    emit(plan.get("after", []))


active = None
permission_rounds = []
permission_round_index = 0


def send_permission_round(session, round_):
    send(
        {
            "jsonrpc": "2.0",
            "id": round_["request_id"],
            "method": "session/request_permission",
            "params": {
                "sessionId": session,
                "toolCall": {
                    "toolCallId": round_["tool_call_id"],
                    "title": round_["title"],
                    "rawInput": round_["raw_input"],
                    "locations": round_["locations"],
                },
                "options": [
                    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "no", "name": "Deny", "kind": "reject_once"},
                ],
            },
        }
    )


for line in sys.stdin:
    message = json.loads(line)
    with (root / "agent-requests.jsonl").open("a") as stream:
        stream.write(json.dumps(message) + "\n")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        if settings.get("initialize") == "hang":
            continue
        reply(
            message,
            {
                "protocolVersion": settings.get("protocol", 1),
                "agentCapabilities": settings.get(
                    "capabilities",
                    {"loadSession": True, "sessionCapabilities": {"list": {}}},
                ),
                "agentInfo": {"name": "scripted-acp-peer", "version": "1"},
                "authMethods": [],
            },
        )
    elif method == "session/new":
        if settings.get("new") == "hang":
            continue
        finish_open(message, {"sessionId": "new-session"}, "new-session")
    elif method == "session/load":
        update(params["sessionId"], "Historical answer")
        finish_open(message, {}, params["sessionId"])
    elif method == "session/list":
        reply(
            message,
            {
                "sessions": [{"sessionId": "saved", "cwd": str(root), "title": "Saved work"}],
                "nextCursor": "page-2",
            },
        )
    elif method == "session/prompt":
        active = message
        session = params["sessionId"]
        text = params["prompt"][0]["text"]
        if text in {"permission-rounds-same", "permission-rounds-interleaved"}:
            if text == "permission-rounds-same":
                permission_rounds = [
                    {
                        "request_id": "permission-same-1",
                        "tool_call_id": "tool-shared",
                        "title": "Read first project file",
                        "raw_input": {"path": "round-1.py"},
                        "locations": [{"path": "round-1.py", "line": 1}],
                    },
                    {
                        "request_id": "permission-same-2",
                        "tool_call_id": "tool-shared",
                        "title": "Read second project file",
                        "raw_input": {"path": "round-2.py"},
                        "locations": [{"path": "round-2.py", "line": 2}],
                    },
                ]
            else:
                permission_rounds = [
                    {
                        "request_id": "permission-a-1",
                        "tool_call_id": "tool-a",
                        "title": "Read A first",
                        "raw_input": {"round": "A1"},
                        "locations": [{"path": "a-1.py", "line": 1}],
                    },
                    {
                        "request_id": "permission-b-1",
                        "tool_call_id": "tool-b",
                        "title": "Read B",
                        "raw_input": {"round": "B1"},
                        "locations": [{"path": "b-1.py", "line": 2}],
                    },
                    {
                        "request_id": "permission-a-2",
                        "tool_call_id": "tool-a",
                        "title": "Read A second",
                        "raw_input": {"round": "A2"},
                        "locations": [{"path": "a-2.py", "line": 3}],
                    },
                ]
            permission_round_index = 0
            send_permission_round(session, permission_rounds[permission_round_index])
        elif text in {"permission", "permission-finishes"}:
            tool_call = {"toolCallId": "tool-1", "title": "Read project file"}
            if "permission_locations" in settings:
                tool_call["locations"] = settings["permission_locations"]
            if "permission_raw_input" in settings:
                tool_call["rawInput"] = settings["permission_raw_input"]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": "permission-1",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": settings.get("permission_session", session),
                        "toolCall": tool_call,
                        "options": [
                            {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "no", "name": "Deny", "kind": "reject_once"},
                            {"optionId": "forever", "name": "Always", "kind": "allow_always"},
                        ],
                    },
                }
            )
            if text == "permission-finishes":
                reply(message, {"stopReason": "end_turn"})
        elif text == "wait":
            update(session, "Started")
        elif text == "mismatch":
            update("wrong-session", "Wrong answer")
            reply(message, {"stopReason": "end_turn"})
        elif text == "oversize":
            sys.stdout.write("x" * (1024 * 1024 + 1))
            sys.stdout.flush()
        elif text == "malformed":
            sys.stdout.write("{not json}\n")
            sys.stdout.flush()
        elif text == "too-much-text":
            for _ in range(5):
                update(session, "x" * 50000)
            reply(message, {"stopReason": "end_turn"})
        elif text.startswith("unsupported:"):
            name = text.removeprefix("unsupported:")
            send(
                {
                    "jsonrpc": "2.0",
                    "id": "unsupported",
                    "method": name,
                    "params": {
                        "sessionId": session,
                        "path": str(root / "must-not-exist"),
                        "content": "unauthorised",
                        "command": "true",
                        "terminalId": "terminal-1",
                    },
                }
            )
        elif text == "error":
            error = {
                "code": settings.get("error_code", -32603),
                "message": settings.get("error_message", "Fake provider error"),
            }
            if "error_data" in settings:
                error["data"] = settings["error_data"]
            send({"jsonrpc": "2.0", "id": message["id"], "error": error})
        elif text == "bad-response":
            reply(message, {"stopReason": "invented-reason"})
        elif text == "exit":
            break
        else:
            update(session, "Hidden reasoning", "agent_thought_chunk")
            update(session, "Explicit replay", _meta={"kiro": {"replay": True}})
            update(session, "Hello ")
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session,
                        "update": {
                            "sessionUpdate": "session_info_update",
                            "_meta": {"kiro": {"kind": "turn_end"}},
                        },
                    },
                }
            )
            update(session, "world")
            # A response to another request cannot finish this prompt.
            send({"jsonrpc": "2.0", "id": 9999, "result": {"stopReason": "refusal"}})
            reply(message, {"stopReason": "end_turn"})
            update(session, "After the response")
    elif method == "session/cancel":
        if active is not None:
            reply(active, {"stopReason": "cancelled"})
            active = None
    elif (
        permission_rounds
        and message.get("id") == permission_rounds[permission_round_index]["request_id"]
    ):
        with (root / "permission-round-responses.jsonl").open("a") as stream:
            stream.write(json.dumps(message) + "\n")
        permission_round_index += 1
        if permission_round_index < len(permission_rounds):
            send_permission_round(
                active["params"]["sessionId"], permission_rounds[permission_round_index]
            )
        elif message.get("result", {}).get("outcome", {}).get("outcome") != "cancelled":
            update(active["params"]["sessionId"], "Permission rounds resolved")
            reply(active, {"stopReason": "end_turn"})
            active = None
    elif message.get("id") == "permission-1":
        (root / "permission-response.json").write_text(json.dumps(message))
        if active is not None:
            update(active["params"]["sessionId"], "Permission resolved")
            reply(active, {"stopReason": "end_turn"})
            active = None
    elif message.get("id") == "unsupported":
        (root / "unsupported-response.json").write_text(json.dumps(message))
        reply(active, {"stopReason": "end_turn"})
    elif message.get("id") is None and "error" in message:
        (root / "malformed-response.json").write_text(json.dumps(message))
        reply(active, {"stopReason": "end_turn"})
