"""MCP tools owned by the Rex provider."""

from typing import Any

from ...plugin import ToolCall, ToolFunction, ToolSpec


def _rex_read(call: ToolCall) -> ToolFunction:
    async def rex_read(session_id: str, block_id: str, max_bytes: int = 16384) -> dict[str, Any]:
        """Read an exact Rex terminal's bounded prefix and identity evidence."""
        return await call(
            "rex_read", {"session_id": session_id, "block_id": block_id, "max_bytes": max_bytes}
        )

    return rex_read


def _rex_enroll(call: ToolCall) -> ToolFunction:
    async def rex_enroll(
        name: str, session_id: str, block_id: str, request_id: str
    ) -> dict[str, Any]:
        """Explicitly enroll a verified local Rex conversation for cooperative work."""
        return await call(
            "rex_enroll",
            {
                "name": name,
                "session_id": session_id,
                "block_id": block_id,
                "request_id": request_id,
            },
        )

    return rex_enroll


def _rex_receive(call: ToolCall) -> ToolFunction:
    async def rex_receive(peer_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Receive queued Rex assignments; inspect them before acknowledging."""
        return await call("rex_receive", {"peer_id": peer_id, "wait_seconds": wait_seconds})

    return rex_receive


def _rex_ack(call: ToolCall) -> ToolFunction:
    async def rex_ack(peer_id: str, task_id: str) -> dict[str, Any]:
        """Acknowledge one exact Rex assignment after reading it."""
        return await call("rex_ack", {"peer_id": peer_id, "task_id": task_id})

    return rex_ack


def _rex_complete(call: ToolCall) -> ToolFunction:
    async def rex_complete(peer_id: str, task_id: str, result: str) -> dict[str, Any]:
        """Record the actual result of an acknowledged, authorized Rex assignment."""
        return await call(
            "rex_complete", {"peer_id": peer_id, "task_id": task_id, "result": result}
        )

    return rex_complete


def _rex_retire(call: ToolCall) -> ToolFunction:
    async def rex_retire(peer_id: str) -> dict[str, Any]:
        """Retire an exact cooperative enrollment while preserving its task history."""
        return await call("rex_retire", {"peer_id": peer_id})

    return rex_retire


TOOLS = (
    ToolSpec(_rex_read, "read", public_errors=True),
    ToolSpec(_rex_enroll, "write", public_errors=True),
    ToolSpec(_rex_receive, "read", public_errors=True),
    ToolSpec(_rex_ack, "write", public_errors=True),
    ToolSpec(_rex_complete, "write", public_errors=True),
    ToolSpec(_rex_retire, "write", public_errors=True),
)
