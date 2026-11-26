"""Rex provider definition and composition."""

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
from .config import RexConfig
from .reasons import REASON_ORDER, REASONS
from .tools import TOOLS
from .verbs import rpc as rex_rpc

DEFINITION = ProviderDefinition(
    "rex",
    "rex",
    order=70,
    routing_fields=("rex.enabled", "rex.binary", "rex.server", "rex.host_id"),
    legacy_neutral_defaults=(
        ("rex.enabled", False),
        ("rex.binary", "rex"),
        ("rex.server", None),
        ("rex.host_id", None),
    ),
    digest_keys=(
        ("rex.enabled", "rex_enabled"),
        ("rex.binary", "rex_binary"),
        ("rex.server", "rex_server"),
        ("rex.host_id", "rex_host_id"),
    ),
)
CAPABILITY_LIST = ("discovery", "read", "enroll", "queue", "ack", "result")
CAPABILITIES = frozenset(CAPABILITY_LIST)
_STORE_HOOKS = ProjectPathHooks("binding", "foreground_cwd")


def _status(config: Any) -> dict[str, Any]:
    return {
        "state": "configured" if config.provider_enabled("rex") else "disabled",
        "capabilities": list(CAPABILITY_LIST),
        "live_readiness": False,
    }


def _rex(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapter import RexAdapter

        return RexAdapter(ctx, owners.rex)

    return provider_plugin(
        "rex",
        build,
        RexConfig,
        _rex,
        definition=DEFINITION,
        capabilities=CAPABILITIES,
        hooks=lambda: _STORE_HOOKS,
        reasons=REASONS,
        reason_order=REASON_ORDER,
        status_block=_status,
        status_order=40,
        verbs=rex_rpc(owners),
        commands=cli.rex,
        tools=TOOLS,
    )


def rex_plugin() -> ProviderPluginSpec:
    """Return the detached Rex provider composition."""
    return _rex(PluginOwners.detached())
