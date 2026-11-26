"""KiroCrew RPC verb implementations."""

import shutil
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from ...discovery import SelectionRequired
from ...plugin import OwnedRpc, PluginOwners, RpcHandler
from ...process import BridgeError


class CrewVerb(OwnedRpc):
    """One KiroCrew-owned RPC handler bound to live owners."""


def rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind KiroCrew RPC verbs to the live service-owned resources."""

    async def crew_configure(binary: str = "kirocrew", port: int | None = None) -> dict[str, Any]:
        service = owners.service()
        resolved = shutil.which(binary)
        if resolved is None:
            raise ValueError("Kirocrew executable is unavailable")
        candidate = replace(service.config, crew_binary=resolved, crew_port=port, crew_enabled=True)
        candidate.select_provider("crew", True)
        candidate.save(service.config_path)
        service.config.crew_binary = candidate.crew_binary
        service.config.crew_port = candidate.crew_port
        service.config.crew_enabled = True
        service.config.provider_selection = candidate.provider_selection
        service._crew_client = None
        service.provider_status["kirocrew"].update(state="configured", checked_at=time.time())
        client = owners.crew()
        try:
            peers = await client.peers()
        except (BridgeError, OSError, ValueError, TimeoutError):
            service.provider_status["kirocrew"].update(state="unavailable", checked_at=time.time())
            service._publish_crew_version()
            raise
        origin = getattr(client, "origin", None)
        if port is None and service._crew_client is client and isinstance(origin, str):
            learned_port = urlsplit(origin).port
            if learned_port is not None:
                candidate = replace(service.config, crew_port=learned_port)
                candidate.save(service.config_path)
                service.config.crew_port = learned_port
                client.port = learned_port
        service.provider_status["kirocrew"].update(state="connected", checked_at=time.time())
        service._publish_crew_version()
        return {"configured": True, "provider": "crew", "peers": peers}

    async def crew_create(title: str, project: str, request_id: str) -> dict[str, Any]:
        try:
            return await owners.crew().create(title, project, request_id)
        except SelectionRequired as error:
            return {"status": "needs_selection", "matches": error.matches, "errors": error.errors}

    async def crew_read(target: str) -> dict[str, Any]:
        return await owners.crew().detail(target)

    return {
        "crew_configure": CrewVerb(owners, crew_configure),
        "crew_create": CrewVerb(owners, crew_create, "crew"),
        "crew_read": CrewVerb(owners, crew_read, "crew"),
    }
