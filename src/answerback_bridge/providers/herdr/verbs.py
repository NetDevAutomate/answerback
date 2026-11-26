"""Herdr App/core and Herdr TUI RPC verb implementations."""

import shutil
import time
from dataclasses import replace
from typing import Any

from ...plugin import OwnedRpc, PluginOwners, RpcHandler
from ...process import BridgeError


class HerdrVerb(OwnedRpc):
    """One Herdr-owned RPC handler bound to live owners."""


def herdr_rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind Herdr TUI RPC verbs to the live service-owned client."""

    async def herdr_list_agents() -> dict[str, Any]:
        return await owners.herdr().call("agent", "list")

    async def herdr_read_agent(target: str, lines: int = 100) -> dict[str, Any]:
        return await owners.herdr().read(target, lines)

    return {
        "herdr_list_agents": HerdrVerb(owners, herdr_list_agents, "herdr"),
        "herdr_read_agent": HerdrVerb(owners, herdr_read_agent, "herdr"),
    }


def hc_rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind Herdr App/core RPC verbs to the live service-owned resources."""

    async def hc_configure(binary: str = "hc", session: str | None = None) -> dict[str, Any]:
        service = owners.service()
        from .core import Core

        resolved = shutil.which(binary)
        if resolved is None:
            raise ValueError("herdr-core executable is unavailable")
        # Validate the proposal without snapshotting unrelated settings across the inventory await.
        replace(service.config, hc_binary=resolved, hc_session=session, hc_enabled=True)
        core = Core(
            resolved,
            session,
            send_to_done=service.config.hc_send_to_done,
            send_machines=service.config.hc_send_machines,
            events=service.config.hc_events,
            agents=service.agent_programs,
        )
        inventory = await core.inventory()
        if "inventory" in inventory["errors"]:
            raise BridgeError("Core connection is unverified: " + inventory["errors"]["inventory"])
        async with owners.core_lock():
            if (service.config.hc_binary, service.config.hc_session) != (resolved, session):
                with service.store.connection() as db:
                    active = db.execute(
                        "SELECT count(*) FROM tasks WHERE json_extract(metadata, '$.kind')='hc' "
                        "AND status NOT IN ('completed','failed','abandoned','cancelled')"
                    ).fetchone()[0]
                if active:
                    raise ValueError(
                        "Reconcile unfinished core tasks before changing the core connection"
                    )
            candidate = replace(
                service.config, hc_binary=resolved, hc_session=session, hc_enabled=True
            )
            candidate.select_provider("hc", True)
            candidate.save(service.config_path)
            service.config.hc_binary = resolved
            service.config.hc_session = session
            service.config.hc_enabled = True
            service.config.provider_selection = candidate.provider_selection
            service._replace_core(core)
            service.provider_status["hc"].update(state="connected", checked_at=time.time())
        return {"configured": True, "provider": "hc", **inventory}

    async def hc_read(target: str, lines: int = 100) -> dict[str, Any]:
        service = owners.service()
        if not service.config.provider_enabled("hc"):
            raise ValueError("Core is not configured; run hc-configure first")
        return await owners.core().read(target, lines)

    return {
        "hc_configure": HerdrVerb(owners, hc_configure),
        "hc_read": HerdrVerb(owners, hc_read, "hc"),
    }
