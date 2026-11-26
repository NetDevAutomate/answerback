"""Composition root for adapters. No provider selection occurs in dispatch."""

from collections.abc import Sequence
from typing import Any

from ..config import NATIVE_PROVIDERS, Config
from ..plugin import KernelContext, ProviderPlugin, check_api_version
from ..providers.herdr.adapters.core import CoreDiscovery
from ..providers.herdr.adapters.herdr import HerdrDiscovery
from ..rex import Rex
from .codex import CodexDiscovery
from .crew import CrewDiscovery
from .kiro import KiroDiscovery
from .native import NativeDiscovery
from .registry import AdapterRegistry, DiscoveryRegistry
from .rex import RexDiscovery


def build_adapters(ctx: KernelContext, plugins: Sequence[ProviderPlugin]) -> AdapterRegistry:
    """Build one adapter per plugin, in the given order, after checking each API version.

    Each plugin's store hooks are registered on ``ctx.store`` under the plugin's receipt
    kind (``definition.name``) before its adapter is built, so the adapter may rely on them.
    """
    for plugin in plugins:
        check_api_version(plugin)
    adapters = []
    for plugin in plugins:
        hooks = plugin.store_hooks()
        if hooks is not None:
            ctx.store.hooks.register(plugin.definition.name, hooks)
        adapters.append(plugin.build(ctx))
    return AdapterRegistry(adapters)


def build_discovery(
    config: Config,
    herdr: Any,
    crew: Any,
    desktop: Any,
    core: Any,
) -> DiscoveryRegistry:
    """Compatibility construction for standalone, read-only Directory callers."""
    return DiscoveryRegistry(
        [
            KiroDiscovery(config, lambda: None),
            CodexDiscovery(config, lambda: desktop),
            HerdrDiscovery(config, lambda: herdr),
            CrewDiscovery(config, lambda: crew),
            CoreDiscovery(config, lambda: core),
            RexDiscovery(
                config, lambda: Rex(config.rex_binary, config.rex_server, config.rex_host_id)
            ),
            *(NativeDiscovery(name, config) for name in NATIVE_PROVIDERS),
        ]
    )
