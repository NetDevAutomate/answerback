"""Herdr App/core and Herdr TUI provider definitions and composition."""

from typing import Any

from ...adapters.catalog import ProviderDefinition
from ...adapters.contracts import ProviderAdapter
from ...plugin import (
    KernelContext,
    OwnedEventSource,
    PluginOwners,
    ProjectPathHooks,
    ProviderPluginSpec,
    provider_plugin,
)
from . import cli
from .config import HcConfig, HerdrConfig
from .reasons import (
    HC_REASON_ORDER,
    HC_REASONS,
    HERDR_REASON_ORDER,
    HERDR_REASONS,
)
from .tools import HC_TOOLS, HERDR_TOOLS
from .verbs import hc_rpc, herdr_rpc

HERDR_DEFINITION = ProviderDefinition(
    "herdr",
    "herdr",
    order=20,
    worker_reports=True,
    routing_fields=("herdr.session", "herdr.binary"),
    digest_keys=(("herdr.session", "herdr_session"), ("herdr.binary", "herdr_binary")),
)
HC_DEFINITION = ProviderDefinition(
    "hc",
    "hc",
    order=10,
    worker_reports=True,
    verified_delivery_first=True,
    routing_fields=("hc.enabled", "hc.binary", "hc.session"),
    digest_keys=(
        ("hc.enabled", "hc_enabled"),
        ("hc.binary", "hc_binary"),
        ("hc.session", "hc_session"),
    ),
)
HERDR_CAPABILITY_LIST = ("send", "read", "list", "result")
HERDR_CAPABILITIES = frozenset(HERDR_CAPABILITY_LIST)
HC_CAPABILITY_LIST = ("discovery", "read", "send", "worker_report")
HC_CAPABILITIES = frozenset(HC_CAPABILITY_LIST)
_HC_STORE_HOOKS = ProjectPathHooks("peer", "project")


def _herdr_status(config: Any) -> dict[str, Any]:
    return {
        "state": "unchecked" if config.provider_enabled("herdr") else "disabled",
        "capabilities": list(HERDR_CAPABILITY_LIST),
    }


def _hc_status(config: Any) -> dict[str, Any]:
    return {
        "state": "configured" if config.provider_enabled("hc") else "disabled",
        "capabilities": list(HC_CAPABILITY_LIST),
    }


async def _hc_tail(service: Any, target: str, resources: Any) -> str | None:
    return (await service.hc_read(target, 40))["text"]


def _herdr(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapters.herdr import HerdrAdapter

        return HerdrAdapter(ctx, owners.herdr)

    return provider_plugin(
        "herdr",
        build,
        HerdrConfig,
        _herdr,
        definition=HERDR_DEFINITION,
        capabilities=HERDR_CAPABILITIES,
        reasons=HERDR_REASONS,
        reason_order=HERDR_REASON_ORDER,
        status_block=_herdr_status,
        status_order=60,
        verbs=herdr_rpc(owners),
        commands=cli.herdr,
        tools=HERDR_TOOLS,
    )


def _hc(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapters.core import CoreAdapter

        return CoreAdapter(ctx, owners.core, owners.core_tasks, owners.core_lock())

    return provider_plugin(
        "hc",
        build,
        HcConfig,
        _hc,
        definition=HC_DEFINITION,
        capabilities=HC_CAPABILITIES,
        hooks=lambda: _HC_STORE_HOOKS,
        reasons=HC_REASONS,
        reason_order=HC_REASON_ORDER,
        status_block=_hc_status,
        status_order=50,
        verbs=hc_rpc(owners),
        commands=cli.hc,
        tools=HC_TOOLS,
        sources=(OwnedEventSource("hc", owners, _hc_tail),),
    )


def hc_plugin() -> ProviderPluginSpec:
    """Return the detached Herdr App/core provider composition."""
    return _hc(PluginOwners.detached())


def herdr_plugin() -> ProviderPluginSpec:
    """Return the detached Herdr TUI provider composition."""
    return _herdr(PluginOwners.detached())
