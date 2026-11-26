"""Independent actor integration regressions; no real model call is made."""

import asyncio
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from answerback_bridge.actors.model import ActorAuth
from answerback_bridge.actors.store import ActorStore
from answerback_bridge.config import Config
from answerback_bridge.config_inventory import archive_config
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TaskStore
from answerback_bridge.transport import RemoteBridge


async def test_actor_reply_does_not_wait_behind_all_native_admission_slots():
    entered = asyncio.Event()
    release = asyncio.Event()
    actor_entered = asyncio.Event()
    count = 0

    class AdmissionBridge:
        METHODS = {"peer_submit", "actor_reply"}

        async def call(self, method, params):
            nonlocal count
            if method == "peer_submit":
                count += 1
                if count == 4:
                    entered.set()
                await release.wait()
                return {"accepted": True}
            actor_entered.set()
            return {"message_id": "acknowledged-reply"}

    with tempfile.TemporaryDirectory(prefix="ab-review-", dir="/tmp") as folder:
        root = await asyncio.to_thread(Path(folder).resolve)
        cfg = root / "config.json"
        service = Service(cfg)
        service.bridge = AdmissionBridge()
        remote = RemoteBridge(cfg)
        remote.path.parent.mkdir(mode=0o700)
        tasks = []
        observation = None
        async with await asyncio.start_unix_server(service.handle, path=remote.path):
            try:
                tasks = [asyncio.create_task(remote.call("peer_submit", {})) for _ in range(4)]
                await asyncio.wait_for(entered.wait(), 2)
                tasks.append(asyncio.create_task(remote.call("actor_reply", {})))
                observation = asyncio.create_task(actor_entered.wait())
                observed, _ = await asyncio.wait({observation}, timeout=0.3)
                assert observed, "Actor reply is starved behind unrelated native admission slots"
                assert not release.is_set()
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                if service.clients:
                    await asyncio.gather(*service.clients, return_exceptions=True)
                if observation is not None:
                    observation.cancel()
                    await asyncio.gather(observation, return_exceptions=True)


async def test_actor_receive_polls_do_not_starve_native_admission_slots() -> None:
    polls_entered = asyncio.Event()
    release = asyncio.Event()
    submit_entered = asyncio.Event()
    count = 0

    with tempfile.TemporaryDirectory(prefix="ab-review-", dir="/tmp") as folder:
        cfg = await asyncio.to_thread(Path(folder).resolve) / "config.json"
        bridge = Bridge(Config(provider_selection=[]), cfg)
        service = Service(cfg)
        service.bridge = bridge

        async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal count
            if method == "actor_receive":
                count += 1
                if count == 4:
                    polls_entered.set()
                await release.wait()
                return {"messages": []}
            submit_entered.set()
            return {"accepted": True}

        bridge.call = call
        remote = RemoteBridge(cfg)
        remote.path.parent.mkdir(mode=0o700)
        tasks = []
        observation = None
        async with await asyncio.start_unix_server(service.handle, path=remote.path):
            try:
                tasks = [asyncio.create_task(remote.call("actor_receive", {})) for _ in range(4)]
                await asyncio.wait_for(polls_entered.wait(), 2)
                tasks.append(asyncio.create_task(remote.call("peer_submit", {})))
                observation = asyncio.create_task(submit_entered.wait())
                observed, _ = await asyncio.wait({observation}, timeout=0.3)
                assert observed, "Peer submit is starved behind actor receive long-polls"
                assert not release.is_set()
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                if service.clients:
                    await asyncio.gather(*service.clients, return_exceptions=True)
                if observation is not None:
                    observation.cancel()
                    await asyncio.gather(observation, return_exceptions=True)
                await bridge.close()


async def test_archive_preview_retains_stopped_config_with_unfinished_actor_run(tmp_path):
    project = await asyncio.to_thread(tmp_path.resolve)
    directory = project / "answerback-bridge"
    cfg = directory / "config.json"
    Config(provider_selection=[]).save(cfg)
    runtime = directory / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "config-owner.json").write_text(json.dumps({"config_path": str(cfg)}))
    (runtime / "owner.lock").touch(mode=0o600)
    TaskStore(directory / "tasks.sqlite")
    store = ActorStore(directory / "bus/actors.sqlite")
    run = str(uuid4())
    store.create_run(run, str(project), ["coordinator", "worker"])
    actors = []
    for role in ("coordinator", "worker"):
        auth = ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"native_id": role})
        actors.append(auth)
    store.seal_run(run, auth=actors[0])
    store.send(actors[0], actors[1].actor_id, "unfinished", "assignment", {"work": "pending"})
    result = await archive_config(cfg)
    assert result["bridge_status"]["state"] == "stopped"
    assert result["unresolved_receipts"] == 0
    assert result["archive_eligible"] is False, (
        "Unfinished actor messages bypassed the configuration retention guard"
    )
    assert any("actor" in reason.lower() for reason in result["reasons"])
