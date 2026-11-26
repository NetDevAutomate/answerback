"""T-89: discovery reads the ``hc events`` follower behind the ``hc_events`` switch.

A scripted ``hc`` stands in for Herdr Core. It answers ``ls``, ``get``, ``machine ls``
and ``send`` from a settings file, and for ``events --json`` prints a snapshot (and,
on request, a burst of ``title`` events) and then stays running like the real
follower child, recording its PID so the tests can prove it is reaped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import answerback_bridge.providers.herdr.core as core_module
from answerback_bridge.config import Config
from answerback_bridge.providers.herdr.core import Core
from answerback_bridge.providers.herdr.core_events import EventFollower
from answerback_bridge.service import Bridge

# ---------------------------------------------------------------- fixtures


def row(address: str = "t1", **changes: Any) -> dict[str, Any]:
    """One terminal as both ``hc get --json`` and ``hc events --json`` report it."""
    return {
        "address": address,
        "argv": ["/bin/zsh"],
        "pid": 100,
        "created_ms": 1794570000000,
        "cwd": "/work/launch",
        "foreground": {"pid": 101, "name": "claude", "argv": ["claude"], "cwd": "/work/repo"},
        "state": "live",
        "status": {"app": "claude", "state": "done"},
        "turn": 3,
        "title": "",
        "labels": {"herdr.space": "Main", "herdr.tab": "Review"},
        **changes,
    }


def mixed_rows() -> list[dict[str, Any]]:
    """Live terminals covering every readiness branch a listing can produce."""
    shell = {"pid": 201, "name": "zsh", "argv": ["/bin/zsh"], "cwd": "/home"}
    node = {"pid": 202, "name": "node", "argv": ["node", "server.js"], "cwd": "/w"}
    return [
        row("t1"),
        row("t2", status={"app": "claude", "state": "idle"}, labels={"where": "Desk"}),
        row("t3", foreground=shell, status=None),
        row("t4", foreground=None, status={"app": "codex", "state": "idle"}),
        row("t5", status={"app": "claude", "state": "working"}),
        row("t6", status=None),
        row("t7", foreground=node, status={"app": "claude", "state": "idle"}),
        row("t8", status={"app": "codex", "state": "idle"}),
        row("t9", foreground={"pid": 203, "name": "claude", "argv": ["claude"], "cwd": "/w\x07"}),
        row("example-host/t17", status={"app": "claude", "state": "idle"}),
        row("studio/t3", status={"app": "claude", "state": "idle"}),
    ]


MACHINES = [
    {"name": "example-host", "state": "connected"},
    {"name": "studio", "state": "connected"},
    {"name": "offline", "state": "unreachable"},
]


def snapshot(seq: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"channel": None, "seq": seq, "terminals": rows}


def title_event(seq: int, address: str, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"seq": seq, "type": "title", "terminal": address, "data": text},
    }


@dataclass
class FakeCore:
    """A scripted ``hc`` executable that records every argv it receives."""

    binary: Path
    settings_path: Path
    log_path: Path
    pid_path: Path
    go_path: Path

    def update(self, **changes: Any) -> None:
        current = json.loads(self.settings_path.read_text())
        self.settings_path.write_text(json.dumps(current | changes))

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def polling_calls(self) -> list[list[str]]:
        """Every call except the long-lived ``events`` follower itself."""
        return [args for args in self.calls() if args[0] != "events"]

    def events_pids(self) -> list[int]:
        if not self.pid_path.exists():
            return []
        return [int(line) for line in self.pid_path.read_text().split()]

    def serve(self, rows: list[dict[str, Any]], *, finished: int = 0, seq: int = 7) -> None:
        """Answer ``ls``/``get`` and the event snapshot from the same terminal state."""
        live = [item for item in rows if item["state"] == "live"]
        self.update(
            inventory={"terminals": live, "machines": MACHINES, "hidden_finished": finished},
            terminals={item["address"]: item for item in rows},
            machines={"machines": MACHINES},
            events=[snapshot(seq, rows)],
        )


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    cli = FakeCore(
        tmp_path / "hc",
        tmp_path / "settings.json",
        tmp_path / "calls.jsonl",
        tmp_path / "events.pid",
        tmp_path / "go",
    )
    cli.settings_path.write_text(
        json.dumps(
            {
                "terminals": {},
                "inventory": {"terminals": [], "machines": [], "hidden_finished": 0},
                "machines": {"machines": []},
                "events": [snapshot(1, [])],
                "burst": [],
                "events_exit": False,
                "send": {"turn": 4, "started": True, "status": "done"},
            }
        )
    )
    cli.binary.write_text(f"""#!{sys.executable}
import json
import os
import sys
import time
from pathlib import Path

settings = json.loads(Path({str(cli.settings_path)!r}).read_text())
args = sys.argv[1:]
with Path({str(cli.log_path)!r}).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["events", "--json"]:
    with Path({str(cli.pid_path)!r}).open("a") as pids:
        pids.write(f"{{os.getpid()}}\\n")
    for message in settings["events"]:
        sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()
    if settings["events_exit"]:
        sys.exit(3)
    if settings["burst"]:
        while not Path({str(cli.go_path)!r}).exists():
            time.sleep(0.01)
        sys.stdout.write("".join(json.dumps(item) + "\\n" for item in settings["burst"]))
        sys.stdout.flush()
    while True:
        time.sleep(1)
if args == ["ls", "--json"]:
    value = settings["inventory"]
elif args == ["machine", "ls", "--json"]:
    value = settings["machines"]
elif args[0] == "get" and args[-1] == "--json":
    value = settings["terminals"].get(args[1])
    if value is None:
        sys.exit(4)
elif args[0] == "send" and "--" in args:
    value = settings["send"]
else:
    sys.exit(2)
print(json.dumps(value))
""")
    cli.binary.chmod(0o700)
    return cli


async def eventually(check: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def started(core: Core) -> Core:
    core.start_events()
    await eventually(lambda: core.events_state() == "running")
    return core


def bridge_config(fake: FakeCore, **changes: Any) -> Config:
    return Config(hc_enabled=True, codex_enabled=False, hc_binary=str(fake.binary), **changes)


# ------------------------------------------- behaviour 1: the config switch


def test_hc_events_defaults_off_and_rejects_non_booleans() -> None:
    assert Config().hc_events is False
    assert Config(hc_events=True).hc_events is True
    with pytest.raises(ValueError, match="events"):
        Config(hc_events="yes")  # type: ignore[arg-type]


def test_hc_events_is_written_only_when_switched_on(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    assert "hc" not in json.loads(path.read_text())["providers"]
    Config(provider_selection=[], hc_events=True).save(path)
    assert json.loads(path.read_text())["providers"]["hc"]["events"] is True
    assert Config.load(path).hc_events is True


def test_core_rejects_a_non_boolean_events_switch() -> None:
    with pytest.raises(ValueError, match="events"):
        Core("hc", events=1)  # type: ignore[arg-type]


# ------------------------------- behaviour 2: identical peers on both paths


async def test_event_view_and_polling_list_identical_peers(fake: FakeCore) -> None:
    finished = row("t30", state="finished", foreground=None, status=None)
    fake.serve([*mixed_rows(), finished], finished=1)
    # same_path (fix round `ans-fix2`): the rows report `done`; both cores name the
    # `send_to_done=True` that they took from the constructor's default at `0b4c2707`.
    polled = await Core(
        str(fake.binary), send_to_done=True, send_machines=["example-host"]
    ).inventory()
    poll_calls = fake.polling_calls()
    assert ["ls", "--json"] in poll_calls
    assert len([args for args in poll_calls if args[0] == "get"]) == len(mixed_rows())

    core = await started(
        Core(str(fake.binary), send_to_done=True, send_machines=["example-host"], events=True)
    )
    try:
        before = len(fake.polling_calls())
        viewed = await core.inventory()
        assert fake.polling_calls()[before:] == [["machine", "ls", "--json"]]
    finally:
        await core.close()

    assert viewed == polled
    reasons = {peer["target"]: peer["send_readiness_reason"] for peer in viewed["peers"]}
    assert reasons == {
        "t1": "ready",
        "t2": "ready",
        "t4": "foreground_unknown",
        "t5": "working",
        "t6": "unreported",
        "t7": "foreground_agent_unknown",
        "t8": "status_mismatch",
        "example-host/t17": "ready",
        "studio/t3": "remote_read_only",
    }
    assert viewed["excluded_non_agents"] == 1
    assert set(viewed["errors"]) == {"machine:offline", "t9"}
    assert viewed["excluded_finished"] == 1


async def test_send_ready_and_binding_match_peer_for_every_listed_terminal(
    fake: FakeCore,
) -> None:
    fake.serve(mixed_rows())
    core = await started(Core(str(fake.binary), send_machines=["example-host"], events=True))
    try:
        listed = (await core.inventory())["peers"]
        for peer in listed:
            exact = await core.peer(peer["target"])
            assert {key: exact[key] for key in ("peer_id", "binding", "send_ready")} == {
                key: peer[key] for key in ("peer_id", "binding", "send_ready")
            }
    finally:
        await core.close()


# --------------------------------------------- behaviour 2: fallback rules


async def test_switch_on_but_not_started_falls_back_to_polling(fake: FakeCore) -> None:
    fake.serve([row("t1")])
    core = Core(str(fake.binary), events=True)
    found = await core.inventory()
    assert [peer["target"] for peer in found["peers"]] == ["t1"]
    assert fake.polling_calls() == [["ls", "--json"], ["get", "t1", "--json"]]
    assert fake.events_pids() == []
    assert core.events_state() == "stale"


async def test_stale_follower_falls_back_to_polling(fake: FakeCore) -> None:
    fake.serve([row("t1")])
    fake.update(events=[], events_exit=True)
    core = Core(str(fake.binary), events=True)
    core.start_events()
    try:
        await eventually(lambda: len(fake.events_pids()) >= 1)
        await eventually(lambda: core.follower is not None and core.follower.restarts >= 1)
        assert core.events_state() == "stale"
        found = await core.inventory()
        assert [peer["target"] for peer in found["peers"]] == ["t1"]
        assert ["ls", "--json"] in fake.polling_calls()
    finally:
        await core.close()


async def test_snapshot_older_than_max_age_falls_back_to_polling(
    fake: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.serve([row("t1")])
    core = await started(Core(str(fake.binary), events=True))
    try:
        monkeypatch.setattr(core_module, "EVENTS_MAX_AGE", 0.05)
        await asyncio.sleep(0.2)
        assert core.events_state() == "stale"
        await core.inventory()
        assert fake.polling_calls() == [["ls", "--json"], ["get", "t1", "--json"]]
    finally:
        await core.close()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"title": "x" * 5000}, id="truncated"),
        pytest.param({"pid": None}, id="missing-pid"),
        pytest.param({"created_ms": None}, id="missing-created"),
        pytest.param({"argv": []}, id="missing-argv"),
        pytest.param(
            {"foreground": {"pid": 101, "name": "claude", "argv": ["claude"]}},
            id="missing-foreground-cwd",
        ),
        pytest.param({"status": {"state": "done"}}, id="missing-status-app"),
    ],
)
async def test_incomplete_or_truncated_record_falls_back_to_get_for_that_terminal(
    fake: FakeCore, changes: dict[str, Any]
) -> None:
    rows = [row("t1"), row("t2", **changes)]
    fake.serve(rows)
    exact = row("t2")
    fake.update(terminals={"t1": rows[0], "t2": exact})
    core = await started(Core(str(fake.binary), events=True))
    try:
        found = await core.inventory()
    finally:
        await core.close()
    assert fake.polling_calls() == [["machine", "ls", "--json"], ["get", "t2", "--json"]]
    expected = Core(str(fake.binary))._peer(exact, "t2")
    assert [peer for peer in found["peers"] if peer["target"] == "t2"] == [expected]


async def test_machine_list_failure_keeps_view_peers_and_reports_partial(
    fake: FakeCore,
) -> None:
    fake.serve([row("t1")])
    fake.update(machines={"unexpected": True})
    core = await started(Core(str(fake.binary), events=True))
    try:
        found = await core.inventory()
    finally:
        await core.close()
    assert [peer["target"] for peer in found["peers"]] == ["t1"]
    assert found["complete"] is False
    assert "machines" in found["errors"]


# ------------------------------- behaviour 3: send and peer still use `get`


async def test_send_and_peer_still_call_get_before_and_after_with_the_switch_on(
    fake: FakeCore,
) -> None:
    fake.serve([row("t1")])
    # same_path (fix round `ans-fix2`): the row reports `done`, so the send below
    # needs `send_to_done=True`, which it took from the default at `0b4c2707`.
    core = await started(Core(str(fake.binary), send_to_done=True, events=True))
    try:
        listed = (await core.inventory())["peers"][0]
        start = len(fake.polling_calls())
        peer = await core.peer("t1")
        assert fake.polling_calls()[start:] == [["get", "t1", "--json"]]
        start = len(fake.polling_calls())
        await core.send("t1", "Review", listed)
        calls = fake.polling_calls()[start:]
    finally:
        await core.close()
    assert peer["binding"] == listed["binding"]
    assert calls == [
        ["get", "t1", "--json"],
        ["send", "--json", "--", "t1", "Review"],
        ["get", "t1", "--json"],
    ]


# --------------------------------------- behaviour 4: title events are cheap


async def test_ten_thousand_title_events_cause_no_calls_wakeups_or_logs(
    fake: FakeCore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake.serve([row("t1")], seq=10)
    fake.update(burst=[title_event(11 + index, "t1", f"title {index}") for index in range(10_000)])
    notified: list[str] = []
    original = EventFollower._notify

    def counting(self: EventFollower, record: Any) -> None:
        notified.append(record.address)
        original(self, record)

    monkeypatch.setattr(EventFollower, "_notify", counting)
    core = await started(Core(str(fake.binary), events=True))
    follower = core.follower
    assert follower is not None
    checked: list[str] = []

    def predicate(record: Any) -> bool:
        checked.append(record.address)
        return True

    try:
        await core.inventory()
        calls = len(fake.calls())
        waiter = asyncio.create_task(follower.wait_for("t1", predicate, timeout=60))
        await asyncio.sleep(0)
        with caplog.at_level(logging.DEBUG):
            fake.go_path.touch()
            await eventually(lambda: follower.view.seq == 10_010)
        assert follower.view.terminals["t1"].title == "title 9999"
        assert len(fake.calls()) == calls
        assert notified == []
        assert checked == []
        assert not waiter.done()
        assert caplog.records == []
        assert core.events_state() == "running"
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    finally:
        await core.close()


# ----------------------------------------------- behaviour 5: lifecycle


async def test_switch_off_never_starts_a_follower_child(fake: FakeCore, tmp_path: Path) -> None:
    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        assert bridge.core.follower is None
        await bridge.core.inventory()
        await asyncio.sleep(0.3)
        assert fake.events_pids() == []
        assert all(args[0] != "events" for args in fake.calls())
        assert "events" not in bridge.provider_status["hc"]
    finally:
        await bridge.close()


async def test_bridge_start_and_stop_leave_no_follower_child(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake, hc_events=True).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        await eventually(lambda: bridge.core.events_state() == "running")
        (pid,) = fake.events_pids()
        assert alive(pid)
        assert bridge.provider_status["hc"]["events"] == "running"
    finally:
        await bridge.close()
    assert not alive(pid)
    assert bridge.provider_status["hc"]["events"] == "stale"


async def test_core_start_and_close_leave_no_child(fake: FakeCore) -> None:
    fake.serve([row("t1")])
    core = await started(Core(str(fake.binary), events=True))
    (pid,) = fake.events_pids()
    assert alive(pid)
    await core.close()
    assert not alive(pid)
    core.start_events()  # a closed Core never restarts its follower
    await asyncio.sleep(0.3)
    assert fake.events_pids() == [pid]


async def test_close_before_the_follower_first_runs_starts_no_child(fake: FakeCore) -> None:
    fake.serve([row("t1")])
    core = Core(str(fake.binary), events=True)
    core.start_events()
    await core.close()
    await asyncio.sleep(0.3)
    assert fake.events_pids() == []


async def test_bridge_init_failure_after_core_starts_no_child(
    fake: FakeCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("startup failed half way")

    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake, hc_events=True).save(path)
    monkeypatch.setattr("answerback_bridge.adapters.factory.build_adapters", broken)
    with pytest.raises(RuntimeError, match="half way"):
        Bridge(Config.load(path), path)
    await asyncio.sleep(0.3)
    assert fake.events_pids() == []


def test_startup_failure_after_the_bridge_is_built_leaves_no_child(
    fake: FakeCore, tmp_path: Path
) -> None:
    """The runtime may fail before it ever calls ``Bridge.close``; the loop's teardown reaps."""
    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake, hc_events=True).save(path)
    failure: list[BaseException] = []

    async def serve_then_fail() -> None:
        bridge = Bridge(Config.load(path), path)
        await eventually(lambda: bridge.core.events_state() == "running")
        raise RuntimeError("socket bind failed")

    def thread() -> None:
        try:
            asyncio.run(serve_then_fail())
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion below
            failure.append(error)

    worker = threading.Thread(target=thread)
    worker.start()
    worker.join(30)
    assert not worker.is_alive()
    assert [str(error) for error in failure] == ["socket bind failed"]
    (pid,) = fake.events_pids()
    assert not alive(pid)


async def test_reregistering_hc_replaces_and_stops_the_old_follower(
    fake: FakeCore, tmp_path: Path
) -> None:
    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake, hc_events=True).save(path)
    bridge = Bridge(Config.load(path), path)
    try:
        await eventually(lambda: len(fake.events_pids()) == 1)
        (first,) = fake.events_pids()
        bridge.provider_register("hc", binary=str(fake.binary))
        await eventually(lambda: len(fake.events_pids()) == 2 and not alive(first))
        await bridge.hc_configure(binary=str(fake.binary))
        await eventually(lambda: len(fake.events_pids()) == 3)
        second = fake.events_pids()[1]
        await eventually(lambda: not alive(second))
        assert bridge.core.follower is not None
    finally:
        await bridge.close()
    assert not any(alive(pid) for pid in fake.events_pids())


# ------------------------- fix round 1: unmodelled event types and no-loop swap


def unknown_event(seq: int, address: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"seq": seq, "type": "teleported", "terminal": address, "data": {"x": 1}},
    }


async def test_unknown_event_type_sends_only_that_terminal_to_get(fake: FakeCore) -> None:
    rows = [row("t1"), row("t2"), row("t3")]
    fake.serve(rows, seq=10)
    fake.update(
        burst=[
            unknown_event(11, "t2"),
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"seq": 12, "type": "created", "terminal": "t9", "data": row("t9")},
            },
        ]
    )
    fake.update(terminals={**{item["address"]: item for item in rows}, "t9": row("t9")})
    core = await started(Core(str(fake.binary), events=True))
    follower = core.follower
    assert follower is not None
    try:
        fake.go_path.touch()
        await eventually(lambda: follower.view.seq == 12)
        start = len(fake.polling_calls())
        found = await core.inventory()
        assert fake.polling_calls()[start:] == [
            ["machine", "ls", "--json"],
            ["get", "t2", "--json"],
        ]
        assert [peer["target"] for peer in found["peers"]] == ["t1", "t2", "t3", "t9"]
        assert found["peers"][1] == Core(str(fake.binary))._peer(row("t2"), "t2")
    finally:
        await core.close()


async def test_full_record_after_unknown_event_returns_terminal_to_the_view(
    fake: FakeCore,
) -> None:
    fake.serve([row("t1"), row("t2")], seq=10)
    fake.update(
        burst=[
            unknown_event(11, "t2"),
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"seq": 12, "type": "restarted", "terminal": "t2", "data": row("t2")},
            },
        ]
    )
    core = await started(Core(str(fake.binary), events=True))
    follower = core.follower
    assert follower is not None
    try:
        fake.go_path.touch()
        await eventually(lambda: follower.view.seq == 12)
        start = len(fake.polling_calls())
        await core.inventory()
        assert fake.polling_calls()[start:] == [["machine", "ls", "--json"]]
    finally:
        await core.close()


async def test_ten_thousand_unknown_events_cost_one_flag_and_one_get(
    fake: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import answerback_bridge.providers.herdr.core_events as core_events

    fake.serve([row("t1"), row("t2")], seq=10)
    fake.update(burst=[unknown_event(11 + index, "t2") for index in range(10_000)])
    rebuilt: list[str] = []
    original = core_events.replace

    def counting(record: Any, **changes: Any) -> Any:
        rebuilt.append(record.address)
        return original(record, **changes)

    monkeypatch.setattr(core_events, "replace", counting)
    core = await started(Core(str(fake.binary), events=True))
    follower = core.follower
    assert follower is not None
    try:
        fake.go_path.touch()
        await eventually(lambda: follower.view.seq == 10_010)
        assert rebuilt == ["t2"]
        assert follower.view.terminals["t2"].unverified is True
        assert follower.view.terminals["t1"].unverified is False
        assert core.events_state() == "running"
        start = len(fake.polling_calls())
        await core.inventory()
        gets = [args for args in fake.polling_calls()[start:] if args[0] == "get"]
        assert gets == [["get", "t2", "--json"]]
    finally:
        await core.close()


def test_replacing_core_without_a_running_loop_keeps_the_old_core(
    fake: FakeCore, tmp_path: Path
) -> None:
    """Callers guarantee a running loop; if one ever does not, nothing is swapped or leaked."""
    fake.serve([row("t1")])
    path = tmp_path / "config.json"
    bridge_config(fake, hc_events=True).save(path)
    bridge = Bridge(Config.load(path), path)
    old = bridge.core
    with pytest.raises(RuntimeError):
        bridge.provider_register("hc", binary=str(fake.binary))
    assert bridge.core is old
    asyncio.run(bridge.close())
    assert fake.events_pids() == []
