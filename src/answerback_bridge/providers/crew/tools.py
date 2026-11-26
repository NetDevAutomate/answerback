"""MCP tools owned by the KiroCrew provider."""

from typing import Any

from ...plugin import ToolCall, ToolFunction, ToolSpec


def _crew_read(call: ToolCall) -> ToolFunction:
    async def crew_read(target: str) -> dict[str, Any]:
        """Read bounded Kirocrew history; idle state does not prove task completion."""
        return await call("crew_read", {"target": target})

    return crew_read


TOOLS = (ToolSpec(_crew_read, "read", public_errors=True),)
