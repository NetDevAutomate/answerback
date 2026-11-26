"""Read-only discovery of locally indexed Codex Desktop/IDE conversations."""

import asyncio
import time
import uuid
from typing import Any

from ...process import BridgeError
from . import history

SOURCES = ["vscode", "appServer"]


def peer_from_thread(thread: dict[str, Any]) -> dict[str, Any] | None:
    if (
        thread.get("source") not in SOURCES
        or thread.get("ephemeral") is True
        or thread.get("canAcceptDirectInput") is False
        or thread.get("parentThreadId")
        or thread.get("originator") == "answerback-native-cli"
    ):
        return None
    try:
        ident = str(uuid.UUID(thread["id"]))
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise BridgeError("Codex discovery returned an invalid chat identity") from error
    title = thread.get("name") or thread.get("preview") or f"Codex chat {ident}"
    cwd = thread.get("cwd") or ""
    preview = thread.get("preview") or ""
    if not isinstance(title, str) or not isinstance(cwd, str) or not isinstance(preview, str):
        raise BridgeError("Codex discovery returned invalid chat metadata")
    status = thread.get("status")
    return {
        "peer_id": f"desktop:{ident}",
        "provider": "desktop",
        "target": ident,
        "thread_id": ident,
        "name": title[:1000],
        "title": title[:1000],
        "preview": preview[:1000],
        "project": cwd[:4096],
        "agent_kind": "codex",
        "kind": "discovered_codex",
        "application": "Codex ChatGPT Desktop IDE",
        "source": thread["source"],
        "originator": str(thread.get("originator") or "")[:200],
        "capabilities": ["queue", "persisted_result"],
        # The disposable reader is not the Desktop app's execution owner.
        "live_status": "unknown",
        "reported_status": status.get("type", "unknown") if isinstance(status, dict) else "unknown",
        "metadata_truncated": len(title) > 1000 or len(cwd) > 4096 or len(preview) > 1000,
    }


class DesktopIndex:
    def __init__(self, binary: str, home: str | None = None, *, max_pages: int = 5) -> None:
        if not 1 <= max_pages <= 10:
            raise ValueError("Discovery page budget must be between 1 and 10")
        self.binary = binary
        self.home = home
        self.max_pages = max_pages
        self.lock = asyncio.Lock()
        self.cached: dict[str, Any] | None = None
        self.expires = 0.0

    async def _page(
        self, reader: history.HistoryReader, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        result = await reader.request(
            "thread/list",
            {
                "cursor": cursor,
                "limit": limit,
                "sortKey": "updated_at",
                "sourceKinds": SOURCES,
                "modelProviders": [],
                "archived": False,
                "useStateDbOnly": True,
            },
        )
        data = result.get("data")
        following = result.get("nextCursor")
        if not isinstance(data, list) or len(data) > limit:
            raise BridgeError("Codex discovery returned an unsupported page")
        if following is not None and (not isinstance(following, str) or not following):
            raise BridgeError("Codex discovery returned an invalid cursor")
        peers = []
        for item in data:
            if not isinstance(item, dict):
                raise BridgeError("Codex discovery returned invalid metadata")
            peer = peer_from_thread(item)
            if peer is not None:
                peers.append(peer)
        return {
            "peers": peers,
            "next_cursor": following,
            "complete": following is None and not any(p["metadata_truncated"] for p in peers),
            "scope": "locally indexed non-archived Desktop/IDE chats; metadata only",
            "live_status": "unknown",
            "rows_examined": len(data),
        }

    async def page(self, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 100 or (cursor is not None and not isinstance(cursor, str)):
            raise ValueError("Use limit 1–100 and an optional cursor string")
        async with asyncio.timeout(26), history.HistoryReader(self.binary, self.home) as reader:
            return await self._page(reader, cursor, limit)

    async def inventory(self) -> dict[str, Any]:
        async with self.lock:
            if self.cached is not None and time.monotonic() < self.expires:
                return self.cached
            async with asyncio.timeout(26), history.HistoryReader(self.binary, self.home) as reader:
                cursor = None
                seen = set()
                peers: dict[str, dict[str, Any]] = {}
                examined = 0
                truncated = False
                for _ in range(self.max_pages):
                    page = await self._page(reader, cursor, 100)
                    for peer in page["peers"]:
                        peers.setdefault(peer["thread_id"], peer)
                        truncated |= peer["metadata_truncated"]
                    examined += page["rows_examined"]
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise BridgeError("Codex discovery repeated a pagination cursor")
                    seen.add(cursor)
                self.cached = {
                    "peers": list(peers.values()),
                    "next_cursor": cursor,
                    "complete": cursor is None and not truncated,
                    "rows_examined": examined,
                    "scope": page["scope"],
                    "live_status": "unknown",
                }
                self.expires = time.monotonic() + 10
                return self.cached

    async def read(self, ident: str) -> dict[str, Any]:
        ident = str(uuid.UUID(ident))
        async with asyncio.timeout(26), history.HistoryReader(self.binary, self.home) as reader:
            value = await reader.request("thread/read", {"threadId": ident, "includeTurns": False})
        thread = value.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != ident:
            raise BridgeError("Codex returned a different chat identity")
        peer = peer_from_thread(thread)
        if peer is None:
            raise BridgeError("This is not an eligible Desktop/IDE conversation")
        return peer
