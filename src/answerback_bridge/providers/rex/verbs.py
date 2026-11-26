"""Rex RPC verb implementations."""

from typing import Any

from ...plugin import OwnedRpc, PluginOwners, RpcHandler


class RexVerb(OwnedRpc):
    """One Rex-owned RPC handler bound to live owners."""


def rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind Rex RPC verbs to the live service-owned Rex client."""

    async def rex_configure(binary: str = "rex", server: str | None = None) -> dict[str, Any]:
        return await owners.rex().configure(binary, server)

    async def rex_read(session_id: str, block_id: str, max_bytes: int = 16384) -> dict[str, Any]:
        return await owners.rex().read(session_id, block_id, max_bytes)

    async def rex_enroll(
        name: str, session_id: str, block_id: str, request_id: str
    ) -> dict[str, Any]:
        return await owners.rex().enroll(name, session_id, block_id, request_id)

    async def rex_receive(peer_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        return await owners.rex().receive(peer_id, wait_seconds)

    async def rex_ack(peer_id: str, task_id: str) -> dict[str, Any]:
        return await owners.rex().ack(peer_id, task_id)

    async def rex_complete(peer_id: str, task_id: str, result: str) -> dict[str, Any]:
        return await owners.rex().complete(peer_id, task_id, result)

    async def rex_retire(peer_id: str) -> dict[str, Any]:
        return await owners.rex().retire(peer_id)

    return {
        "rex_configure": RexVerb(owners, rex_configure),
        "rex_read": RexVerb(owners, rex_read),
        "rex_enroll": RexVerb(owners, rex_enroll),
        "rex_receive": RexVerb(owners, rex_receive),
        "rex_ack": RexVerb(owners, rex_ack),
        "rex_complete": RexVerb(owners, rex_complete),
        "rex_retire": RexVerb(owners, rex_retire),
    }
