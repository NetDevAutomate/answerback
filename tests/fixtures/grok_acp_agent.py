"""Deterministic ACP fixture for Grok transport policy; never invokes a model."""

import json
import os
import sys
from pathlib import Path

root = Path.cwd()
settings = json.loads((root / "grok-settings.json").read_text())
(root / "grok-pid").write_text(str(os.getpid()))
(root / "grok-argv.json").write_text(json.dumps(sys.argv[1:]))
(root / "grok-environment.json").write_text(
    json.dumps(
        {
            key: value
            for key, value in os.environ.items()
            if key == "GROK_HOME"
            or key == "GROK_DISABLE_AUTOUPDATER"
            or key.endswith("_ENABLED")
            and key.startswith(("GROK_CLAUDE_", "GROK_CURSOR_"))
        }
    )
)


def send(message):
    print(json.dumps(message), flush=True)


def reply(message, result):
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})


def update(session, text):
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def mcp_update(servers):
    send(
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/mcp/servers_updated",
            "params": {"mcpServers": servers},
        }
    )


def permission(session, ident):
    send(
        {
            "jsonrpc": "2.0",
            "id": ident,
            "method": "session/request_permission",
            "params": {
                "sessionId": settings.get("permission_session", session),
                "toolCall": {"toolCallId": ident, "title": "Read the selected workspace file"},
                "options": [
                    {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "no", "name": "Reject once", "kind": "reject_once"},
                    {"optionId": "forever", "name": "Always allow", "kind": "allow_always"},
                ],
            },
        }
    )


active = None
pending_permissions = set()
for line in sys.stdin:
    message = json.loads(line)
    with (root / "grok-requests.jsonl").open("a") as stream:
        stream.write(json.dumps(message) + "\n")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        if "initial_mcp_notification" in settings:
            mcp_update(settings["initial_mcp_notification"])
        metadata = {
            "grokShell": True,
            "agentVersion": "1.0.77",
            "currentWorkingDirectory": str(root),
            "defaultAuthMethodId": None,
            "mcpServers": settings.get("inherited_mcp", []),
        }
        metadata.update(settings.get("initialization_meta", {}))
        reply(
            message,
            {
                "protocolVersion": settings.get("protocol", 1),
                "agentCapabilities": {
                    "loadSession": True,
                    "sessionCapabilities": {"resume": {}, "close": {}},
                },
                "authMethods": settings.get("auth_methods", []),
                "_meta": metadata,
            },
        )
    elif method == "authenticate":
        reply(message, {})
    elif method == "session/new":
        send(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/session/setup",
                "params": {"phase": "persistence_init", "sessionId": "provisional"},
            }
        )
        if settings.get("auth_error"):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": "Authentication required",
                        "data": "no auth method id provided",
                    },
                }
            )
            continue
        if settings.get("setup_replay"):
            update("admitted-session", "Historical answer")
        if not settings.get("missing_mcp_snapshot"):
            # Native 1.0.77 reports discovered configuration separately from the
            # explicit ACP MCP list; it reported [] with an injected server.
            mcp_update(settings.get("merged_mcp", []))
        reply(message, {"sessionId": settings.get("new_id", "admitted-session")})
    elif method in {"session/load", "session/resume"}:
        update(params["sessionId"], "Historical answer")
        reply(message, {})
    elif method == "session/prompt":
        active = message
        session = params["sessionId"]
        text = params["prompt"][0]["text"]
        if text in {"permission", "two-permissions"}:
            pending_permissions = {"permission-1"}
            permission(session, "permission-1")
            if text == "two-permissions":
                pending_permissions.add("permission-2")
                permission(session, "permission-2")
        elif text == "wait":
            update(session, "Started")
        elif text == "exit":
            break
        elif text == "malformed":
            print("{not json}", flush=True)
        elif text == "duplicate-key":
            print('{"jsonrpc":"2.0","id":1,"id":2,"result":{}}', flush=True)
        elif text == "scalar":
            print("[]", flush=True)
        elif text == "oversized":
            sys.stdout.write("x" * (1024 * 1024 + 1))
            sys.stdout.flush()
        elif text == "error":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32603, "message": "Fixture provider failure"},
                }
            )
        elif text == "unsupported":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": "unsupported",
                    "method": "_x.ai/auth/get_url",
                    "params": {},
                }
            )
        elif text == "mcp-drift":
            mcp_update([{"name": "unexpected"}])
            reply(message, {"stopReason": "end_turn"})
        else:
            if text not in {"empty", "unknown-stop"}:
                update("foreign" if text == "wrong-session" else session, "Current answer")
            send({"jsonrpc": "2.0", "id": 900000, "result": {"stopReason": "refusal"}})
            reason = settings.get("stop_reason", "end_turn")
            reply(message, {"stopReason": "invented" if text == "unknown-stop" else reason})
            if text == "duplicate":
                reply(message, {"stopReason": reason})
            update(session, "After terminal response")
    elif method == "session/cancel":
        if active is not None and not settings.get("ignore_cancel"):
            reply(active, {"stopReason": "cancelled"})
            active = None
    elif message.get("id") in pending_permissions:
        with (root / "grok-permission-responses.jsonl").open("a") as stream:
            stream.write(json.dumps(message) + "\n")
        pending_permissions.remove(message["id"])
        if active is not None and not pending_permissions:
            update(active["params"]["sessionId"], "Permission resolved")
            reply(active, {"stopReason": "end_turn"})
            active = None
    elif message.get("id") is None and "error" in message and active is not None:
        reply(active, {"stopReason": "end_turn"})
