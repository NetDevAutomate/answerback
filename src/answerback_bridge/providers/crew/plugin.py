"""KiroCrew provider definition and composition."""

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
from .config import CrewConfig
from .reasons import REASON_ORDER, REASONS
from .tools import TOOLS
from .verbs import rpc as crew_rpc

DEFINITION = ProviderDefinition(
    "crew",
    "crew",
    order=50,
    worker_reports=True,
    routing_fields=("crew.binary", "crew.enabled", "crew.port"),
    digest_keys=(
        ("crew.binary", "crew_binary"),
        ("crew.enabled", "crew_enabled"),
        ("crew.port", "crew_port"),
    ),
)
CAPABILITY_LIST = ("discovery", "send", "read", "worker_report")
CAPABILITIES = frozenset(CAPABILITY_LIST)
AGENT_PROGRAMS = {"kirocrew": "kirocrew"}
_STORE_HOOKS = ProjectPathHooks("peer", "project")


def _status(config: Any) -> dict[str, Any]:
    return {
        "state": "configured" if config.provider_enabled("crew") else "unconfigured",
        "capabilities": list(CAPABILITY_LIST),
    }


async def _crew_tail(service: Any, target: str, resources: Any) -> str | None:
    peer_id = f"crew:{target}"
    detail = resources.crew_details.get(peer_id)
    debug = getattr(service, "debug", None)
    if debug is not None and debug.until(peer_id) is not None:
        detail = None
    if detail is None:
        detail = await service.crew_read(target)
    messages = detail.get("messages") or []
    return "\n".join(str(row.get("content", "")) for row in messages[-3:])


def _crew(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        from .adapter import CrewAdapter

        return CrewAdapter(ctx, owners.crew)

    return provider_plugin(
        "crew",
        build,
        CrewConfig,
        _crew,
        definition=DEFINITION,
        capabilities=CAPABILITIES,
        hooks=lambda: _STORE_HOOKS,
        programs=AGENT_PROGRAMS,
        reasons=REASONS,
        reason_order=REASON_ORDER,
        status_block=_status,
        status_name="kirocrew",
        status_order=90,
        verbs=crew_rpc(owners),
        commands=cli.crew,
        tools=TOOLS,
        sources=(OwnedEventSource("crew", owners, _crew_tail),),
    )


def crew_plugin() -> ProviderPluginSpec:
    """Return the detached KiroCrew provider composition."""
    return _crew(PluginOwners.detached())
