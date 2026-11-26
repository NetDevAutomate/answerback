"""T-78: Crew receipt lookup follows the discovered chat binding."""

from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.adapters.contracts import Peer, TaskReceipt
from answerback_bridge.adapters.crew import CrewAdapter
from answerback_bridge.config import Config
from answerback_bridge.crew import _peer
from answerback_bridge.plugin import kernel_context
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TaskStore


def chat(created: float = 1.0) -> dict[str, Any]:
    return _peer(
        {
            "key": "review",
            "title": "Reviewer",
            "project": "",
            "agent": "kiro",
            "created": created,
            "running": False,
        },
        "http://127.0.0.1:9",
    )


@pytest.mark.parametrize(
    ("current_binding", "stored_peer"),
    [
        ("current", {"binding": "old"}),
        ("current", {}),
        (None, {}),
        ("", {"binding": ""}),
        (1, {"binding": 1}),
        ("current", None),
        ("current", "old"),
    ],
)
def test_crew_matches_requires_chat_binding(
    tmp_path: Path, current_binding: Any, stored_peer: Any
) -> None:
    """red_at_base: card 3, same slot alone or two absent bindings cannot match."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    adapter = CrewAdapter(
        kernel_context(Config(crew_enabled=True), tmp_path / "config.json", store), lambda: None
    )
    peer = Peer.from_record({**chat(), "binding": current_binding})
    task, _ = store.reserve(
        "crew:fixture:review", "Review", "old-chat", {"kind": "crew", "slot": "review"}
    )
    task["metadata"]["peer"] = stored_peer
    assert not adapter.matches(peer, TaskReceipt.from_record(task))


@pytest.mark.parametrize("name_field", ["target", "slot", "alias"])
def test_crew_matches_keeps_same_chat_receipts(tmp_path: Path, name_field: str) -> None:
    """pins_existing: card 3, matching identity retains the existing name/provider checks."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    adapter = CrewAdapter(
        kernel_context(Config(crew_enabled=True), tmp_path / "config.json", store), lambda: None
    )
    record = chat()
    peer = Peer.from_record(record)
    task, _ = store.reserve(
        "crew:fixture:review",
        "Review",
        "same-chat",
        {"kind": "crew", name_field: "review", "peer": record},
    )
    assert adapter.matches(peer, TaskReceipt.from_record(task))
    task["metadata"]["kind"] = "herdr"
    assert not adapter.matches(peer, TaskReceipt.from_record(task))
    task["metadata"].update(kind="crew", **{name_field: "different-chat"})
    assert not adapter.matches(peer, TaskReceipt.from_record(task))


async def test_crew_task_for_peer_excludes_recreated_chat(tmp_path: Path) -> None:
    """red_at_base: card 3, the public lookup must not return a recreated slot's old result."""
    bridge = Bridge(Config(crew_enabled=True), tmp_path / "config.json")

    class ChatDiscovery:
        record = chat()

        async def peers(self) -> list[dict[str, Any]]:
            return [self.record]

    fake = ChatDiscovery()
    bridge._crew_client = fake
    task, _ = bridge.store.reserve(
        "crew:fixture:review",
        "Review",
        "old-task",
        {"kind": "crew", "slot": "review", "peer": fake.record},
    )
    bridge.complete(task["id"], "Earlier chat")
    try:
        assert (await bridge.task_for_peer("crew:review"))["id"] == task["id"]
        fake.record = chat(created=2.0)
        with pytest.raises(ValueError, match="No receipt for this peer"):
            await bridge.task_for_peer("crew:review")
    finally:
        await bridge.close()
