"""Provider configuration contract and compatibility exports.

Each provider owns its concrete ``ProviderConfig`` subclass. The kernel keeps only the
block contract and resolves historical imports lazily from provider packages.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from importlib import import_module
from typing import Any, ClassVar


@dataclass
class ProviderConfig:
    """One provider's block. Subclasses add fields and validate their own policy."""

    provider: ClassVar[str]
    flat_aliases: ClassVar[Mapping[str, str]] = {}

    def validate(self) -> None:
        """Raise ``ValueError`` for a wrong value; may normalise a field in place."""
        return None

    def legacy_selected(self) -> bool:
        """Whether a config saved before ``provider_selection`` selected this provider."""
        return False

    @classmethod
    def flat_key(cls, name: str) -> str:
        return cls.flat_aliases.get(name, f"{cls.provider}_{name}")

    @classmethod
    def flat_keys(cls) -> dict[str, str]:
        """Return flat key to field mappings, refusing aliases that collide."""
        keys: dict[str, str] = {}
        for entry in fields(cls):
            flat = cls.flat_key(entry.name)
            if flat in keys:
                raise ValueError(
                    f"{cls.provider} config fields {keys[flat]!r} and {entry.name!r} both "
                    f"declare flat key {flat!r}"
                )
            keys[flat] = entry.name
        return keys


_COMPATIBILITY_EXPORTS = {
    "CodexConfig": ("answerback_bridge.providers.codex.config", "CodexConfig"),
    "CrewConfig": ("answerback_bridge.providers.crew.config", "CrewConfig"),
    "HcConfig": ("answerback_bridge.providers.herdr.config", "HcConfig"),
    "HerdrConfig": ("answerback_bridge.providers.herdr.config", "HerdrConfig"),
    "KIRO_TOOL_NAME": ("answerback_bridge.providers.kiro.config", "KIRO_TOOL_NAME"),
    "KiroConfig": ("answerback_bridge.providers.kiro.config", "KiroConfig"),
    "validate_kiro_trust_tools": (
        "answerback_bridge.providers.kiro.config",
        "validate_kiro_trust_tools",
    ),
    "NATIVE_PROVIDERS": ("answerback_bridge.providers.native.config", "NATIVE_PROVIDERS"),
    "NativeConfig": ("answerback_bridge.providers.native.config", "NativeConfig"),
    "CodexCliConfig": ("answerback_bridge.providers.native.config", "CodexCliConfig"),
    "ClaudeCodeConfig": ("answerback_bridge.providers.native.config", "ClaudeCodeConfig"),
    "GrokConfig": ("answerback_bridge.providers.native.config", "GrokConfig"),
    "RexConfig": ("answerback_bridge.providers.rex.config", "RexConfig"),
}


def __getattr__(name: str) -> Any:
    """Resolve a historical concrete-schema import from its provider package."""
    try:
        module, attribute = _COMPATIBILITY_EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
