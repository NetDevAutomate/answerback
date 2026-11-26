"""Compatibility exports for provider-owned CLI surfaces."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "rex": ("answerback_bridge.providers.rex.cli", "rex"),
    "hc": ("answerback_bridge.providers.herdr.cli", "hc"),
    "kiro": ("answerback_bridge.providers.kiro.cli", "kiro"),
    "codex": ("answerback_bridge.providers.codex.cli", "codex"),
    "herdr": ("answerback_bridge.providers.herdr.cli", "herdr"),
    "crew": ("answerback_bridge.providers.crew.cli", "crew"),
}


def __getattr__(name: str) -> Any:
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
