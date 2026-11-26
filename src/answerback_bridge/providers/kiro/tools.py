"""MCP tools owned by the Kiro provider."""

from typing import Any

from ...plugin import ToolCall, ToolFunction, ToolSpec


def _kiro_sessions(call: ToolCall) -> ToolFunction:
    async def kiro_sessions(
        query: str = "", cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Search saved Kiro session metadata; this does not grant control of a live TUI."""
        return await call("kiro_sessions", {"query": query, "cwd": cwd, "cursor": cursor})

    return kiro_sessions


TOOLS = (ToolSpec(_kiro_sessions, "read"),)
