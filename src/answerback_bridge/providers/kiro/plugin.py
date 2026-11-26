"""Kiro provider definition and composition."""

from typing import Any

from ...adapters.catalog import ProviderDefinition
from ...adapters.contracts import ProviderAdapter
from ...plugin import (
    KernelContext,
    PluginOwners,
    ProjectPathHooks,
    ProviderPluginSpec,
    provider_plugin,
)
from . import cli
from .config import KiroConfig
from .reasons import REASON_ORDER, REASONS
from .tools import TOOLS
from .verbs import rpc as kiro_rpc

DEFINITION = ProviderDefinition(
    "kiro",
    "kiro",
    order=40,
    recovery_states=("running", "blocked", "queued"),
    routing_fields=("kiro.binary", "kiro.workspaces"),
    digest_keys=(("kiro.binary", "kiro_binary"), ("kiro.workspaces", "kiro_workspaces")),
)
CAPABILITY_LIST = ("send", "result", "permissions", "cancel")
CAPABILITIES = frozenset(CAPABILITY_LIST)
AGENT_PROGRAMS = {"kiro": "kiro", "kiro-cli": "kiro"}
_STORE_HOOKS = ProjectPathHooks("cwd")


def _status(config: Any) -> dict[str, Any]:
    return {
        "state": "configured" if config.provider_enabled("kiro") else "unconfigured",
        "protocol": "acp-v1",
        "capabilities": list(CAPABILITY_LIST),
    }


def _kiro(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapter import KiroAdapter

        return KiroAdapter(ctx, owners.workers)

    return provider_plugin(
        "kiro",
        build,
        KiroConfig,
        _kiro,
        definition=DEFINITION,
        capabilities=CAPABILITIES,
        hooks=lambda: _STORE_HOOKS,
        programs=AGENT_PROGRAMS,
        reasons=REASONS,
        reason_order=REASON_ORDER,
        status_block=_status,
        status_name="kiro_cli",
        status_order=100,
        verbs=kiro_rpc(owners),
        commands=cli.kiro,
        tools=TOOLS,
    )


def kiro_plugin() -> ProviderPluginSpec:
    """Return the detached Kiro provider composition."""
    return _kiro(PluginOwners.detached())
