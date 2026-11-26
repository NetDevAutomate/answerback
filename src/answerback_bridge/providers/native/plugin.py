"""Definitions and composition for owned native CLI providers."""

from collections.abc import Callable
from typing import Any

from ...adapters.catalog import ProviderDefinition
from ...adapters.contracts import ProviderAdapter
from ...plugin import KernelContext, PluginOwners, ProviderPluginSpec, provider_plugin
from ...provider_config import ProviderConfig
from .config import ClaudeCodeConfig, CodexCliConfig, GrokConfig
from .reasons import REASON_ORDER, REASONS

CAPABILITY_LIST = ("send", "result", "permissions", "cancel")
CAPABILITIES = frozenset(CAPABILITY_LIST)
DEFINITIONS = {
    provider: ProviderDefinition(
        provider,
        provider,
        order=order,
        recovery_states=("running", "blocked", "submitted", "queued"),
        routing_fields=(f"{provider}.binary", f"{provider}.home", f"{provider}.workspaces"),
        legacy_neutral_defaults=(
            (f"{provider}.binary", binary),
            (f"{provider}.home", None),
            (f"{provider}.workspaces", {}),
        ),
        digest_keys=tuple(
            (f"{provider}.{name}", f"{provider}_{name}")
            for name in ("binary", "home", "workspaces")
        ),
    )
    for provider, binary, order in (
        ("codex_cli", "codex", 80),
        ("claude_code", "claude", 90),
        ("grok", "grok", 100),
    )
}
AGENT_PROGRAMS = {
    "codex_cli": {"codex": "codex"},
    "claude_code": {"claude": "claude", "claude-code": "claude"},
    "grok": {"grok": "grok", "grok-build": "grok"},
}
STATUS_ORDER = {"codex_cli": 10, "claude_code": 20, "grok": 30}


def _native(
    name: str,
    config_schema: type[ProviderConfig],
    owners: PluginOwners,
    owner_factory: Callable[[PluginOwners], ProviderPluginSpec],
) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapter import NativeAdapter

        adapter = NativeAdapter(name, ctx)
        adapter.controls.pressure = owners.pressure()
        return adapter

    def status(config: Any) -> dict[str, Any]:
        return {
            "state": "configured" if config.provider_enabled(name) else "disabled",
            "capabilities": list(CAPABILITY_LIST),
            "session_ownership": "bridge",
        }

    return provider_plugin(
        name,
        build,
        config_schema,
        owner_factory,
        definition=DEFINITIONS[name],
        capabilities=CAPABILITIES,
        programs=AGENT_PROGRAMS[name],
        reasons=REASONS if name == "codex_cli" else (),
        reason_order=REASON_ORDER if name == "codex_cli" else (),
        status_block=status,
        status_order=STATUS_ORDER[name],
    )


def _codex_cli(owners: PluginOwners) -> ProviderPluginSpec:
    return _native("codex_cli", CodexCliConfig, owners, _codex_cli)


def _claude_code(owners: PluginOwners) -> ProviderPluginSpec:
    return _native("claude_code", ClaudeCodeConfig, owners, _claude_code)


def _grok(owners: PluginOwners) -> ProviderPluginSpec:
    return _native("grok", GrokConfig, owners, _grok)


def codex_cli_plugin() -> ProviderPluginSpec:
    """Return the detached Codex CLI provider composition."""
    return _codex_cli(PluginOwners.detached())


def claude_code_plugin() -> ProviderPluginSpec:
    """Return the detached Claude Code provider composition."""
    return _claude_code(PluginOwners.detached())


def grok_plugin() -> ProviderPluginSpec:
    """Return the detached Grok provider composition."""
    return _grok(PluginOwners.detached())
