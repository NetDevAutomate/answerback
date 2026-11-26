"""Kiro RPC verb implementations."""

from typing import Any, cast

from ...config import public_task
from ...plugin import OwnedRpc, PluginOwners, RpcHandler
from ...transport import private_directory, runtime_dir


class KiroVerb(OwnedRpc):
    """One Kiro-owned RPC handler bound to live owners."""


def rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind Kiro RPC verbs to the live service-owned resources."""

    async def kiro_register(alias: str, cwd: str, binary: str = "kiro-cli") -> dict[str, Any]:
        return await owners.service().workspace_add(alias, cwd, binary=binary)

    async def kiro_submit(alias: str, message: str, request_id: str) -> dict[str, Any]:
        service = owners.service()
        from .adapter import KiroAdapter

        receipt = await cast(KiroAdapter, service.adapters.get("kiro")).submit_alias(
            alias, message, request_id, guard=service.admission_guard("kiro")
        )
        return public_task(receipt.as_record())

    async def kiro_sessions(
        query: str = "", cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        service = owners.service()
        from . import acp

        words = query.casefold().split(maxsplit=32)
        if len(words) > 32:
            raise ValueError("query must contain at most 32 search terms")

        async def update(event: dict[str, Any]) -> None:
            pass

        async def deny(request: dict[str, Any]) -> None:
            return None

        # This read-only listing must never start kiro-cli in a caller-chosen directory:
        # the process cwd is always the bridge's own runtime directory. The caller's cwd
        # is passed on only as the session/list filter, exactly as before.
        directory = runtime_dir(service.config_path)
        private_directory(directory)
        async with acp.KiroACP(service.config.kiro_binary, str(directory), update, deny) as client:
            result = await client.list_sessions(cwd=cwd, cursor=cursor)
        records = result.get("sessions", [])
        selected = []
        for item in records:
            if words:
                haystack = (str(item.get("title", "")) + " " + str(item.get("cwd", ""))).casefold()
                if not all(word in haystack for word in words):
                    continue
            selected.append(item)
        return {
            "sessions": selected,
            "next_cursor": result.get("nextCursor"),
            "note": "Saved sessions; listing does not establish ownership of a live TUI",
        }

    return {
        "kiro_register": KiroVerb(owners, kiro_register),
        "kiro_submit": KiroVerb(owners, kiro_submit, "kiro"),
        "kiro_sessions": KiroVerb(owners, kiro_sessions, "kiro"),
    }
