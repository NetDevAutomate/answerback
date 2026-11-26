"""Core send policy: reported-`done` terminals and allow-listed remote machines.

Labels of fix round `ans-fix1` (review M1) say what a test shows at the start
commit `e84b70b`, where `hc_send_to_done` was on by default:
`red_at_pretip` fails there, `pins_existing` passes there on purpose, and
`same_path` passes there and now names the setting it used to take from a default.
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from routing_fakes import PROJECT, policy

from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError
from answerback_bridge.providers.herdr.core import (
    Core,
    CoreIdentityChanged,
    CoreNotReady,
    CoreNotSent,
    CoreTypedWhileWorking,
)
from answerback_bridge.routing.route import _idle_peers
from answerback_bridge.service import Bridge


def row(address: str = "t1", **changes: Any) -> dict[str, Any]:
    """Return one `hc get --json` terminal row for a recognized Claude agent."""
    return {
        "address": address,
        "argv": ["/bin/zsh"],
        "pid": 100,
        "created_ms": 1794570000000,
        "cwd": "/work/launch",
        "foreground": {
            "pid": 101,
            "name": "claude",
            "argv": ["claude"],
            "cwd": "/work/Analytics",
        },
        "state": "live",
        "status": {"app": "claude", "state": "done"},
        "turn": 3,
        "labels": {"herdr.seen": "3"},
        **changes,
    }


@dataclass
class FakeCore:
    """A scripted `hc` executable that records every argv it receives."""

    binary: Path
    settings_path: Path
    log_path: Path

    def update(self, **changes: Any) -> None:
        current = json.loads(self.settings_path.read_text())
        self.settings_path.write_text(json.dumps(current | changes))

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def sends(self) -> list[list[str]]:
        return [args for args in self.calls() if args[0] == "send"]


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    cli = FakeCore(tmp_path / "hc", tmp_path / "settings.json", tmp_path / "calls.jsonl")
    cli.settings_path.write_text(
        json.dumps(
            {
                "terminals": {"t1": row()},
                "send": {"turn": 4, "started": True, "status": "done"},
                "after_send": {"status": {"app": "claude", "state": "working"}, "turn": 4},
            }
        )
    )
    cli.binary.write_text(f"""#!{sys.executable}
import json
import sys
from pathlib import Path

settings_path = Path({str(cli.settings_path)!r})
settings = json.loads(settings_path.read_text())
args = sys.argv[1:]
with Path({str(cli.log_path)!r}).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["ls", "--json"]:
    value = settings.get("inventory", {{"terminals": [], "machines": []}})
elif args == ["machine", "ls", "--json"]:
    value = {{"machines": [{{"name": "example-host", "state": "connected", "id": "a" * 32}}]}}
elif args[0] == "get" and args[-1] == "--json":
    value = settings["terminals"].get(args[1])
    if value is None:
        sys.exit(4)
elif args[0] == "send" and "--" in args and "--force" not in args[: args.index("--")]:
    target = args[-2]
    if settings.get("send_error") is not None:
        print(json.dumps({{"error": {{"kind": settings["send_error"]}}}}), file=sys.stderr)
        sys.exit(3)
    value = settings["send"]
    if settings.get("after_send"):
        settings["terminals"][target].update(settings["after_send"])
        settings_path.write_text(json.dumps(settings))
else:
    sys.exit(2)
print(json.dumps(value))
""")
    cli.binary.chmod(0o700)
    return cli


def done_core(fake: FakeCore) -> Core:
    """A Core with `send_to_done` on, named explicitly.

    same_path: every test that calls this passes at `e84b70b`, where it built
    `Core(binary)` and took the same value from a default.
    """
    return Core(str(fake.binary), send_to_done=True)


# Behaviour 1: with `send_to_done` on, a reported-`done` terminal can receive an assignment.


async def test_done_terminal_with_every_other_check_is_ready(fake: FakeCore) -> None:
    peer = await done_core(fake).peer("t1")
    assert peer["state"] == "done"
    assert peer["send_ready"] is True
    assert peer["send_readiness_reason"] == "ready"
    assert peer["capabilities"] == ["read", "send", "worker_report"]


async def test_core_built_without_the_keyword_does_not_send_to_a_done_terminal(
    fake: FakeCore,
) -> None:
    # red_at_pretip (fix round `ans-fix2`, review 2 finding B1): at `0b4c2707`
    # `Core(binary)` took `send_to_done=True` from the constructor, reported the
    # same terminal `send_ready` and typed the text once, without `--if`.
    core = Core(str(fake.binary))
    assert core.send_to_done is False
    peer = await core.peer("t1")
    assert peer["state"] == "done"
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "done"
    assert peer["capabilities"] == ["read"]
    with pytest.raises(CoreNotReady, match="reported idle$"):
        await core.send("t1", "Review", expected=peer)
    assert fake.sends() == []


async def test_done_terminal_is_not_ready_when_the_switch_is_off(fake: FakeCore) -> None:
    core = Core(str(fake.binary), send_to_done=False)
    peer = await core.peer("t1")
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "done"
    assert peer["capabilities"] == ["read"]
    with pytest.raises(CoreNotReady):
        await core.send("t1", "Review", expected=peer)
    assert fake.sends() == []


async def test_unreadable_terminal_before_send_is_not_sent(fake: FakeCore) -> None:
    """T-226 item 4: a failed `hc get` before `hc send` is CoreNotSent, nothing typed."""
    fake.update(terminals={"example-host/t17": remote()})
    core = Core(str(fake.binary), send_machines=["example-host"])
    peer = await core.peer("example-host/t17")
    fake.update(terminals={})
    with pytest.raises(CoreNotSent, match="nothing was sent"):
        await core.send("example-host/t17", "Review", expected=peer, machine_id="a" * 32)
    assert fake.sends() == []


async def test_remote_peer_never_advertises_worker_report(fake: FakeCore) -> None:
    """T-226 item 1: a remote worker cannot reach this bridge's socket.

    It reports through the result file this bridge reads, so `worker_report` would
    promise a `task_complete` callback that the topology cannot carry.
    """
    fake.update(terminals={"example-host/t17": remote()})
    peer = await Core(str(fake.binary), send_machines=["example-host"]).peer("example-host/t17")
    assert peer["send_ready"] is True
    assert "worker_report" not in peer["capabilities"]
    assert "result_file" in peer["capabilities"]


async def test_peer_names_its_machine_and_offers_no_cancel(fake: FakeCore) -> None:
    """T-226 item 2: callers read `machine` and `cancel` instead of guessing.

    Every field a peer had before stays, with its value; a read-only remote peer
    names its machine too.
    """
    fake.update(
        terminals={"example-host/t17": remote(), "studio/t2": remote("studio/t2"), "t1": row()}
    )
    core = Core(str(fake.binary), send_machines=["example-host"])
    listed, unlisted, local = (
        await core.peer("example-host/t17"),
        await core.peer("studio/t2"),
        await core.peer("t1"),
    )
    assert (listed["machine"], unlisted["machine"], local["machine"]) == (
        "example-host",
        "studio",
        None,
    )
    assert listed["cancel"] is unlisted["cancel"] is local["cancel"] is False
    assert {"peer_id", "target", "send_ready", "binding", "capabilities"} <= set(local)
    assert listed["target"] == "example-host/t17"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": {"app": "claude", "state": "working"}}, "working"),
        ({"status": {"app": "claude", "state": "blocked"}}, "blocked"),
        ({"status": {"app": "claude", "state": "error"}}, "error"),
        ({"status": None}, "unreported"),
        ({"state": "finished"}, "not_live"),
        ({"status": {"app": "kiro-cli", "state": "done"}}, "status_mismatch"),
    ],
)
async def test_done_policy_leaves_other_states_and_checks_unchanged(
    fake: FakeCore, changes: dict[str, Any], reason: str
) -> None:
    fake.update(terminals={"t1": row(**changes)})
    peer = await done_core(fake).peer("t1")
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == reason


async def test_done_terminal_without_current_directory_is_not_ready(fake: FakeCore) -> None:
    terminal = row()
    terminal["foreground"]["cwd"] = None
    fake.update(terminals={"t1": terminal})
    peer = await done_core(fake).peer("t1")
    assert peer["send_readiness_reason"] == "current_directory_unknown"


async def test_send_to_done_omits_the_idle_condition_and_never_forces(fake: FakeCore) -> None:
    core = done_core(fake)
    selected = await core.peer("t1")
    receipt = await core.send("t1", "Review", expected=selected)
    assert receipt == {
        "target": "t1",
        "turn": 4,
        "started": True,
        "status": "done",
        "identity_guard": "observed_before_and_after",
        "identity_observer": "core:local",
    }
    assert fake.sends() == [["send", "--json", "--", "t1", "Review"]]
    calls = fake.calls()
    assert calls[-2:] == [["send", "--json", "--", "t1", "Review"], ["get", "t1", "--json"]]
    assert calls[-3] == ["get", "t1", "--json"]


async def test_send_to_idle_still_uses_the_idle_condition(fake: FakeCore) -> None:
    fake.update(
        terminals={"t1": row(status={"app": "claude", "state": "idle"})},
        send={"turn": 4, "started": True, "status": "working"},
    )
    core = Core(str(fake.binary))
    await core.send("t1", "Review", expected=await core.peer("t1"))
    assert fake.sends() == [["send", "--if", "idle", "--json", "--", "t1", "Review"]]


async def test_done_send_uses_the_state_observed_just_before_sending(fake: FakeCore) -> None:
    fake.update(terminals={"t1": row(status={"app": "claude", "state": "idle"})})
    core = done_core(fake)
    selected = await core.peer("t1")
    fake.update(terminals={"t1": row()})
    await core.send("t1", "Review", expected=selected)
    assert fake.sends() == [["send", "--json", "--", "t1", "Review"]]


@pytest.mark.parametrize("status", ["working", "blocked", "error", None, "unknown"])
async def test_done_send_receipt_with_unexpected_status_is_uncertain(
    fake: FakeCore, status: str | None
) -> None:
    # T-187: the identity check after the send still runs, and the same agent being
    # there makes this the typed-while-working case, carrying the receipt and turns.
    fake.update(send={"turn": 4, "started": True, "status": status})
    core = done_core(fake)
    selected = await core.peer("t1")
    with pytest.raises(BridgeError, match="uncertain") as error:
        await core.send("t1", "Review", expected=selected)
    assert not isinstance(error.value, CoreNotReady)
    assert isinstance(error.value, CoreTypedWhileWorking)
    assert error.value.receipt == {
        "target": "t1",
        "turn": 4,
        "started": True,
        "status": status,
        "identity_guard": "observed_before_and_after",
        "identity_observer": "core:local",
    }
    assert (error.value.turn_before, error.value.turn_after) == (3, 4)
    assert len(fake.sends()) == 1
    assert fake.calls()[-1] == ["get", "t1", "--json"]


async def test_done_send_into_a_replaced_agent_is_an_identity_change_not_a_race(
    fake: FakeCore,
) -> None:
    # A `working` receipt plus a different foreground after the send is the
    # replaced-agent case, which keeps invalidating the submission.
    fake.update(
        send={"turn": 4, "started": False, "status": "working"},
        after_send={
            "foreground": {"pid": 999, "argv": ["claude"], "cwd": "/work/Analytics"},
            "status": {"app": "claude", "state": "working"},
            "turn": 4,
        },
    )
    core = done_core(fake)
    selected = await core.peer("t1")
    with pytest.raises(CoreIdentityChanged, match="uncertain") as error:
        await core.send("t1", "Review", expected=selected)
    assert not isinstance(error.value, CoreTypedWhileWorking)
    assert len(fake.sends()) == 1


@pytest.mark.parametrize(
    ("changes", "turn"),
    [
        ({}, 3),
        ({"turn": 0}, 0),
        ({"turn": None}, None),
        ({"turn": -1}, None),
        ({"turn": "3"}, None),
    ],
)
async def test_peer_reports_the_turn_counter_outside_the_binding(
    fake: FakeCore, changes: dict[str, Any], turn: int | None
) -> None:
    reference = await done_core(fake).peer("t1")
    terminal = row(**changes)
    if "turn" in changes and changes["turn"] is None:
        del terminal["turn"]
    fake.update(terminals={"t1": terminal})
    peer = await done_core(fake).peer("t1")
    assert peer["turn"] == turn
    assert peer["binding"] == reference["binding"]


@pytest.mark.parametrize("status", ["done", "idle"])
async def test_done_send_receipt_accepts_done_or_idle(fake: FakeCore, status: str) -> None:
    fake.update(send={"turn": 4, "started": True, "status": status})
    core = done_core(fake)
    receipt = await core.send("t1", "Review", expected=await core.peer("t1"))
    assert receipt["status"] == status


async def test_done_send_after_check_detects_a_replaced_agent(fake: FakeCore) -> None:
    fake.update(
        after_send={"foreground": {"pid": 999, "argv": ["claude"], "cwd": "/work/Analytics"}}
    )
    core = done_core(fake)
    selected = await core.peer("t1")
    with pytest.raises(CoreIdentityChanged, match="uncertain"):
        await core.send("t1", "Review", expected=selected)
    assert len(fake.sends()) == 1


def test_config_switch_defaults_off_and_rejects_non_booleans() -> None:
    # red_at_pretip: at `e84b70b` the default is True.
    assert Config().hc_send_to_done is False
    assert Config(hc_send_to_done=True).hc_send_to_done is True
    assert Config(hc_send_to_done=False).hc_send_to_done is False
    with pytest.raises(ValueError):
        Config(hc_send_to_done="no")  # type: ignore[arg-type]


def test_default_send_to_done_is_not_written_by_an_unrelated_save(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    # red_at_pretip: at `e84b70b` the value omitted from a save is True, so a saved
    # True is absent from the file and the default that loads back is True.
    Config(provider_selection=[]).save(path)
    assert "hc" not in json.loads(path.read_text())["providers"]
    assert Config.load(path).hc_send_to_done is False
    Config(provider_selection=[], hc_send_to_done=True).save(path)
    assert json.loads(path.read_text())["providers"]["hc"]["send_to_done"] is True
    assert Config.load(path).hc_send_to_done is True


# Behaviour 2: remote terminals accept assignments only from allow-listed machines.


def remote(address: str = "example-host/t17", **changes: Any) -> dict[str, Any]:
    """Return a remote row; `hc get` reports the same identity fields as locally."""
    return row(address, **({"status": {"app": "claude", "state": "idle"}} | changes))


async def test_remote_terminal_is_read_only_by_default(fake: FakeCore) -> None:
    fake.update(terminals={"example-host/t17": remote()})
    core = Core(str(fake.binary))
    peer = await core.peer("example-host/t17")
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "remote_read_only"
    assert peer["capabilities"] == ["read"]
    with pytest.raises(CoreNotReady):
        await core.send("example-host/t17", "Review", expected=peer)
    assert fake.sends() == []


async def test_allow_listed_remote_terminal_is_ready_and_receives_the_send(
    fake: FakeCore,
) -> None:
    fake.update(
        terminals={
            "example-host/t17": remote(),
            "t1": row(status={"app": "claude", "state": "idle"}),
        },
        send={"turn": 4, "started": True, "status": "idle"},
    )
    core = Core(str(fake.binary), send_machines=["example-host"])
    peer = await core.peer("example-host/t17")
    local = await core.peer("t1")
    assert peer["send_ready"] is True
    assert peer["send_readiness_reason"] == "ready"
    assert peer["capabilities"] == ["read", "send", "result_file"]
    assert local["capabilities"] == ["read", "send", "worker_report"]
    receipt = await core.send("example-host/t17", "Review", expected=peer, machine_id="a" * 32)
    assert receipt["target"] == "example-host/t17"
    # T-226 item 3: same guard word, but the observer names the remote core.
    assert receipt["identity_guard"] == "observed_before_and_after"
    assert receipt["identity_observer"] == "core:example-host"
    assert fake.sends() == [["send", "--if", "idle", "--json", "--", "example-host/t17", "Review"]]
    # T-226 fix round 1 (F1): the core id is read right before and right after `hc send`.
    assert fake.calls()[-4:] == [
        ["machine", "ls", "--json"],
        ["send", "--if", "idle", "--json", "--", "example-host/t17", "Review"],
        ["get", "example-host/t17", "--json"],
        ["machine", "ls", "--json"],
    ]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": {"app": "claude", "state": "working"}}, "working"),
        ({"status": None}, "unreported"),
        ({"state": "finished"}, "not_live"),
    ],
)
async def test_allow_listed_remote_terminal_uses_local_readiness_rules(
    fake: FakeCore, changes: dict[str, Any], reason: str
) -> None:
    fake.update(terminals={"example-host/t17": remote(**changes)})
    peer = await Core(str(fake.binary), send_machines=["example-host"]).peer("example-host/t17")
    assert peer["send_readiness_reason"] == reason


async def test_allow_listed_remote_terminal_has_no_local_pid(fake: FakeCore) -> None:
    fake.update(terminals={"example-host/t17": remote()})
    core = Core(str(fake.binary), send_machines=["example-host"])
    peer = await core.peer("example-host/t17")
    assert await core.trace_pid("example-host/t17", peer["binding"]) is None


@pytest.mark.parametrize("address", ["studio/t17", "Example-Host/t17", "example-host2/t17"])
async def test_machine_not_on_the_list_is_refused(fake: FakeCore, address: str) -> None:
    fake.update(terminals={address: remote(address)})
    core = Core(str(fake.binary), send_machines=["example-host"])
    peer = await core.peer(address)
    assert peer["send_readiness_reason"] == "remote_read_only"
    with pytest.raises(CoreNotReady):
        await core.send(address, "Review", expected=peer)
    assert fake.sends() == []


async def test_send_rechecks_the_allow_list_when_peer_is_bypassed(fake: FakeCore) -> None:
    fake.update(terminals={"example-host/t17": remote()})
    selected = await Core(str(fake.binary), send_machines=["example-host"]).peer("example-host/t17")
    before = len(fake.calls())
    with pytest.raises(CoreNotReady):
        await Core(str(fake.binary)).send("example-host/t17", "Review", expected=selected)
    assert fake.calls()[before:] == []


def test_config_machine_list_defaults_empty_and_is_omitted_at_default(tmp_path: Path) -> None:
    assert Config().hc_send_machines == []
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    assert "hc_send_machines" not in json.loads(path.read_text())
    Config(provider_selection=[], hc_send_machines=["example-host"]).save(path)
    assert Config.load(path).hc_send_machines == ["example-host"]


@pytest.mark.parametrize("state", ["done", "idle"])
async def test_message_starting_with_dashes_is_text_after_the_separator(
    fake: FakeCore, state: str
) -> None:
    fake.update(
        terminals={"t1": row(status={"app": "claude", "state": state})},
        send={"turn": 4, "started": True, "status": state},
    )
    core = done_core(fake)
    await core.send("t1", "--force", expected=await core.peer("t1"))
    (sent,) = fake.sends()
    separator = sent.index("--")
    assert "--force" not in sent[:separator]
    assert sent[separator + 1 :] == ["t1", "--force"]


# Fix round 1: the running bridge honours both settings.


def bridge_config(fake: FakeCore, **changes: Any) -> Config:
    return Config(hc_enabled=True, codex_enabled=False, hc_binary=str(fake.binary), **changes)


async def test_bridge_with_switch_off_refuses_a_done_terminal(
    fake: FakeCore, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    bridge_config(fake, hc_send_to_done=False).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("t1")
        assert peer["send_ready"] is False
        assert peer["send_readiness_reason"] == "done"
    finally:
        await bridge.close()


def project_terminal(fake: FakeCore) -> None:
    """Put the `done` terminal in the routing fixture's project.

    The row is the `hc get --json` form of `row()`; only the foreground directory
    differs, so that routing's project rule cannot be what excludes the peer.
    """
    terminal = row()
    terminal["foreground"]["cwd"] = PROJECT
    fake.update(terminals={"t1": terminal})


async def test_idle_status_mismatch_fails_receipt_and_releases_terminal(
    fake: FakeCore, tmp_path: Path
) -> None:
    terminal = row(status={"app": "claude", "state": "idle"})
    terminal["foreground"]["cwd"] = PROJECT
    fake.update(terminals={"t1": terminal}, send_error="status_mismatch")
    bridge = Bridge(bridge_config(fake), tmp_path / "config.json")
    try:
        peer = await bridge.core.peer("t1")
        task = await bridge.peer_submit(
            "hc:t1", "Review", "status-mismatch", binding=peer["binding"]
        )
        await asyncio.gather(*list(bridge.core_tasks.jobs.values()))
        refused = bridge.store.get(task["id"])
        assert refused["status"] == "failed"
        assert refused["detail"] == ("Core refused: status changed before typing; nothing was sent")

        fake.update(send_error=None)
        retry = await bridge.peer_submit(
            "hc:t1", "Review again", "status-mismatch-retry", binding=peer["binding"]
        )
        await asyncio.gather(*list(bridge.core_tasks.jobs.values()))
        assert retry["id"] != task["id"]
        assert bridge.store.get(retry["id"])["status"] == "submitted"
    finally:
        await bridge.close()


async def test_other_idle_send_exit_three_remains_uncertain(fake: FakeCore, tmp_path: Path) -> None:
    terminal = row(status={"app": "claude", "state": "idle"})
    terminal["foreground"]["cwd"] = PROJECT
    fake.update(terminals={"t1": terminal}, send_error="blocked")
    bridge = Bridge(bridge_config(fake), tmp_path / "config.json")
    try:
        peer = await bridge.core.peer("t1")
        task = await bridge.peer_submit(
            "hc:t1", "Review", "other-exit-three", binding=peer["binding"]
        )
        await asyncio.gather(*list(bridge.core_tasks.jobs.values()))
        receipt = bridge.store.get(task["id"])
        assert receipt["status"] == "uncertain"
        assert receipt["detail"] == (
            "Core delivery unverified (BridgeError); inspect before retrying"
        )
    finally:
        await bridge.close()


async def test_with_nothing_set_a_done_terminal_is_refused_and_not_reused(
    fake: FakeCore, tmp_path: Path
) -> None:
    # red_at_pretip: at `e84b70b` the same terminal is `send_ready` with reason
    # `ready`, the direct send types once, and routing lists it for reuse.
    project_terminal(fake)
    path = tmp_path / "config.json"
    bridge_config(fake).save(path)
    assert "hc_send_to_done" not in json.loads(path.read_text())
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("t1")
        assert peer["state"] == "done"
        assert peer["send_ready"] is False
        assert peer["send_readiness_reason"] == "done"
        assert peer["capabilities"] == ["read"]
        with pytest.raises(CoreNotReady, match="reported idle$"):
            await bridge.core.send("t1", "Review", expected=peer)
        with pytest.raises(ValueError, match="Core agent is not ready: done"):
            await bridge.peer_submit("hc:t1", "Review", "nothing-set", binding=peer["binding"])
        assert fake.sends() == []
        assert bridge.store.by_request("nothing-set") is None
        assert _idle_peers(policy(), [peer], set(), {}) == {}
    finally:
        await bridge.close()


async def test_bridge_with_switch_on_makes_a_done_terminal_ready_and_reusable(
    fake: FakeCore, tmp_path: Path
) -> None:
    # pins_existing: passes at `e84b70b`. It is the control of the test above: the
    # one difference is the setting, so the refusal there comes from the default.
    project_terminal(fake)
    path = tmp_path / "config.json"
    bridge_config(fake, hc_send_to_done=True).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("t1")
        assert peer["send_ready"] is True
        assert peer["send_readiness_reason"] == "ready"
        assert _idle_peers(policy(), [peer], set(), {}) == {"claude": [peer]}
        receipt = await bridge.core.send("t1", "Review", expected=peer)
        assert receipt["status"] == "done"
        assert fake.sends() == [["send", "--json", "--", "t1", "Review"]]
    finally:
        await bridge.close()


async def test_bridge_with_allow_listed_machine_reports_its_terminal_ready(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.update(terminals={"example-host/t17": remote()})
    path = tmp_path / "config.json"
    bridge_config(fake, hc_send_machines=["example-host"]).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("example-host/t17")
        assert peer["send_ready"] is True
        assert peer["send_readiness_reason"] == "ready"
    finally:
        await bridge.close()


async def test_reregistered_and_reconfigured_core_keep_the_send_policy(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.update(terminals={"t1": row(), "example-host/t17": remote()})
    path = tmp_path / "config.json"
    config = bridge_config(fake, hc_send_to_done=False, hc_send_machines=["example-host"])
    config.save(path)
    bridge = Bridge(config, path)
    try:
        bridge.provider_register("hc", binary=str(fake.binary))
        assert (await bridge.core.peer("t1"))["send_readiness_reason"] == "done"
        assert (await bridge.core.peer("example-host/t17"))["send_ready"] is True
        await bridge.hc_configure(binary=str(fake.binary))
        assert (await bridge.core.peer("t1"))["send_readiness_reason"] == "done"
        assert (await bridge.core.peer("example-host/t17"))["send_ready"] is True
    finally:
        await bridge.close()


# T-187: a `done` send that races the agent's own next turn settles on the
# worker's report instead of leaving the terminal busy and the result withheld.


async def settled_jobs(bridge: Bridge) -> None:
    jobs = list(bridge.core_tasks.jobs.values())
    async with asyncio.timeout(5):
        await asyncio.gather(*jobs)


async def test_done_send_racing_the_agents_turn_settles_on_the_workers_report(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.update(send={"turn": 4, "started": False, "status": "working"})
    path = tmp_path / "config.json"
    bridge_config(fake, hc_send_to_done=True).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("t1")
        task = await bridge.peer_submit("hc:t1", "Review", "race-turn", binding=peer["binding"])
        await settled_jobs(bridge)
        stored = bridge.store.get(task["id"])
        assert stored["status"] == "uncertain"
        assert stored["result"] is None
        assert "turn 3 before the send, 4 after" in stored["detail"]
        metadata = stored["metadata"]
        assert metadata["typed_while_working"] is True
        assert metadata["delivery_verified"] is True
        assert (metadata["turn_before"], metadata["turn_after"]) == (3, 4)
        assert metadata["delivery"] == {
            "target": "t1",
            "turn": 4,
            "started": False,
            "status": "working",
            "identity_guard": "observed_before_and_after",
            "identity_observer": "core:local",
        }
        assert "identity_invalidated" not in metadata
        (sent,) = fake.sends()
        assert sent[:4] == ["send", "--json", "--", "t1"]
        # The worker has the brief, with the task ID in it, and reports.
        assert f"Bridge task: {task['id']}" in sent[4]
        completed = bridge.complete(task["id"], "Reviewed")
        assert completed["status"] == "completed"
        assert completed["result"] == "Reviewed"
        assert bridge.store.get(task["id"])["metadata"].get("provisional_result") is None
        # The terminal is free again: a new assignment is admitted.
        fake.update(terminals={"t1": row()}, send={"turn": 5, "started": True, "status": "done"})
        peer = await bridge.core.peer("t1")
        again = await bridge.peer_submit("hc:t1", "Next", "after-race", binding=peer["binding"])
        assert again["id"] != task["id"]
        await settled_jobs(bridge)
        assert bridge.store.get(again["id"])["status"] == "submitted"
    finally:
        await bridge.close()


async def test_done_send_into_a_replaced_agent_stays_uncertain_and_refuses_the_report(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.update(
        send={"turn": 4, "started": False, "status": "working"},
        after_send={
            "foreground": {"pid": 999, "argv": ["claude"], "cwd": "/work/Analytics"},
            "status": {"app": "claude", "state": "working"},
            "turn": 4,
        },
    )
    path = tmp_path / "config.json"
    bridge_config(fake, hc_send_to_done=True).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        peer = await bridge.core.peer("t1")
        task = await bridge.peer_submit("hc:t1", "Review", "replaced", binding=peer["binding"])
        await settled_jobs(bridge)
        stored = bridge.store.get(task["id"])
        assert stored["status"] == "uncertain"
        assert stored["metadata"]["identity_invalidated"] is True
        assert "delivery_verified" not in stored["metadata"]
        assert "typed_while_working" not in stored["metadata"]
        with pytest.raises(ValueError, match="invalidated"):
            bridge.complete(task["id"], "Reviewed")
        assert bridge.store.get(task["id"])["status"] == "uncertain"
        # The terminal stays reserved until a person inspects the receipt.
        with pytest.raises(ValueError, match="already has task"):
            bridge.store.reserve(stored["target"], "Next", "after-replaced", {"kind": "hc"})
    finally:
        await bridge.close()
