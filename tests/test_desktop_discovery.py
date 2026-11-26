import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from codex_herdr_bridge import codex
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.discovery import Directory, SelectionRequired
from codex_herdr_bridge.process import BridgeError, Output
from codex_herdr_bridge.service import Bridge

CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff"


def thread(ident: str = CHAT, **fields: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "name": "Herdr bridge collaboration",
        "cwd": "/work/herdr",
        "source": "vscode",
        "originator": "codex_work_desktop",
        "ephemeral": False,
        "modelProvider": "bedrock",
        "status": {"type": "notLoaded"},
        "updatedAt": 100,
        **fields,
    }


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> Any:
    class Reader:
        calls: list[tuple[str, dict[str, Any]]] = []
        pages: dict[str | None, dict[str, Any]] = {
            None: {"data": [thread()], "nextCursor": None},
        }
        current: dict[str, Any] = thread()
        failure = False

        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> "Reader":
            if self.failure:
                raise BridgeError("discovery unavailable")
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "thread/list":
                return self.pages[params.get("cursor")]
            assert method == "thread/read"
            return {"thread": self.current}

    monkeypatch.setattr("codex_herdr_bridge.codex.HistoryReader", Reader)
    return Reader


class EmptyHerdr:
    async def call(self, *_: str) -> dict[str, Any]:
        return {"agents": []}


async def test_bridge_finds_unpaired_desktop_chat_by_name_and_project(
    history: Any, tmp_path: Path
) -> None:
    bridge = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    try:
        result = await bridge.peer_search("herdr collaboration", "desktop")
        assert result["peers"], "Unpaired Desktop chats must be discoverable"
        peer = result["peers"][0]
        assert peer["peer_id"] == f"desktop:{CHAT}"
        assert peer["project"] == "/work/herdr"
        assert peer["live_status"] == "unknown"
        assert peer["reported_status"] == "notLoaded"
        assert bridge.config.desktop_threads == {}
        assert not (tmp_path / "config.json").exists()
        assert (await bridge.peer_search("ChatGPT", "desktop"))["peers"]
        method, params = history.calls[0]
        assert method == "thread/list"
        assert params["sourceKinds"] == ["vscode", "appServer"]
        assert params["modelProviders"] == []
        assert params["archived"] is False
        assert params["useStateDbOnly"] is True
    finally:
        await bridge.close()


async def test_index_pages_deduplicates_and_omits_terminal_and_ephemeral_threads(
    history: Any,
) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {
        None: {"data": [thread(), thread(OTHER, source="cli")], "nextCursor": "next"},
        "next": {"data": [thread(), thread(OTHER, ephemeral=True)], "nextCursor": None},
    }
    result = await DesktopIndex("codex").inventory()
    assert [p["thread_id"] for p in result["peers"]] == [CHAT]
    assert result["complete"] is True
    assert len(history.calls) == 2


async def test_partial_inventory_refuses_keyword_but_exact_id_can_be_revalidated(
    history: Any,
) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {None: {"data": [thread()], "nextCursor": "next"}}
    index = DesktopIndex("codex", max_pages=1)
    directory = Directory(Config("test", codex_enabled=True), EmptyHerdr(), None, index)
    with pytest.raises(SelectionRequired):
        await directory.resolve("collaboration", "desktop")
    selected = await directory.resolve(f"desktop:{CHAT}")
    assert selected["thread_id"] == CHAT
    assert history.calls[-1][0] == "thread/read"


async def test_failure_is_not_reported_as_empty_complete_discovery(
    history: Any, tmp_path: Path
) -> None:
    history.failure = True
    bridge = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    try:
        result = await bridge.peer_search(provider="desktop")
        assert result["complete"] is False
        assert "desktop" in result["errors"]
    finally:
        await bridge.close()


async def test_paired_alias_enriches_without_duplicate_discovered_peer(
    history: Any, tmp_path: Path
) -> None:
    bridge = Bridge(
        Config("test", codex_enabled=True, desktop_threads={"author": CHAT}),
        tmp_path / "config.json",
    )
    try:
        peers = (await bridge.peer_search("collaboration", "desktop"))["peers"]
        assert len(peers) == 1
        assert peers[0]["peer_id"] == "desktop:author"
        assert peers[0]["thread_id"] == CHAT
    finally:
        await bridge.close()


async def test_canonical_desktop_identity_is_rechecked_and_cli_identity_rejected(
    history: Any,
) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.current = thread(source="cli")
    with pytest.raises(BridgeError):
        await DesktopIndex("codex").read(CHAT)
    history.current = thread(OTHER)
    with pytest.raises(BridgeError):
        await DesktopIndex("codex").read(CHAT)


async def test_discovered_submission_uses_receipt_identity_without_creating_pairing(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends = []

    async def queue(binary: str, ident: str, task: str, message: str, home: str | None) -> str:
        sends.append((ident, task, message))
        return "queued"

    monkeypatch.setattr("codex_herdr_bridge.codex.queue", queue)
    bridge = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    try:
        sent = await bridge.peer_submit(f"desktop:{CHAT}", "Review", "request-1")
        again = await bridge.peer_submit(f"desktop:{CHAT}", "Review", "request-1")
        assert sent["status"] == "queued"
        assert again["id"] == sent["id"]
        assert sent["metadata"]["thread_id"] == CHAT
        assert sent["metadata"]["binding"] == "discovered"
        assert len(sends) == 1
        assert bridge.config.desktop_threads == {}
        assert not (tmp_path / "config.json").exists()
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"source": "cli"}, id="source"),
        pytest.param({"ephemeral": True}, id="ephemeral"),
        pytest.param({"canAcceptDirectInput": False}, id="direct-input"),
        pytest.param({"parentThreadId": OTHER}, id="child-thread"),
    ],
)
async def test_paired_submit_rechecks_the_same_eligibility_as_peer_submit(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any]
) -> None:
    """red_at_base: an alias must not bypass the current thread's eligibility."""
    history.current = thread(**fields)
    send = AsyncMock(return_value="queued")
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge = Bridge(
        Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT}),
        tmp_path / "config.json",
    )
    try:
        with pytest.raises(BridgeError, match="eligible Desktop/IDE"):
            await bridge.peer_submit("desktop:reviewer", "Review", "peer-refused")
        result = await bridge.desktop_submit("reviewer", "Review", "alias-refused")
        assert result["status"] == "failed"
        assert result["detail"] == "This is not an eligible Desktop/IDE conversation"
        assert bridge.store.get(result["id"])["status"] == "failed"
        send.assert_not_awaited()
    finally:
        await bridge.close()


@pytest.mark.parametrize("source", ["vscode", "appServer"])
async def test_eligible_paired_submit_and_retry_keep_one_queue_delivery(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """pins_existing: valid aliases queue once and retry without native access."""
    history.current = thread(source=source, canAcceptDirectInput=True)
    send = AsyncMock(return_value="queued")
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge = Bridge(
        Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT}),
        tmp_path / "config.json",
    )
    try:
        first = await bridge.desktop_submit("reviewer", "Review", "alias-retry")
        assert first["status"] == "queued"
        assert first["metadata"]["alias"] == "reviewer"
        assert first["metadata"]["thread_id"] == CHAT
        history.failure = True
        assert await bridge.desktop_submit("reviewer", "Review", "alias-retry") == first
        send.assert_awaited_once()
        assert history.calls == [("thread/read", {"threadId": CHAT, "includeTurns": False})]
    finally:
        await bridge.close()


async def test_paired_submit_still_refuses_native_cli_origin(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: a native CLI origin is excluded even without stored ownership."""
    history.current = thread(originator="answerback-native-cli")
    send = AsyncMock(return_value="queued")
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge = Bridge(
        Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT}),
        tmp_path / "config.json",
    )
    try:
        result = await bridge.desktop_submit("reviewer", "Review", "cli-origin")
        assert result["status"] == "failed"
        send.assert_not_awaited()
    finally:
        await bridge.close()


async def test_queue_nul_failure_is_recorded_on_the_reserved_receipt(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: real argv rejection must leave a diagnosable receipt."""
    monkeypatch.setattr(codex, "codex_env", lambda _: {})
    binary = str(tmp_path / "missing-codex")
    message = "queue fixture\x00message tail"
    with pytest.raises(ValueError, match="embedded null byte"):
        await codex.queue(binary, CHAT, "fixture-task", message)
    bridge = Bridge(
        Config(
            provider_selection=["codex"],
            desktop_threads={"reviewer": CHAT},
            codex_binary=binary,
        ),
        tmp_path / "config.json",
    )
    try:
        result = await bridge.desktop_submit("reviewer", message, "nul-queue")
        assert result["status"] == "uncertain"
        assert result["detail"] == "embedded null byte"
        assert message not in result["detail"]
        assert "message tail" not in result["detail"]
        persisted = bridge.store.get(result["id"])
        assert persisted["status"] == "uncertain"
        assert persisted["detail"] == result["detail"]
        history.failure = True
        assert await bridge.desktop_submit("reviewer", message, "nul-queue") == result
    finally:
        await bridge.close()


async def test_queue_missing_executable_still_records_uncertain_receipt(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: the OSError delivery path remains recorded on the task."""
    monkeypatch.setattr(codex, "codex_env", lambda _: {})
    bridge = Bridge(
        Config(
            provider_selection=["codex"],
            desktop_threads={"reviewer": CHAT},
            codex_binary=str(tmp_path / "missing-codex"),
        ),
        tmp_path / "config.json",
    )
    try:
        result = await bridge.desktop_submit("reviewer", "Review", "missing-queue")
        assert result["status"] == "uncertain"
        assert "No such file or directory" in result["detail"]
        assert bridge.store.get(result["id"])["detail"] == result["detail"]
    finally:
        await bridge.close()


async def test_queue_unrecognized_receipt_still_records_uncertain_delivery(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: BridgeError handling survives the added argument-error case."""
    monkeypatch.setattr(codex, "codex_env", lambda _: {})
    run = AsyncMock(return_value=Output("", ""))
    monkeypatch.setattr(codex, "run", run)
    bridge = Bridge(
        Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT}),
        tmp_path / "config.json",
    )
    try:
        result = await bridge.desktop_submit("reviewer", "Review", "unrecognized-queue")
        assert result["status"] == "uncertain"
        assert result["detail"] == "Codex did not return a recognizable queue receipt"
        assert bridge.store.get(result["id"])["detail"] == result["detail"]
        run.assert_awaited_once()
    finally:
        await bridge.close()


async def test_inventory_cache_coalesces_simultaneous_reads(history: Any) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    index = DesktopIndex("codex")
    a, b = await asyncio.gather(index.inventory(), index.inventory())
    assert a == b
    assert len(history.calls) == 1


async def test_page_cursor_is_exposed_and_does_not_read_conversation_items(history: Any) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {"page": {"data": [thread()], "nextCursor": "later"}}
    result = await DesktopIndex("codex").page("page", 10)
    assert result["next_cursor"] == "later"
    assert result["complete"] is False
    assert [m for m, _ in history.calls] == ["thread/list"]


async def test_repeated_cursor_fails_closed(history: Any) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {
        None: {"data": [thread()], "nextCursor": "same"},
        "same": {"data": [], "nextCursor": "same"},
    }
    with pytest.raises(BridgeError, match="cursor"):
        await DesktopIndex("codex").inventory()


async def test_paired_canonical_alias_does_not_depend_on_discovery_availability(
    history: Any,
) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.failure = True
    directory = Directory(
        Config("test", codex_enabled=True, desktop_threads={"author": CHAT}),
        EmptyHerdr(),
        None,
        DesktopIndex("codex"),
    )
    selected = await directory.resolve("desktop:author")
    assert selected["thread_id"] == CHAT
    assert selected["target"] == "author"


async def test_discovered_receipt_refresh_survives_restart_without_alias(
    history: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge import codex

    class CompletedHistory(history):
        async def scan(self, ident: str, **_: Any) -> Any:
            assert ident == CHAT
            return (
                [
                    {
                        "id": "turn",
                        "status": "completed",
                        # Started after the receipt was created (T-189 history binding).
                        "startedAt": 4_000_000_000,
                        "completedAt": 4_000_000_001,
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": codex.task_marker(task_id) + "\nReview",
                                    }
                                ],
                            },
                            {"type": "agentMessage", "phase": "final_answer", "text": "Done"},
                        ],
                    }
                ],
                None,
            )

    first = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    receipt, _ = first.store.reserve(
        f"desktop:{CHAT}",
        "Review",
        "persist",
        {
            "kind": "desktop",
            "binding": "discovered",
            "thread_id": CHAT,
        },
    )
    task_id = receipt["id"]
    first.store.update(task_id, "queued")
    await first.close()
    monkeypatch.setattr(codex, "HistoryReader", CompletedHistory)
    second = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    try:
        result = await second.get_task(task_id)
        assert result["status"] == "completed"
        assert result["result"] == "Done"
        found = await second.task_for_peer(f"desktop:{CHAT}")
        assert found["id"] == task_id
    finally:
        await second.close()


async def test_short_preview_keeps_discovery_useful_when_native_title_is_ellipsized(
    history: Any, tmp_path: Path
) -> None:
    history.pages = {
        None: {
            "data": [
                thread(
                    name="My machine locked up…",
                    preview="Continue the Python uv tool for a herdr bridge",
                )
            ],
            "nextCursor": None,
        }
    }
    bridge = Bridge(Config("test", codex_enabled=True), tmp_path / "config.json")
    try:
        result = await bridge.peer_search("python uv bridge", "desktop")
        assert [peer["thread_id"] for peer in result["peers"]] == [CHAT]
    finally:
        await bridge.close()


async def test_truncated_preview_cannot_hide_a_second_keyword_match(history: Any) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {
        None: {
            "data": [
                thread(name="First chat", preview="payment audit"),
                thread(
                    OTHER,
                    name="Second chat",
                    cwd="/elsewhere",
                    preview="x" * 1001 + " payment audit",
                ),
            ],
            "nextCursor": None,
        }
    }
    index = DesktopIndex("codex")
    directory = Directory(Config("test", codex_enabled=True), EmptyHerdr(), None, index)
    found = await directory.search("payment audit", "desktop")
    assert found["complete"] is False
    with pytest.raises(SelectionRequired):
        await directory.resolve("payment audit", "desktop")


async def test_partial_search_returns_continuation_cursor(history: Any) -> None:
    from codex_herdr_bridge.desktop import DesktopIndex

    history.pages = {None: {"data": [thread()], "nextCursor": "continue-after-this-page"}}
    directory = Directory(
        Config("test", codex_enabled=True), EmptyHerdr(), None, DesktopIndex("codex", max_pages=1)
    )
    result = await directory.search(provider="desktop")
    assert result["cursors"]["desktop"] == "continue-after-this-page"
