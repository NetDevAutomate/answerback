"""MCP tools owned by the Herdr provider."""

from typing import Any

from ...plugin import ToolCall, ToolFunction, ToolSpec


def _hc_read(call: ToolCall) -> ToolFunction:
    async def hc_read(target: str, lines: int = 100) -> dict[str, Any]:
        """Read a Herdr App/core terminal; preserves incomplete screen/history observations."""
        return await call("hc_read", {"target": target, "lines": lines})

    return hc_read


def _herdr_list_agents(call: ToolCall) -> ToolFunction:
    async def herdr_list_agents() -> dict[str, Any]:
        """List agents in the explicitly configured Herdr session."""
        return await call("herdr_list_agents", {})

    return herdr_list_agents


def _herdr_read_agent(call: ToolCall) -> ToolFunction:
    async def herdr_read_agent(target: str, lines: int = 100) -> dict[str, Any]:
        """Read recent output from an explicit Herdr agent name or pane ID; maximum 500 lines."""
        return await call("herdr_read_agent", {"target": target, "lines": lines})

    return herdr_read_agent


HC_TOOLS = (ToolSpec(_hc_read, "read"),)
HERDR_TOOLS = (
    ToolSpec(_herdr_list_agents, "read"),
    ToolSpec(_herdr_read_agent, "read"),
)
