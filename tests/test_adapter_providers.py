import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from test_workers import FakeACP, wait_status

from codex_herdr_bridge.adapters.contracts import Peer, Submission, TaskReceipt
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge

CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.mark.parametrize("provider", ["desktop", "kiro"])
async def test_provider_contract_preserves_duplicates_and_correlated_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    config = Config(
        provider_selection=["codex", "kiro"],
        kiro_workspaces={"reviewer": str(tmp_path)},
    )
    bridge = Bridge(config, tmp_path / "config.json")
    monkeypatch.setattr("codex_herdr_bridge.workers.KiroACP", FakeACP)
    send = AsyncMock(return_value="queued")
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge.desktop_index.read = AsyncMock(
        return_value={
            "peer_id": f"desktop:{CHAT}",
            "provider": "desktop",
            "target": CHAT,
            "thread_id": CHAT,
            "project": str(tmp_path),
            "kind": "discovered_codex",
        }
    )
    try:
        adapter = bridge.adapters.get(provider)
        record = (
            await bridge.desktop_index.read(CHAT)
            if provider == "desktop"
            else {
                "peer_id": "kiro:reviewer",
                "provider": "kiro",
                "target": "reviewer",
                "project": str(tmp_path),
                "kind": "bridge_managed",
            }
        )
        prepared = await adapter.prepare(Peer.from_record(record))
        submission = Submission("Review", "contract-one")
        first = await adapter.submit(prepared, submission, guard=lambda: None)
        second = await adapter.submit(prepared, submission, guard=lambda: None)
        assert first.task_id == second.task_id
        assert first.target == prepared.target_key
        if provider == "desktop":
            assert send.await_count == 1
            assert first.status == "queued"
            bridge.store.update(first.task_id, "completed", result="Verified result")
        else:
            await wait_status(bridge.store, first.task_id, "completed")
        final = await adapter.observe(
            TaskReceipt.from_record(bridge.store.get(first.task_id)), refresh=False
        )
        assert final.status == "completed"
        assert final.task_id == first.task_id
    finally:
        await bridge.close()


async def test_kiro_checks_admission_guard_after_awaited_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(provider_selection=["kiro"], kiro_workspaces={"reviewer": str(tmp_path)})
    bridge = Bridge(config, tmp_path / "config.json")
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        adapter = bridge.adapters.get("kiro")
        prepared = await adapter.prepare(
            Peer.from_record(
                {
                    "peer_id": "kiro:reviewer",
                    "provider": "kiro",
                    "target": "reviewer",
                    "project": str(tmp_path),
                }
            )
        )
        original = asyncio.to_thread

        async def paused(function, *args, **kwargs):
            if getattr(function, "__name__", "") == "is_dir":
                entered.set()
                await release.wait()
            return await original(function, *args, **kwargs)

        monkeypatch.setattr("codex_herdr_bridge.workers.asyncio.to_thread", paused)
        allowed = [True]

        def guard() -> None:
            if not allowed[0]:
                raise ValueError("Routing changed")

        task = asyncio.create_task(
            adapter.submit(prepared, Submission("Review", "race"), guard=guard)
        )
        await asyncio.wait_for(entered.wait(), 1)
        allowed[0] = False
        release.set()
        with pytest.raises(ValueError, match="Routing changed"):
            await task
        assert bridge.store.recent() == []
        assert not bridge.workers.jobs
    finally:
        release.set()
        await bridge.close()


async def test_disabled_provider_never_refreshes_native_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    row, _ = bridge.store.reserve(
        f"desktop:{CHAT}",
        "Review",
        "disabled",
        {"kind": "desktop", "thread_id": CHAT, "binding": "discovered"},
    )
    reader = Mock(side_effect=AssertionError("Disabled provider was called"))
    monkeypatch.setattr("codex_herdr_bridge.codex.HistoryReader", reader)
    try:
        receipt = await bridge.get_task(row["id"])
        assert receipt["provider_disabled"] is True
        assert reader.call_count == 0
    finally:
        await bridge.close()


async def test_stored_reads_do_not_wait_for_native_history_slots(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=["codex"]), tmp_path / "config.json")
    row, _ = bridge.store.reserve(
        f"desktop:{CHAT}",
        "Review",
        "stored-read",
        {"kind": "desktop", "thread_id": CHAT, "binding": "discovered"},
    )
    adapter = bridge.adapters.get("desktop")
    await adapter.history_slots.acquire()
    await adapter.history_slots.acquire()
    try:
        async with asyncio.timeout(0.2):
            result = await bridge.get_task(row["id"], refresh=False)
        assert result["id"] == row["id"]
    finally:
        adapter.history_slots.release()
        adapter.history_slots.release()
        await bridge.close()


async def test_paired_codex_retry_recovers_receipt_without_native_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_herdr_bridge.process import BridgeError

    bridge = Bridge(
        Config(provider_selection=["codex"], desktop_threads={"reviewer": CHAT}),
        tmp_path / "config.json",
    )
    bridge.desktop_index.read = AsyncMock(
        return_value={
            "peer_id": f"desktop:{CHAT}",
            "provider": "desktop",
            "target": CHAT,
            "thread_id": CHAT,
            "project": str(tmp_path),
            "kind": "discovered_codex",
        }
    )
    send = AsyncMock(return_value="queued")
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    try:
        first = await bridge.peer_submit("desktop:reviewer", "Review", "retry")
        bridge.desktop_index.read.side_effect = BridgeError("Native reader unavailable")
        second = await bridge.peer_submit("desktop:reviewer", "Review", "retry")
        assert second["id"] == first["id"]
        assert send.await_count == 1
        bridge.config.desktop_threads["reviewer"] = "11111111-2222-3333-4444-555555555555"
        with pytest.raises(ValueError, match="different"):
            await bridge.peer_submit("desktop:reviewer", "Review", "retry")
        assert send.await_count == 1
    finally:
        await bridge.close()
