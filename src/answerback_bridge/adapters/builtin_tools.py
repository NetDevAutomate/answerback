"""Compatibility exports for provider-owned MCP tool declarations."""

from importlib import import_module
from typing import Any

_TOOL_MODULES = {
    "hc": ("answerback_bridge.providers.herdr.tools", "HC_TOOLS"),
    "herdr": ("answerback_bridge.providers.herdr.tools", "HERDR_TOOLS"),
    "kiro": ("answerback_bridge.providers.kiro.tools", "TOOLS"),
    "codex": ("answerback_bridge.providers.codex.tools", "TOOLS"),
    "crew": ("answerback_bridge.providers.crew.tools", "TOOLS"),
    "rex": ("answerback_bridge.providers.rex.tools", "TOOLS"),
}


def __getattr__(name: str) -> Any:
    if name != "TOOLS":
        raise AttributeError(name)
    tools = {
        provider: getattr(import_module(module), attribute)
        for provider, (module, attribute) in _TOOL_MODULES.items()
    }
    globals()[name] = tools
    return tools
