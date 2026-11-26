import os
import shlex
from pathlib import Path
from uuid import uuid4

import pytest

from answerback_bridge.config import Config, public_task
from answerback_bridge.service import Bridge


@pytest.fixture
def saved_rex_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real disposable executable, reached only by the unmodified native launch path."""
    executable = tmp_path / "rex-fixture"
    marker = tmp_path / "executed"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf ran > {shlex.quote(str(marker))}\n"
        """printf '%s' '{"source":"fixture"}'\n"""
    )
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    return executable, marker


@pytest.mark.parametrize("binary", ["rex-fixture", "./rex-fixture"])
@pytest.mark.parametrize("operation", ["cli", "probe", "terminal", "read", "discovery"])
async def test_saved_unpinned_rex_never_executes(
    tmp_path: Path, saved_rex_program, binary: str, operation: str
) -> None:
    """red_at_base (round 2): legacy names cannot launch through a loaded service."""
    from test_rex import B, S

    from answerback_bridge.adapters.rex import RexDiscovery
    from answerback_bridge.process import BridgeError

    _, marker = saved_rex_program
    path = tmp_path / "config.json"
    Config(
        codex_enabled=False,
        rex_enabled=True,
        rex_binary=binary,
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="local",
    ).save(path)
    bridge = Bridge(Config.load(path), path)
    refusal = "Rex executable must be an absolute path; run answerback rex-configure"
    try:
        # A legacy Rex configuration does not prevent construction or non-Rex calls.
        assert await bridge.call("provider_list", {}) == bridge.providers.snapshot()
        adapter = bridge.rex.adapter
        if operation == "discovery":
            page = await RexDiscovery(bridge.config, lambda: bridge.rex).discover()
            assert not marker.exists()
            assert page.as_record()["complete"] is False
            assert page.peers == ()
            assert page.errors == (("rex:0", refusal),)
        else:
            with pytest.raises(BridgeError) as caught:
                if operation == "cli":
                    await adapter._cli("whoami", "--json")
                elif operation == "probe":
                    await adapter.probe()
                elif operation == "terminal":
                    await adapter.terminal(S, B)
                else:
                    await bridge.call("rex_read", {"session_id": S, "block_id": B})
            assert not marker.exists()
            assert str(caught.value) == refusal
        assert bridge.config.rex_binary == Config.load(path).rex_binary == binary
    finally:
        await bridge.close()


async def test_saved_absolute_rex_keeps_native_execution(tmp_path: Path, saved_rex_program) -> None:
    """pins_existing: a loaded absolute executable still runs through RexService."""
    executable, marker = saved_rex_program
    path = tmp_path / "config.json"
    Config(
        codex_enabled=False,
        rex_enabled=True,
        rex_binary=str(executable),
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="local",
    ).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        assert await bridge.rex.adapter._cli("whoami", "--json") == {"source": "fixture"}
        assert marker.read_text() == "ran"
    finally:
        await bridge.close()


@pytest.mark.parametrize("binary", ["rex-fixture", "./rex-fixture"])
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_first_pin_preserves_enrollments_and_unfinished_work(
    tmp_path: Path,
    saved_rex_program,
    monkeypatch: pytest.MonkeyPatch,
    binary: str,
    acknowledged: bool,
) -> None:
    """red_at_base (MM3): explicit first pin unblocks work on the same Rex server."""
    from test_rex import ENDPOINT, B, S, cli
    from test_rex_mailbox import binding

    from answerback_bridge.process import BridgeError

    executable, marker = saved_rex_program
    script = cli.__wrapped__(monkeypatch)
    path = tmp_path / "config.json"
    Config(
        codex_enabled=False,
        rex_enabled=True,
        rex_binary=binary,
        rex_server=ENDPOINT,
        rex_host_id="local",
    ).save(path)
    bridge = Bridge(Config.load(path), path)
    target = binding(
        endpoint=ENDPOINT,
        server_instance_id="run-1",
        server_host_id="local",
        block_host_id="local",
        session_id=S,
        block_id=B,
    )
    busy = bridge.rex.mailbox.enroll("busy", target, "busy")
    bridge.rex.mailbox.enroll(
        "idle",
        {**target, "block_id": "block:33333333-3333-4333-8333-333333333333"},
        "idle",
    )
    task = bridge.rex.mailbox.submit(busy["peer_id"], "work", "work")
    if acknowledged:
        bridge.rex.mailbox.acknowledge(busy["peer_id"], task["id"])
    peers_before = bridge.rex.mailbox.peers()
    task_before = bridge.store.get(task["id"])
    params = {"peer_id": busy["peer_id"], "task_id": task["id"]}
    try:
        for method, values in (
            ("rex_receive", {"peer_id": busy["peer_id"]}),
            ("rex_ack", params),
            ("rex_complete", {**params, "result": "done"}),
        ):
            with pytest.raises(BridgeError) as caught:
                await bridge.call(method, values)
            assert str(caught.value) == (
                "Rex executable must be an absolute path; run answerback rex-configure"
            )
        assert script.calls == []
        assert not marker.exists()
        assert bridge.rex.mailbox.peers() == peers_before
        assert bridge.store.get(task["id"]) == task_before

        async def probe(adapter):
            assert adapter.binary == str(executable)
            assert adapter.endpoint == ENDPOINT
            return {
                "endpoint": ENDPOINT,
                "server_host_id": "local",
                "server_instance_id": "run-1",
            }

        monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
        result = await bridge.call("rex_configure", {"binary": binary})
        assert result["configured"] is True
        assert bridge.config.rex_binary == Config.load(path).rex_binary == str(executable)
        assert bridge.rex.mailbox.peers() == peers_before
        assert bridge.store.get(task["id"]) == task_before

        inbox = await bridge.call("rex_receive", {"peer_id": busy["peer_id"]})
        if acknowledged:
            assert inbox["active_task"] == public_task(task_before)
        else:
            assert inbox["tasks"][0]["id"] == task["id"]
            assert inbox["tasks"][0]["message"] == "work"
        assert (await bridge.call("rex_ack", params))["status"] == "submitted"
        done = await bridge.call("rex_complete", {**params, "result": "done"})
        assert done["status"] == "completed"
        assert done["result"] == "done"
        assert bridge.rex.mailbox.peers() == peers_before
        assert script.calls and all(call[0] == str(executable) for call in script.calls)
    finally:
        await bridge.close()
        assert not marker.exists()


class Observation:
    def __init__(self):
        self.binding = dict(
            endpoint="unix:///tmp/rex.sock",
            server_instance_id="one",
            server_host_id="local",
            block_host_id="local",
            session_id="session:" + str(uuid4()),
            block_id="block:" + str(uuid4()),
            foreground_pid=42,
            foreground_name="agent",
            foreground_path=None,
            foreground_script=None,
            foreground_cwd="/tmp",
        )

    async def terminal(self, session_id, block_id):
        return {"binding": self.binding.copy(), "foreground": {"user_id": os.getuid()}}

    async def inventory(self):
        return {"peers": [], "complete": False, "errors": ["partial"]}


@pytest.mark.asyncio
async def test_cooperative_roundtrip_and_stale_retry(tmp_path):
    bridge = Bridge(
        Config(
            codex_enabled=False,
            rex_enabled=True,
            rex_server="unix:///tmp/rex.sock",
            rex_host_id="local",
        ),
        tmp_path / "config.json",
    )
    obs = Observation()
    bridge.rex.adapter = obs
    peer = await bridge.call(
        "rex_enroll",
        dict(
            name="agent",
            session_id=obs.binding["session_id"],
            block_id=obs.binding["block_id"],
            request_id="enroll",
        ),
    )
    task = await bridge.peer_submit(peer["peer_id"], "work", "request")
    inbox = await bridge.call("rex_receive", {"peer_id": peer["peer_id"]})
    assert inbox["tasks"][0]["message"] == "work"
    with pytest.raises(ValueError):
        bridge.complete(task["id"], "bypass")
    await bridge.call("rex_ack", dict(peer_id=peer["peer_id"], task_id=task["id"]))
    await bridge.call(
        "rex_complete", dict(peer_id=peer["peer_id"], task_id=task["id"], result="done")
    )
    obs.binding["foreground_pid"] = 43
    with pytest.raises(ValueError):
        await bridge.call("rex_receive", {"peer_id": peer["peer_id"]})
    assert (await bridge.peer_submit(peer["peer_id"], "work", "request"))["id"] == task["id"]
    with pytest.raises(ValueError):
        await bridge.task_for_peer(peer["peer_id"])
    assert (await bridge.get_task(task["id"]))["result"] == "done"
    await bridge.close()


@pytest.fixture
def routed(tmp_path):
    bridge = Bridge(
        Config(
            codex_enabled=False,
            rex_enabled=True,
            rex_server="unix:///tmp/rex.sock",
            rex_host_id="local",
        ),
        tmp_path / "config.json",
    )
    bridge.rex.adapter = Observation()
    return bridge


async def enroll(bridge, name="agent"):
    binding = bridge.rex.adapter.binding
    return await bridge.rex_enroll(name, binding["session_id"], binding["block_id"], name)


@pytest.mark.parametrize("value", [1, "true", None])
def test_strict_flag(value):
    with pytest.raises(ValueError):
        Config(rex_enabled=value)


@pytest.mark.parametrize(
    "endpoint", ["http://localhost", "unix://remote/path", "unix:///tmp/a?x=1"]
)
def test_strict_endpoint(endpoint):
    with pytest.raises(ValueError):
        Config(rex_server=endpoint)


def test_old_configuration_and_preservation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    assert Config.load(path).rex_enabled is False
    config = Config(
        codex_enabled=False,
        rex_enabled=True,
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="local",
    )
    config.save(path)
    assert Config.load(path).rex_host_id == "local"


async def test_disabled_and_wrong_uid(routed):
    routed.config.rex_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await enroll(routed)
    routed.config.rex_enabled = True
    original = routed.rex.adapter.terminal

    async def foreign(*args):
        row = await original(*args)
        row["foreground"]["user_id"] = os.getuid() + 1
        return row

    routed.rex.adapter.terminal = foreign
    with pytest.raises(ValueError, match="owned"):
        await enroll(routed)
    assert routed.rex.mailbox.peers() == []


async def test_identity_change_and_transient_failure(routed):
    from answerback_bridge.process import BridgeError

    peer = await enroll(routed)
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    original = routed.rex.adapter.terminal

    async def unavailable(*args):
        raise BridgeError("Temporary connection failure")

    routed.rex.adapter.terminal = unavailable
    with pytest.raises(BridgeError):
        await routed.rex_ack(peer["peer_id"], task["id"])
    assert routed.rex.mailbox.get(peer["peer_id"])["state"] == "active"
    routed.rex.adapter.terminal = original
    routed.rex.adapter.binding["server_instance_id"] = "two"
    with pytest.raises(ValueError):
        await routed.rex_ack(peer["peer_id"], task["id"])
    assert routed.store.get(task["id"])["status"] == "uncertain"
    assert routed.rex.mailbox.get(peer["peer_id"])["state"] == "stale"


async def test_wait_notification_capacity_shutdown(routed):
    import asyncio

    peer = await enroll(routed)
    waiting = asyncio.create_task(routed.rex_receive(peer["peer_id"], 15))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="capacity"):
        await routed.rex_receive(peer["peer_id"], 15)
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    assert (await asyncio.wait_for(waiting, 1))["tasks"][0]["id"] == task["id"]
    routed.abandon(task["id"], "human reconciled")
    waiting = asyncio.create_task(routed.rex_receive(peer["peer_id"], 15))
    await asyncio.sleep(0)
    await routed.close()
    assert (await asyncio.wait_for(waiting, 1))["shutdown"] is True
    assert routed.store.get(task["id"])["status"] == "abandoned"
    for value in [True, -1, 16, 1.5]:
        with pytest.raises(ValueError):
            await routed.rex_receive(peer["peer_id"], value)


async def test_history_restart_reset_and_retirement(routed):
    peer = await enroll(routed)
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    routed.abandon(task["id"], "reconciled")
    await routed.rex_retire(peer["peer_id"])
    for index in range(110):
        other, _ = routed.store.reserve("other-" + str(index), "message", "other-" + str(index))
        routed.store.update(other["id"], "abandoned")
    routed.reset_connections()
    restarted = Bridge(routed.config, routed.config_path)
    with pytest.raises(ValueError):
        await restarted.task_for_peer(peer["peer_id"])
    assert (await restarted.get_task(task["id"]))["id"] == task["id"]
    assert (await restarted.peer_submit(peer["peer_id"], "work", "work"))["id"] == task["id"]
    assert restarted.rex.mailbox.get(peer["peer_id"])["state"] == "retired"


async def test_concurrent_discovery_budget(monkeypatch):
    import asyncio

    from answerback_bridge.discovery import Directory

    class Slow:
        async def inventory(self):
            await asyncio.sleep(10)

    class Healthy:
        async def peers(self):
            return [
                {
                    "peer_id": "crew:one",
                    "provider": "crew",
                    "target": "one",
                    "project": "/project",
                    "name": "one",
                }
            ]

    monkeypatch.setattr("answerback_bridge.discovery.PROVIDER_SECONDS", 0.03)
    directory = Directory(
        Config(codex_enabled=False, hc_enabled=True, crew_enabled=True),
        None,
        Healthy(),
        core=Slow(),
    )
    result = await asyncio.wait_for(directory.search(), 0.2)
    assert result["peers"][0]["peer_id"] == "crew:one"
    assert "hc" in result["errors"]


async def test_configuration_probe_before_save_and_busy_refusal(routed, monkeypatch):
    class Probe:
        def __init__(self, binary, endpoint):
            self.endpoint = endpoint

        async def probe(self):
            return {
                "endpoint": self.endpoint or "unix:///tmp/default.sock",
                "server_host_id": "local",
                "server_instance_id": "one",
            }

    monkeypatch.setattr("answerback_bridge.rex_service.Rex", Probe)
    monkeypatch.setattr("answerback_bridge.rex.shutil.which", lambda binary: "/fixture/" + binary)
    routed.config.rex_binary = "/fixture/rex"
    routed.rex.adapter_binding = (
        routed.config.rex_binary,
        routed.config.rex_server,
        routed.config.rex_host_id,
    )
    peer = await enroll(routed)
    original = routed.rex.adapter
    await routed.rex_configure("rex")
    routed.rex.adapter = original
    assert routed.config.rex_server == "unix:///tmp/rex.sock"
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    with pytest.raises(ValueError, match="unfinished"):
        await routed.rex_configure("other", "unix:///tmp/other.sock")
    routed.abandon(task["id"], "reconciled")
    await routed.rex_configure("other", "unix:///tmp/other.sock")
    assert routed.rex.mailbox.get(peer["peer_id"])["state"] == "stale"
    assert Config.load(routed.config_path).rex_binary == "/fixture/other"


async def test_rpc_wait_does_not_hold_ordinary_slots(routed):
    import asyncio
    import json

    from answerback_bridge.runtime import Service
    from answerback_bridge.transport import RPC_VERSION

    peer = await enroll(routed)
    service = Service(routed.config_path)
    service.bridge = routed

    class Writer:
        def __init__(self):
            self.output = b""

        def write(self, data):
            self.output += data

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def request(method, params):
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "version": RPC_VERSION,
                        "config_path": str(service.config_path),
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        writer = Writer()
        await service.handle(reader, writer)
        return json.loads(writer.output)

    waiting = asyncio.create_task(
        request("rex_receive", {"peer_id": peer["peer_id"], "wait_seconds": 15})
    )
    await asyncio.sleep(0)
    # All ordinary slots remain available while the receiver waits.
    acquired = 0
    try:
        for _ in range(4):
            await asyncio.wait_for(service.slots.acquire(), 0.1)
            acquired += 1
    finally:
        for _ in range(acquired):
            service.slots.release()
    response = await asyncio.wait_for(
        request(
            "peer_submit", {"selector": peer["peer_id"], "message": "work", "request_id": "rpc"}
        ),
        1,
    )
    assert response["ok"]
    assert (await asyncio.wait_for(waiting, 1))["result"]["tasks"][0]["id"] == response["result"][
        "id"
    ]
    task_id = response["result"]["id"]
    assert (await request("rex_ack", {"peer_id": peer["peer_id"], "task_id": task_id}))["ok"]
    assert (
        await request(
            "rex_complete", {"peer_id": peer["peer_id"], "task_id": task_id, "result": "done"}
        )
    )["ok"]


async def test_rpc_wait_capacity_and_cancellation_release(routed):
    import asyncio
    import json

    from answerback_bridge.runtime import Service
    from answerback_bridge.transport import RPC_VERSION

    peers = []
    for index in range(9):
        routed.rex.adapter.binding["block_id"] = "block:" + str(uuid4())
        peers.append(await enroll(routed, "rpc-peer-" + str(index)))
    bindings = {peer["binding"]["block_id"]: peer["binding"] for peer in peers}
    started = asyncio.Event()
    observed = 0

    async def terminal(session_id, block_id):
        nonlocal observed
        observed += 1
        if observed == 8:
            started.set()
        return {"binding": dict(bindings[block_id]), "foreground": {"user_id": os.getuid()}}

    routed.rex.adapter.terminal = terminal
    service = Service(routed.config_path)
    service.bridge = routed

    class Writer:
        def __init__(self):
            self.output = b""

        def write(self, data):
            self.output += data

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def receive(peer_id, seconds):
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "version": RPC_VERSION,
                        "config_path": str(service.config_path),
                        "method": "rex_receive",
                        "params": {"peer_id": peer_id, "wait_seconds": seconds},
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        writer = Writer()
        await service.handle(reader, writer)
        return json.loads(writer.output)

    waiting = [asyncio.create_task(receive(peer["peer_id"], 15)) for peer in peers[:8]]
    try:
        async with asyncio.timeout(1):
            await started.wait()
        refused = await asyncio.wait_for(receive(peers[8]["peer_id"], 15), 1)
        assert refused["ok"] is False and "capacity" in str(refused)
        waiting[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting[0]
        retry = await asyncio.wait_for(receive(peers[0]["peer_id"], 0), 1)
        assert retry["ok"] is True and retry["result"]["tasks"] == []
    finally:
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
    assert routed.rex.waiters == {}


async def test_inventory_merge_partial_uncertainty_and_raw_submit(routed):
    peer = await enroll(routed)
    binding = routed.rex.adapter.binding

    async def inventory():
        row = await routed.rex.adapter.terminal(binding["session_id"], binding["block_id"])
        return {
            "peers": [
                {
                    **row,
                    "peer_id": "rex:terminal:" + binding["session_id"] + "/" + binding["block_id"],
                    "provider": "rex",
                    "label": "raw",
                }
            ],
            "complete": True,
            "errors": [],
        }

    routed.rex.adapter.inventory = inventory
    result = await routed.peer_search(provider="rex")
    assert len(result["peers"]) == 1
    assert result["peers"][0]["verified"] is True
    routed.rex.adapter.inventory = Observation().inventory
    partial = await routed.peer_search(provider="rex")
    assert partial["peers"][0]["verified"] is False
    assert partial["peers"][0]["capabilities"] == []
    assert partial["complete"] is False
    refusal = await routed.peer_submit(
        "rex:terminal:" + binding["session_id"] + "/" + binding["block_id"], "work", "raw"
    )
    assert refusal["status"] == "needs_selection"
    assert routed.store.by_request("raw") is None
    assert (await routed.directory().resolve(peer["peer_id"]))["verified"] is True


async def test_completion_retry_after_retirement(routed):
    peer = await enroll(routed)
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    await routed.rex_ack(peer["peer_id"], task["id"])
    await routed.rex_complete(peer["peer_id"], task["id"], "done")
    await routed.rex_retire(peer["peer_id"])
    assert (await routed.rex_complete(peer["peer_id"], task["id"], "done"))["status"] == "completed"
    with pytest.raises(ValueError, match="different"):
        await routed.rex_complete(peer["peer_id"], task["id"], "changed")


async def test_eight_waiters_and_owned_shutdown(routed):
    import asyncio

    peers = []
    for index in range(9):
        routed.rex.adapter.binding["block_id"] = "block:" + str(uuid4())
        peers.append(await enroll(routed, "agent-" + str(index)))
    bindings = {p["binding"]["block_id"]: p["binding"] for p in peers}

    async def terminal(session_id, block_id):
        return {"binding": bindings[block_id], "foreground": {"user_id": os.getuid()}}

    routed.rex.adapter.terminal = terminal
    waits = [asyncio.create_task(routed.rex_receive(p["peer_id"], 15)) for p in peers[:8]]
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="capacity"):
        await routed.rex_receive(peers[8]["peer_id"], 15)
    await routed.close()
    results = await asyncio.wait_for(asyncio.gather(*waits), 1)
    assert all(result["shutdown"] for result in results)
    assert routed.rex.waiters == {}
    assert len(routed.rex.mailbox.peers()) == 9


async def test_initial_configuration_failed_probe_and_preserved_fields(tmp_path, monkeypatch):
    from answerback_bridge.process import BridgeError

    bridge = Bridge(Config(codex_enabled=False), tmp_path / "config.json")
    failure = True

    class Probe:
        def __init__(self, binary, server):
            assert server is None

        async def probe(self):
            if failure:
                raise BridgeError("unavailable")
            bridge.config.kiro_workspaces["added-during-probe"] = "/tmp/project"
            return {
                "endpoint": "unix:///tmp/default.sock",
                "server_host_id": "local",
                "server_instance_id": "one",
            }

    monkeypatch.setattr("answerback_bridge.rex_service.Rex", Probe)
    monkeypatch.setattr("answerback_bridge.rex.shutil.which", lambda binary: "/fixture/" + binary)
    with pytest.raises(BridgeError):
        await bridge.rex_configure()
    assert not bridge.config_path.exists()
    assert not bridge.config.rex_enabled
    failure = False
    await bridge.rex_configure()
    saved = Config.load(bridge.config_path)
    assert saved.kiro_workspaces == {"added-during-probe": "/tmp/project"}
    assert saved.rex_server == "unix:///tmp/default.sock"
    assert saved.codex_enabled is False


@pytest.mark.parametrize(
    "capabilities, expected",
    [
        ({"read": False, "enroll": False}, []),
        ({"read": True, "enroll": False}, ["read"]),
        ({"read": False, "enroll": True}, ["enroll"]),
        ({"read": True, "enroll": True}, ["read", "enroll"]),
        ({}, []),
    ],
)
async def test_raw_inventory_preserves_native_capabilities(routed, capabilities, expected):
    observation = await routed.rex.adapter.terminal(None, None)
    observation.update(
        peer_id="rex:terminal:raw",
        provider="rex",
        local=False,
        capabilities=capabilities,
    )

    async def inventory():
        return {"peers": [observation], "complete": True, "errors": []}

    routed.rex.adapter.inventory = inventory
    result = await routed.peer_search(provider="rex")
    assert result["peers"][0]["capabilities"] == expected


@pytest.mark.parametrize("observed_pid", [42, 99])
async def test_old_inventory_cannot_invalidate_replacement_enrollment(routed, observed_pid):
    import asyncio

    old_peer = await enroll(routed)
    observation = await routed.rex.adapter.terminal(None, None)
    observation["binding"]["foreground_pid"] = observed_pid
    observation.update(peer_id="rex:terminal:raw", provider="rex", capabilities={})
    started, release = asyncio.Event(), asyncio.Event()

    async def inventory():
        started.set()
        await release.wait()
        return {"peers": [observation], "complete": True, "errors": []}

    routed.rex.adapter.inventory = inventory
    discovery = asyncio.create_task(routed.peer_search(provider="rex"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        # Inventory may stall; ordinary lifecycle operations must still complete.
        await asyncio.wait_for(routed.rex_retire(old_peer["peer_id"]), 1)
        routed.rex.adapter.binding["foreground_pid"] = 43
        replacement = await asyncio.wait_for(enroll(routed, "replacement"), 1)
        task = await asyncio.wait_for(
            routed.peer_submit(replacement["peer_id"], "new work", "replacement-work"), 1
        )
        release.set()
        result = await asyncio.wait_for(discovery, 1)
        row = next(p for p in result["peers"] if p["peer_id"] == replacement["peer_id"])
        assert row["verified"] is False
        assert row["capabilities"] == []
        assert routed.rex.mailbox.get(old_peer["peer_id"])["state"] == "retired"
        assert routed.rex.mailbox.get(replacement["peer_id"])["state"] == "active"
        assert routed.store.get(task["id"])["status"] == "queued"
        assert (await routed.rex_receive(replacement["peer_id"]))["tasks"][0]["id"] == task["id"]
    finally:
        release.set()
        await asyncio.gather(discovery, return_exceptions=True)


async def test_current_inventory_still_invalidates_changed_identity(routed):
    peer = await enroll(routed)
    task = await routed.peer_submit(peer["peer_id"], "work", "work")
    routed.rex.adapter.binding["foreground_pid"] = 43
    observation = await routed.rex.adapter.terminal(None, None)
    observation.update(peer_id="rex:terminal:raw", provider="rex", capabilities={})

    async def inventory():
        return {"peers": [observation], "complete": True, "errors": []}

    routed.rex.adapter.inventory = inventory
    await routed.peer_search(provider="rex")
    assert routed.rex.mailbox.get(peer["peer_id"])["state"] == "stale"
    assert routed.store.get(task["id"])["status"] == "uncertain"


async def test_inventory_from_replaced_adapter_cannot_verify_peer(routed):
    import asyncio

    peer = await enroll(routed)
    old_adapter = routed.rex.adapter
    observation = await old_adapter.terminal(None, None)
    observation.update(peer_id="rex:terminal:raw", provider="rex", capabilities={})
    started, release = asyncio.Event(), asyncio.Event()

    async def inventory():
        started.set()
        await release.wait()
        return {"peers": [observation], "complete": True, "errors": []}

    old_adapter.inventory = inventory
    discovery = asyncio.create_task(routed.peer_search(provider="rex"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        replacement_adapter = Observation()
        replacement_adapter.binding = old_adapter.binding.copy()
        routed.rex.adapter = replacement_adapter
        release.set()
        result = await asyncio.wait_for(discovery, 1)
        row = next(p for p in result["peers"] if p["peer_id"] == peer["peer_id"])
        assert row["verified"] is False
        assert routed.rex.mailbox.get(peer["peer_id"])["state"] == "active"
    finally:
        release.set()
        await asyncio.gather(discovery, return_exceptions=True)


@pytest.fixture
def native_route(tmp_path, monkeypatch):
    from test_rex import ENDPOINT, B, S, cli

    # Reuse the public CLI script, retaining a real Bridge and SQLite mailbox.
    script = cli.__wrapped__(monkeypatch)
    script.foreground = {**script.foreground, "user_id": os.getuid()}
    bridge = Bridge(
        Config(
            codex_enabled=False,
            rex_enabled=True,
            rex_binary="/fixture/rex",
            rex_server=ENDPOINT,
            rex_host_id="local",
        ),
        tmp_path / "config.json",
    )
    return bridge, script, S, B


async def test_rex_read_refuses_foreign_foreground(native_route):
    """red_at_base: the RPC route reads an own terminal but refuses a foreign one."""
    from answerback_bridge.process import BridgeError

    bridge, script, session, block = native_route
    params = {"session_id": session, "block_id": block, "max_bytes": 4}
    try:
        assert (await bridge.call("rex_read", params))["text"] == "é"
        script.foreground = {**script.foreground, "user_id": os.getuid() + 1}
        script.calls.clear()
        with pytest.raises(BridgeError, match="local bounded-read capability"):
            await bridge.call("rex_read", params)
        assert not any(
            "call" in call and "com.superlogical.terminal.format" in call for call in script.calls
        )
    finally:
        await bridge.close()


@pytest.mark.parametrize("operation", ["inventory", "receive", "ack", "complete", "submit"])
async def test_native_absence_invalidates_unfinished_preserves_history(native_route, operation):
    from answerback_bridge.process import BridgeError

    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    peer_id = peer["peer_id"]
    done = await bridge.peer_submit(peer_id, "done work", "done")
    await bridge.rex_ack(peer_id, done["id"])
    await bridge.rex_complete(peer_id, done["id"], "result")
    pending = await bridge.peer_submit(peer_id, "pending", "pending")
    script.overrides = {
        "session.describe_block": BridgeError("lookup failure"),
        "session.list": {"sessions": []},
    }
    if operation == "inventory":
        assert (await bridge.rex.inventory())["complete"]
    else:
        with pytest.raises(BridgeError):
            if operation == "receive":
                await bridge.rex_receive(peer_id)
            elif operation == "ack":
                await bridge.rex_ack(peer_id, pending["id"])
            elif operation == "complete":
                await bridge.rex_complete(peer_id, pending["id"], "first completion")
            else:
                await bridge.peer_submit(peer_id, "new", "new")
    assert bridge.rex.mailbox.get(peer_id)["state"] == "stale"
    assert bridge.store.get(pending["id"])["status"] == "uncertain"
    assert bridge.store.get(done["id"])["status"] == "completed"
    assert (await bridge.rex_complete(peer_id, done["id"], "result"))["result"] == "result"
    assert (await bridge.peer_submit(peer_id, "pending", "pending"))["id"] == pending["id"]
    await bridge.close()


@pytest.mark.parametrize("listing", [{}, {"sessions": "bad"}, {"sessions": [], "partial": True}])
async def test_native_partial_absence_keeps_mailbox_active(native_route, listing):
    from answerback_bridge.process import BridgeError
    from answerback_bridge.rex import RexIdentityChanged

    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    task = await bridge.peer_submit(peer["peer_id"], "work", "work")
    script.overrides = {
        "session.describe_block": BridgeError("lookup failure"),
        "session.list": listing,
    }
    assert not (await bridge.rex.inventory())["complete"]
    with pytest.raises(BridgeError) as caught:
        await bridge.rex_receive(peer["peer_id"])
    assert not isinstance(caught.value, RexIdentityChanged)
    assert bridge.rex.mailbox.get(peer["peer_id"])["state"] == "active"
    assert bridge.store.get(task["id"])["status"] == "queued"
    await bridge.close()


@pytest.mark.parametrize("operation", ["inventory", "receive", "ack", "complete", "submit"])
@pytest.mark.parametrize("missing", ["foreground", "cwd", "host_id", "user_id", "path", "script"])
async def test_native_missing_identity_withholds_authority_preserves_and_recovers(
    native_route, operation, missing
):
    bridge, script, session, block = native_route
    script.foreground = {
        **script.foreground,
        "path": "/usr/bin/python",
        "script": "/project/peer.py",
    }
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    peer_id = peer["peer_id"]
    done = await bridge.peer_submit(peer_id, "previous work", "done")
    await bridge.rex_ack(peer_id, done["id"])
    await bridge.rex_complete(peer_id, done["id"], "retained result")
    task = await bridge.peer_submit(peer_id, "pending work", "pending")
    if operation == "complete":
        await bridge.rex_ack(peer_id, task["id"])
    saved_task = bridge.store.get(task["id"])
    saved_done = bridge.store.get(done["id"])
    # Comparing serialized rows checks that timestamps, binding, metadata and
    # receipt bodies all remain byte-identical during incomplete observations.
    with bridge.store.connection() as db:
        saved_rows = [tuple(row) for row in db.execute("SELECT * FROM rex_peers")]
        saved_receipts = [tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]
    foreground, native_block = script.foreground, script.block
    if missing == "foreground":
        script.overrides = {"com.superlogical.terminal.process": {}}
    elif missing == "host_id":
        script.block = {key: value for key, value in native_block.items() if key != missing}
    else:
        script.foreground = {key: value for key, value in foreground.items() if key != missing}
    try:
        if operation == "inventory":
            inventory = await bridge.rex.inventory()
            assert inventory["complete"] is False
            row = next(row for row in inventory["peers"] if row["peer_id"] == peer_id)
            assert row["verified"] is False
            assert row["capabilities"] == []
        else:
            with pytest.raises(ValueError, match="incomplete"):
                if operation == "receive":
                    await bridge.rex_receive(peer_id)
                elif operation == "ack":
                    await bridge.rex_ack(peer_id, task["id"])
                elif operation == "complete":
                    await bridge.rex_complete(peer_id, task["id"], "first completion")
                else:
                    await bridge.rex.submit(peer_id, "new work", "new")
        assert bridge.rex.mailbox.get(peer_id) == peer
        assert bridge.store.get(task["id"]) == saved_task
        assert bridge.store.get(done["id"]) == saved_done
        assert bridge.store.by_request("new") is None
        with bridge.store.connection() as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM rex_peers")] == saved_rows
            assert [
                tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")
            ] == saved_receipts
        assert (await bridge.rex_complete(peer_id, done["id"], "retained result"))[
            "result"
        ] == "retained result"
        assert (await bridge.rex.submit(peer_id, "pending work", "pending"))["id"] == task["id"]
        with bridge.store.connection() as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM rex_peers")] == saved_rows
            assert [
                tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")
            ] == saved_receipts
        script.foreground, script.block, script.overrides = foreground, native_block, {}
        assert await bridge.rex.verify(peer_id) == peer
        inventory = await bridge.rex.inventory()
        assert inventory["complete"] is True
        row = next(row for row in inventory["peers"] if row["peer_id"] == peer_id)
        assert row["verified"] is True
        assert row["capabilities"] == ["queue", "ack", "result"]
        if operation == "complete":
            assert (await bridge.rex_complete(peer_id, task["id"], "first completion"))[
                "status"
            ] == "completed"
        else:
            assert (await bridge.rex_receive(peer_id))["tasks"][0]["id"] == task["id"]
            assert (await bridge.rex_ack(peer_id, task["id"]))["status"] == "submitted"
    finally:
        await bridge.close()


@pytest.mark.parametrize("operation", ["inventory", "receive"])
@pytest.mark.parametrize(
    "field, changed",
    [
        ("pid", 99),
        ("cwd", "/different-project"),
        ("name", "different-process"),
        ("path", "/different/python"),
        ("script", "/different/peer.py"),
        ("user_id", -1),
        ("host_id", "other-host"),
        ("instance_id", "different-incarnation"),
    ],
)
async def test_native_positive_identity_change_still_invalidates(
    native_route, operation, field, changed
):
    bridge, script, session, block = native_route
    script.foreground = {
        **script.foreground,
        "path": "/usr/bin/python",
        "script": "/project/peer.py",
    }
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    task = await bridge.rex.submit(peer["peer_id"], "work", "work")
    if field == "host_id":
        script.block = {**script.block, field: changed}
    elif field == "instance_id":
        script.identity = {**script.identity, field: changed}
    else:
        # Positive changed evidence remains authoritative even when a different
        # formerly known optional fact disappears in the same observation.
        script.foreground = {**script.foreground, field: changed}
        script.foreground.pop("script" if field != "script" else "path")
    try:
        if operation == "inventory":
            await bridge.rex.inventory()
        else:
            with pytest.raises(ValueError, match="binding changed"):
                await bridge.rex_receive(peer["peer_id"])
        assert bridge.rex.mailbox.get(peer["peer_id"])["state"] == "stale"
        assert bridge.store.get(task["id"])["status"] == "uncertain"
    finally:
        await bridge.close()


async def test_native_new_optional_evidence_verifies_without_replacing_enrollment(native_route):
    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    script.foreground = {
        **script.foreground,
        "path": "/usr/bin/python",
        "script": "/project/peer.py",
    }
    try:
        assert await bridge.rex.verify(peer["peer_id"]) == peer
        inventory = await bridge.rex.inventory()
        assert inventory["complete"]
        row = next(row for row in inventory["peers"] if row["peer_id"] == peer["peer_id"])
        assert row["verified"]
        assert bridge.rex.mailbox.get(peer["peer_id"]) == peer
    finally:
        await bridge.close()


async def test_native_complete_empty_inventory_cannot_invalidate_replacement(
    native_route, monkeypatch
):
    import asyncio
    import json

    from answerback_bridge import rex
    from answerback_bridge.process import Output

    bridge, script, session, block = native_route
    old = await bridge.rex_enroll("agent", session, block, "old")
    started, release = asyncio.Event(), asyncio.Event()
    original = script.run

    async def delayed(argv, **kwargs):
        if "call" in argv and "session.list" in argv:
            started.set()
            await release.wait()
            return Output(json.dumps({"sessions": []}), "")
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    discovery = asyncio.create_task(bridge.rex.inventory())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await bridge.rex_retire(old["peer_id"])
        script.foreground = {**script.foreground, "pid": 43}
        replacement = await bridge.rex_enroll("replacement", session, block, "replacement")
        task = await bridge.peer_submit(replacement["peer_id"], "work", "replacement-work")
        release.set()
        assert (await asyncio.wait_for(discovery, 1))["complete"]
        assert bridge.rex.mailbox.get(replacement["peer_id"])["state"] == "active"
        assert bridge.store.get(task["id"])["status"] == "queued"
    finally:
        release.set()
        await asyncio.gather(discovery, return_exceptions=True)
        await bridge.close()


@pytest.mark.parametrize("creator", ["missing", None, 42, {}, []])
async def test_native_unknown_creator_cannot_authorize_absence(native_route, creator):
    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    task = await bridge.peer_submit(peer["peer_id"], "work", "work")
    row = {"block_id": block}
    if creator != "missing":
        row["creator_name"] = creator
    script.overrides = {"session.list_blocks": {"blocks": [row]}}
    try:
        assert not (await bridge.rex.inventory())["complete"]
        assert bridge.rex.mailbox.get(peer["peer_id"])["state"] == "active"
        assert bridge.store.get(task["id"])["status"] == "queued"
    finally:
        await bridge.close()


@pytest.mark.parametrize("returned_id", ["suffix", "", "block:not-a-uuid"])
async def test_native_malformed_block_id_keeps_mailbox_active(native_route, returned_id):
    from answerback_bridge.process import BridgeError
    from answerback_bridge.rex import RexIdentityChanged

    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    task = await bridge.peer_submit(peer["peer_id"], "work", "work")
    script.overrides = {
        "session.describe_block": {"block": {**script.block, "block_id": returned_id}}
    }
    try:
        with pytest.raises(BridgeError) as caught:
            await bridge.rex_receive(peer["peer_id"])
        assert not isinstance(caught.value, RexIdentityChanged)
        assert bridge.rex.mailbox.get(peer["peer_id"])["state"] == "active"
        assert bridge.store.get(task["id"])["status"] == "queued"
    finally:
        await bridge.close()


@pytest.mark.parametrize("operation", ["inventory", "receive"])
@pytest.mark.parametrize("absent", ["session", "block"])
async def test_native_restart_concurrent_probe_cannot_authorize_absence(
    native_route, monkeypatch, operation, absent
):
    import asyncio
    import json

    from answerback_bridge import rex
    from answerback_bridge.process import BridgeError, Output

    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    task = await bridge.peer_submit(peer["peer_id"], "work", "work")
    started, release = asyncio.Event(), asyncio.Event()
    original = script.run
    method = "session.list" if absent == "session" else "session.list_blocks"
    key = "sessions" if absent == "session" else "blocks"
    if operation == "receive":
        script.overrides = {"session.describe_block": BridgeError("lookup failed")}

    async def delayed(argv, **kwargs):
        if "call" in argv and method in argv:
            started.set()
            await release.wait()
            return Output(json.dumps({key: []}), "")
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    pending = asyncio.create_task(
        bridge.rex.inventory() if operation == "inventory" else bridge.rex_receive(peer["peer_id"])
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        script.identity = {**script.identity, "instance_id": "replacement"}
        # A concurrent public probe updates the shared adapter to server B
        # while the outstanding absence operation still belongs to server A.
        await bridge.rex.adapter.probe()
        release.set()
        if operation == "inventory":
            assert not (await asyncio.wait_for(pending, 1))["complete"]
        else:
            with pytest.raises(BridgeError) as caught:
                await asyncio.wait_for(pending, 1)
            assert not isinstance(caught.value, rex.RexIdentityChanged)
        assert bridge.rex.mailbox.get(peer["peer_id"])["state"] == "active"
        assert bridge.store.get(task["id"])["status"] == "queued"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await bridge.close()


@pytest.mark.parametrize("concurrent_probe", [False, True])
@pytest.mark.parametrize("operation", ["verify", "receive", "ack", "complete", "submit"])
async def test_successful_collection_restart_cannot_authorize_mailbox(
    native_route, monkeypatch, concurrent_probe, operation
):
    import asyncio

    from answerback_bridge import rex
    from answerback_bridge.process import BridgeError

    bridge, script, session, block = native_route
    peer = await bridge.rex_enroll("agent", session, block, "enroll")
    peer_id = peer["peer_id"]
    task = await bridge.peer_submit(peer_id, "work", "work")
    if operation == "complete":
        await bridge.rex_ack(peer_id, task["id"])
    started, release = asyncio.Event(), asyncio.Event()
    original = script.run

    async def delayed(argv, **kwargs):
        if "call" in argv and "session.describe_block" in argv:
            started.set()
            await release.wait()
        return await original(argv, **kwargs)

    async def authorize():
        if operation == "verify":
            return await bridge.rex.verify(peer_id)
        if operation == "receive":
            return await bridge.rex_receive(peer_id)
        if operation == "ack":
            return await bridge.rex_ack(peer_id, task["id"])
        if operation == "complete":
            return await bridge.rex_complete(peer_id, task["id"], "result")
        return await bridge.peer_submit(peer_id, "new work", "new")

    monkeypatch.setattr(rex, "run", delayed)
    pending = asyncio.create_task(authorize())
    try:
        await asyncio.wait_for(started.wait(), 1)
        script.identity = {**script.identity, "instance_id": "replacement"}
        if concurrent_probe:
            assert (await bridge.rex.adapter.probe())["server_instance_id"] == "replacement"
        release.set()
        with pytest.raises(BridgeError) as caught:
            await asyncio.wait_for(pending, 1)
        assert not isinstance(caught.value, rex.RexIdentityChanged)
        # Mixed observations are uncertainty, not authority to mutate durable state.
        assert bridge.rex.mailbox.get(peer_id)["state"] == "active"
        assert bridge.store.get(task["id"])["status"] == (
            "submitted" if operation == "complete" else "queued"
        )
        with bridge.store.connection() as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        # A later coherent observation of B still invalidates enrollment A.
        with pytest.raises(ValueError, match="binding changed"):
            await bridge.rex.verify(peer_id)
        assert bridge.rex.mailbox.get(peer_id)["state"] == "stale"
        assert bridge.store.get(task["id"])["status"] == "uncertain"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await bridge.close()
