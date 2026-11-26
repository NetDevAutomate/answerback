"""Codex Desktop RPC verb implementations."""

import asyncio
from typing import Any

from ...discovery import matching_peers, search_terms
from ...plugin import OwnedRpc, PluginOwners, RpcHandler
from ...process import BridgeError


class CodexVerb(OwnedRpc):
    """One Codex-owned RPC handler bound to live owners."""


def rpc(owners: PluginOwners) -> dict[str, RpcHandler]:
    """Bind Codex RPC verbs to the live service-owned resources."""

    async def desktop_list() -> dict[str, Any]:
        service = owners.service()
        found = await service.peer_search(provider="desktop")
        return {"desktop_threads": service.config.desktop_threads, **found}

    async def desktop_sessions(
        query: str = "", cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        service = owners.service()
        terms = search_terms(query)
        if not service.config.provider_enabled("codex"):
            raise ValueError("Codex provider is disabled")
        page = await owners.desktop().page(cursor, limit)
        peers = page.get("peers")
        if not isinstance(peers, list) or len(peers) > min(limit, 100):
            raise BridgeError("Codex discovery returned an unsupported page")

        def eligible() -> list[dict[str, Any]]:
            selected = matching_peers(peers, terms) if terms else peers
            return [
                peer
                for peer in selected
                if service.store.native_session_owner("codex_cli", peer["thread_id"]) is None
            ]

        return {
            **page,
            "peers": await asyncio.to_thread(eligible),
            "query_scope": "this page only; follow next_cursor for additional chats",
        }

    async def desktop_read(alias: str) -> dict[str, Any]:
        service = owners.service()
        from .history import HistoryReader, user_text

        if not service.config.provider_enabled("codex"):
            raise ValueError("Codex provider is disabled")
        thread_id = service.config.thread(alias)
        async with HistoryReader(service.config.codex_binary, service.config.codex_home) as reader:
            turns = await reader.turns(thread_id, pages=1)
        return {
            "alias": alias,
            "thread_id": thread_id,
            "history": [
                {
                    "turn_id": turn["id"],
                    "status": turn.get("status"),
                    "messages": [
                        {
                            "type": item["type"],
                            "text": (
                                user_text(item)
                                if item["type"] == "userMessage"
                                else item.get("text", "")
                            )[:8000],
                            "phase": item.get("phase"),
                        }
                        for item in turn.get("items", [])
                        if item.get("type") in ("userMessage", "agentMessage")
                    ],
                }
                for turn in turns[:5]
            ],
            "note": (
                "Newest five persisted turns; messages capped at 8000 characters. "
                "Live runtime status is unavailable."
            ),
        }

    async def desktop_submit(alias: str, message: str, request_id: str) -> dict[str, Any]:
        service = owners.service()
        return await service._desktop_submit(
            service.config.thread(alias), message, request_id, alias
        )

    return {
        "desktop_list": CodexVerb(owners, desktop_list),
        "desktop_sessions": CodexVerb(owners, desktop_sessions, "codex"),
        "desktop_read": CodexVerb(owners, desktop_read, "codex"),
        "desktop_submit": CodexVerb(owners, desktop_submit, "codex"),
    }
