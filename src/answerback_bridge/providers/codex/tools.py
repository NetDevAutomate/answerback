"""MCP tools owned by the Codex Desktop provider."""

from typing import Any

from ...plugin import ToolCall, ToolFunction, ToolSpec


def _desktop_list(call: ToolCall) -> ToolFunction:
    async def desktop_list() -> dict[str, Any]:
        """List discovered Desktop/IDE chats and optional saved aliases; no live-status claim."""
        return await call("desktop_list", {})

    return desktop_list


def _desktop_sessions(call: ToolCall) -> ToolFunction:
    async def desktop_sessions(
        query: str = "", cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Search one page of local indexed Desktop/IDE metadata; follow next_cursor for more."""
        return await call("desktop_sessions", {"query": query, "cursor": cursor, "limit": limit})

    return desktop_sessions


def _desktop_read(call: ToolCall) -> ToolFunction:
    async def desktop_read(alias: str) -> dict[str, Any]:
        """Read the newest five persisted turns of a paired Desktop chat; no live status claim."""
        return await call("desktop_read", {"alias": alias})

    return desktop_read


def _desktop_submit(call: ToolCall) -> ToolFunction:
    async def desktop_submit(alias: str, message: str, request_id: str) -> dict[str, Any]:
        """Queue authorized work to a paired Desktop chat. Reuse request_id for retries."""
        return await call(
            "desktop_submit", {"alias": alias, "message": message, "request_id": request_id}
        )

    return desktop_submit


TOOLS = (
    ToolSpec(_desktop_list, "read"),
    ToolSpec(_desktop_sessions, "read"),
    ToolSpec(_desktop_read, "read"),
    ToolSpec(_desktop_submit, "send"),
)
