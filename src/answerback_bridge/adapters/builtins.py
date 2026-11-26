"""Compatibility composition for installed built-in provider plugins.

Provider implementation and policy live under ``answerback_bridge.providers``. This module
only loads those entry points, restores their historical adapter order, rebinds first-party
plugin specs to a live service, and exposes temporary compatibility names.
"""

from collections.abc import Iterable

from ..plugin import (
    PROVIDER_GROUP,
    Owners,
    ProviderPlugin,
    ProviderPluginSpec,
    builtin_plugin,
    builtin_verbs,
    load_plugins,
)
from ..plugin import (
    BuiltinPlugin as BuiltinPlugin,
)
from ..plugin import (
    BuiltinSource as BuiltinSource,
)
from ..tasks import StoreHookRegistry
from . import catalog
from .catalog import ProviderDefinition

# Historical adapter order is composition compatibility, not provider policy.
_BUILTIN_ADAPTER_ORDER = (
    "kiro",
    "codex",
    "herdr",
    "crew",
    "hc",
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
)


def builtin_store_hooks() -> StoreHookRegistry:
    """Return loaded provider store hooks keyed by receipt kind."""
    registry = StoreHookRegistry()
    for plugin in builtin_plugins():
        hooks = plugin.store_hooks()
        if hooks is not None:
            registry.register(plugin.definition.name, hooks)
    return registry


def builtin_plugins(owners: Owners | None = None) -> tuple[ProviderPlugin, ...]:
    """Load providers and restore the adapter registry's historical order."""
    plugins = load_plugins(PROVIDER_GROUP)
    if owners is not None:
        plugins = [
            plugin.with_owners(owners) if isinstance(plugin, ProviderPluginSpec) else plugin
            for plugin in plugins
        ]
    slots = _adapter_slots(plugin.definition for plugin in plugins)
    return tuple(sorted(plugins, key=lambda plugin: slots[plugin.definition.registration]))


def _adapter_slots(
    entries: Iterable[ProviderDefinition],
) -> dict[str, tuple[int, int, tuple[bool, int, str]]]:
    """Assign built-ins their old slots and extensions the catalog gap after them."""
    kernel = {registration: index for index, registration in enumerate(_BUILTIN_ADAPTER_ORDER)}
    slots: dict[str, tuple[int, int, tuple[bool, int, str]]] = {}
    preceding = -1
    for entry in catalog.ordered(entries):
        if entry.registration in kernel:
            preceding = max(preceding, kernel[entry.registration])
            slots[entry.registration] = (kernel[entry.registration], 0, entry.order_key())
        else:
            slots[entry.registration] = (preceding, 1, entry.order_key())
    return slots


# Compatibility aliases retained while downstream code migrates to ``answerback_bridge.plugin``.
_builtin = builtin_plugin
_verbs = builtin_verbs
