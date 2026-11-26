from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config


class HerdrPeers:
    async def call(self, *args: str) -> dict[str, Any]:
        return {
            "agents": [
                {
                    "name": "kiro-reviewer",
                    "pane_id": "w1:p2",
                    "agent_status": "idle",
                    "cwd": "/work/payments",
                    "agent_kind": "kiro",
                },
                {
                    "name": "codex-payments",
                    "pane_id": "w1:p3",
                    "agent_status": "working",
                    "cwd": "/work/payments",
                    "agent_kind": "codex",
                },
            ]
        }


async def test_keywords_find_existing_and_dedicated_sessions(tmp_path: Path) -> None:
    from codex_herdr_bridge.discovery import Directory, SelectionRequired

    config = Config("test", kiro_workspaces={"billing": "/work/billing"})
    directory = Directory(config, HerdrPeers(), None)
    results = await directory.search("payments kiro")
    assert [peer["peer_id"] for peer in results["peers"]] == ["herdr:kiro-reviewer"]
    with pytest.raises(SelectionRequired):
        await directory.resolve("billing")
    dedicated = await directory.resolve("kiro:billing")
    assert dedicated["peer_id"] == "kiro:billing"
    assert dedicated["project"] == "/work/billing"


async def test_ambiguous_keyword_returns_choices_without_selecting(tmp_path: Path) -> None:
    from codex_herdr_bridge.discovery import Directory, SelectionRequired

    directory = Directory(Config("test"), HerdrPeers(), None)
    with pytest.raises(SelectionRequired) as error:
        await directory.resolve("payments")
    assert len(error.value.matches) == 2
    selected = await directory.resolve("herdr:kiro-reviewer")
    assert selected["target"] == "kiro-reviewer"


async def test_unavailable_provider_cannot_make_a_global_keyword_falsely_unique(
    tmp_path: Path,
) -> None:
    from codex_herdr_bridge.discovery import Directory, SelectionRequired
    from codex_herdr_bridge.process import BridgeError

    class MissingHerdr:
        async def call(self, *args: str) -> dict[str, Any]:
            raise BridgeError("Herdr unavailable")

    directory = Directory(
        Config("test", kiro_workspaces={"reviewer": "/work/review"}), MissingHerdr(), None
    )
    with pytest.raises(SelectionRequired):
        await directory.resolve("reviewer")
    assert (await directory.resolve("kiro:reviewer"))["target"] == "reviewer"


async def test_unnamed_herdr_agent_is_discovered_by_native_title_kind_and_foreground_path() -> None:
    from codex_herdr_bridge.discovery import Directory

    class NativeHerdr:
        async def call(self, *args: str) -> dict[str, Any]:
            return {
                "agents": [
                    {
                        "terminal_id": "term-native",
                        "agent": "kiro",
                        "title": "Payments review",
                        "terminal_title": "Reviewing payment code",
                        "terminal_title_stripped": "Payments review",
                        "display_agent": "Kiro",
                        "agent_status": "idle",
                        "workspace_id": "w2",
                        "tab_id": "w2:t1",
                        "pane_id": "w2:p7",
                        "focused": False,
                        "state_change_seq": 7,
                        "revision": 10,
                        "cwd": "/work",
                        "foreground_cwd": "/work/payments",
                    }
                ]
            }

    directory = Directory(Config("test"), NativeHerdr(), None)
    selected = await directory.resolve("payments kiro")
    assert selected["peer_id"] == "herdr:w2:p7"
    assert selected["project"] == "/work/payments"
    assert (await directory.resolve("Payments review"))["target"] == "w2:p7"


@pytest.mark.parametrize("field", ["title", "terminal_title", "terminal_title_stripped"])
async def test_named_herdr_agent_can_be_found_by_each_native_title(field: str) -> None:
    from codex_herdr_bridge.discovery import Directory

    class NativeHerdr:
        async def call(self, *args: str) -> dict[str, Any]:
            return {
                "agents": [
                    {
                        "name": "worker",
                        "pane_id": "w1:p1",
                        "terminal_id": "term-1",
                        "agent": "kiro",
                        "agent_status": "idle",
                        field: "Unique project discussion",
                    }
                ]
            }

    peer = await Directory(Config("test"), NativeHerdr(), None).resolve("Unique discussion")
    assert peer["target"] == "worker"


async def test_unbound_peer_requires_canonical_id_after_unique_text_match() -> None:
    """red_at_76db6d7a: unique Desktop title text selected an unbound chat."""
    from codex_herdr_bridge.discovery import Directory, SelectionRequired

    peer = {
        "peer_id": "desktop:22222222-2222-2222-2222-222222222222",
        "provider": "desktop",
        "target": "22222222-2222-2222-2222-222222222222",
        "thread_id": "22222222-2222-2222-2222-222222222222",
        "name": "Quarterly audit report",
        "title": "Quarterly audit report",
        "preview": "finish the quarterly audit report",
        "project": "/work/reports",
        "kind": "discovered_codex",
    }

    class Desktop:
        async def inventory(self) -> dict[str, Any]:
            return {"peers": [peer], "complete": True}

        async def read(self, ident: str) -> dict[str, Any]:
            assert ident == peer["target"]
            return peer

    directory = Directory(
        Config("test", codex_enabled=True, provider_selection=["codex"]),
        None,
        None,
        Desktop(),
    )
    with pytest.raises(SelectionRequired) as error:
        await directory.resolve("finish quarterly audit")
    assert error.value.matches == [peer]
    assert await directory.resolve(peer["peer_id"]) == peer
