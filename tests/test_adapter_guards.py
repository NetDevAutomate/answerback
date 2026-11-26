import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_herdr_bridge.adapters.contracts import Peer, Submission
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


@pytest.mark.parametrize("provider", ["desktop", "hc", "herdr", "crew"])
async def test_disabling_provider_during_final_preflight_prevents_external_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    registration = "codex" if provider == "desktop" else provider
    config = Config(
        provider_selection=[registration],
        herdr_session="test",
        hc_enabled=True,
        crew_enabled=True,
    )
    bridge = Bridge(config, tmp_path / "config.json")
    entered, release = asyncio.Event(), asyncio.Event()
    target = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" if provider == "desktop" else "one"
    record = {
        "peer_id": f"{provider}:{target}",
        "provider": provider,
        "target": target,
        "project": str(tmp_path),
        "terminal_id": "term",
        "send_ready": True,
        "binding": "first",
        "thread_id": target,
        "kind": "discovered_codex",
    }
    if provider == "herdr":
        record["agent_kind"] = "kiro"
    if provider == "crew":
        # A complete Crew identity, as discovery returns one: the adapter compares it
        # with the live chat before admission, so the guard is what refuses here.
        record.update(created=1.0, agent="kiro")

    async def preflight(*args, **kwargs):
        entered.set()
        await release.wait()
        return {**record, "agent_status": "idle", "cwd": str(tmp_path)}

    send = AsyncMock()
    bridge.desktop_index.read = preflight
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge.core = SimpleNamespace(peer=preflight, send=send)
    bridge.herdr = SimpleNamespace(check=AsyncMock(), get=preflight, call=send, read=AsyncMock())
    bridge._crew_client = SimpleNamespace(peer=preflight, submit=send, origin="http://127.0.0.1")
    task = None
    try:
        prepared = await bridge.router.prepare(Peer.from_record(record))
        task = asyncio.create_task(
            bridge.router.submit(prepared, Submission("Review", "guard-test"))
        )
        await asyncio.wait_for(entered.wait(), 1)
        config.provider_selection = []
        release.set()
        if provider == "desktop":
            assert (await task).status == "failed"
        else:
            with pytest.raises(ValueError, match="not registered"):
                await task
        assert send.await_count == 0
        assert all(row["status"] == "failed" for row in bridge.store.recent())
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


# herdr and crew have no legacy RPC submit any more: send-herdr, MCP and provider
# "crew" all route through peer_submit, whose registration recheck the router-path
# test above covers.
@pytest.mark.parametrize("provider", ["desktop", "kiro"])
async def test_legacy_rpc_submit_rechecks_registration_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    registration = "codex" if provider == "desktop" else provider
    chat = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    config = Config(
        provider_selection=[registration],
        herdr_session="test",
        crew_enabled=True,
        desktop_threads={"desk": chat},
        kiro_workspaces={"one": str(tmp_path)},
    )
    bridge = Bridge(config, tmp_path / "config.json")
    entered, release = asyncio.Event(), asyncio.Event()
    send = AsyncMock(return_value="queued" if provider == "desktop" else {})

    async def preflight(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"terminal_id": "term", "agent_status": "idle"}

    class Reader:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, *args):
            return await preflight()

    original = asyncio.to_thread

    async def checked(function, *args, **kwargs):
        if getattr(function, "__name__", "") == "is_dir":
            await preflight()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr("codex_herdr_bridge.codex.HistoryReader", Reader)
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    monkeypatch.setattr("codex_herdr_bridge.workers.asyncio.to_thread", checked)
    bridge.workers._run = send
    bridge.herdr = SimpleNamespace(check=AsyncMock(), get=preflight, call=send, read=AsyncMock())
    arguments = {"message": "Review", "request_id": "legacy-guard"}
    arguments["alias"] = "desk" if provider == "desktop" else "one"
    task = asyncio.create_task(bridge.call(provider + "_submit", arguments))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        config.provider_selection = []
        release.set()
        if provider == "desktop":
            assert (await task)["status"] == "failed"
        else:
            with pytest.raises(ValueError):
                await task
        await asyncio.sleep(0)
        assert send.await_count == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


@pytest.mark.parametrize("provider", ["desktop", "herdr"])
async def test_native_project_change_is_refused_with_stable_native_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    target = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" if provider == "desktop" else "one"
    config = Config(
        provider_selection=["codex" if provider == "desktop" else provider],
        herdr_session="test",
    )
    bridge = Bridge(config, tmp_path / "config.json")
    record = {
        "peer_id": f"{provider}:{target}",
        "provider": provider,
        "target": target,
        "project": str(old),
        "terminal_id": "term",
        "thread_id": target,
        "kind": "discovered_codex",
    }
    send = AsyncMock(return_value="queued" if provider == "desktop" else {})
    bridge.desktop_index.read = AsyncMock(return_value={**record, "project": str(new)})
    monkeypatch.setattr("codex_herdr_bridge.codex.queue", send)
    bridge.herdr = SimpleNamespace(
        check=AsyncMock(),
        call=send,
        get=AsyncMock(
            return_value={
                "terminal_id": "term",
                "agent_status": "idle",
                "foreground_cwd": str(new),
            }
        ),
    )
    try:
        prepared = await bridge.router.prepare(Peer.from_record(record), expected_project=str(old))
        if provider == "desktop":
            result = await bridge.router.submit(prepared, Submission("Review", "project-change"))
            assert result.status == "failed"
        else:
            with pytest.raises(ValueError, match="project"):
                await bridge.router.submit(prepared, Submission("Review", "project-change"))
        assert send.await_count == 0
    finally:
        await bridge.close()
