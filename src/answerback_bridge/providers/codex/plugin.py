"""Codex Desktop provider definition and composition."""

from typing import Any

from ...adapters.catalog import ProviderDefinition
from ...adapters.contracts import ProviderAdapter
from ...plugin import (
    KernelContext,
    PluginOwners,
    ProviderPluginSpec,
    StoreHooks,
    provider_plugin,
)
from . import cli
from .config import CodexConfig
from .reasons import REASON_ORDER, REASONS
from .tools import TOOLS
from .verbs import rpc as codex_rpc

DEFINITION = ProviderDefinition(
    "codex",
    "desktop",
    order=30,
    routing_fields=("codex.binary", "codex.home", "codex.enabled", "codex.desktop_threads"),
    digest_keys=(
        ("codex.binary", "codex_binary"),
        ("codex.home", "codex_home"),
        ("codex.enabled", "codex_enabled"),
        ("codex.desktop_threads", "desktop_threads"),
    ),
)
CAPABILITY_LIST = ("discovery", "queue", "persisted_result")
CAPABILITIES = frozenset(CAPABILITY_LIST)
INSTRUCTIONS = "Provider codex (legacy alias desktop) only addresses Codex Desktop/IDE."
_STORE_HOOKS: StoreHooks | None = None


def _status(config: Any) -> dict[str, Any]:
    return {
        "state": "unchecked" if config.provider_enabled("codex") else "disabled",
        "capabilities": list(CAPABILITY_LIST),
        "live_progress": False,
        "cancel": False,
    }


def _store_hooks() -> StoreHooks:
    global _STORE_HOOKS
    if _STORE_HOOKS is None:
        from .adapter import DesktopStoreHooks

        _STORE_HOOKS = DesktopStoreHooks()
    return _STORE_HOOKS


def _codex(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapter import CodexAdapter

        return CodexAdapter(ctx, owners.desktop)

    return provider_plugin(
        "codex",
        build,
        CodexConfig,
        _codex,
        definition=DEFINITION,
        capabilities=CAPABILITIES,
        hooks=_store_hooks,
        instructions=INSTRUCTIONS,
        reasons=REASONS,
        reason_order=REASON_ORDER,
        status_block=_status,
        status_order=70,
        verbs=codex_rpc(owners),
        commands=cli.codex,
        tools=TOOLS,
    )


def codex_plugin() -> ProviderPluginSpec:
    """Return the detached Codex Desktop provider composition."""
    return _codex(PluginOwners.detached())
