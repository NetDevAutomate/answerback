"""T-90: `answerback route` may choose terminals on allow-listed machines.

A remote terminal (`machine/tNN`) is a routing candidate only when its machine is
exactly in `hc_send_machines` and the bridge reports it `send_ready`. Local peers are
still preferred inside a harness. The default (no machines) routes exactly as before.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from routing_fakes import (
    PROJECT,
    FakeBridge,
    FakeHandoffs,
    FakeLayout,
    FakeLedger,
    FakeTerminals,
    World,
    policy,
)

from answerback_bridge.process import BridgeError
from answerback_bridge.routing import route as route_module
from answerback_bridge.routing.policy import Policy
from answerback_bridge.routing.pool import Pool, Registry, Worker
from answerback_bridge.routing.route import _idle_peers, route

DAY = "20261120"
DESIGN = "Build a new module with a schema across files."
REMOTE = "example-host/t5"


class LocalOnlyTerminals(FakeTerminals):
    """Like the real `HcTerminals.live`: `hc ls` rows on other machines are not listed."""

    async def live(self) -> list[dict[str, Any]]:
        return [row for row in await super().live() if "/" not in row["address"]]


def remote_peer(world: World, address: str = REMOTE, *, tool: str = "claude", **kw: Any) -> str:
    """A terminal on another machine that the scanner reports like any other peer."""
    world.spawn([tool], **kw)
    local = f"t{world._next:x}"
    row = world.terminals.pop(local)
    row["address"] = address
    world.terminals[address] = row
    return address


def make_pool(world: World, tmp_path: Path, policy_: Policy | None = None) -> Pool:
    return Pool(
        policy_ or policy(),
        LocalOnlyTerminals(world),
        FakeLayout(world),
        FakeBridge(world),
        Registry(tmp_path / "routing-workers.json"),
        FakeHandoffs(world),
        FakeLedger(world),
        now=world.now,
        sleep=world.sleep,
        poll_seconds=3.0,
    )


async def go(world: World, tmp_path: Path, **kwargs: Any) -> tuple[Pool, dict[str, Any]]:
    pool = make_pool(world, tmp_path, kwargs.pop("policy_", None))
    kwargs.setdefault("task", "T-90")
    kwargs.setdefault("lane", "a")
    return pool, await route(pool, pool.bridge, DESIGN, day=DAY, **kwargs)


# --- candidate selection ------------------------------------------------------------


async def test_default_config_drops_remote_peers_as_before(tmp_path: Path) -> None:
    world = World()
    remote_peer(world)
    _, result = await go(world, tmp_path, dry_run=True)
    assert result["action"] == "create" and result["peer"] is None


async def test_a_ready_remote_peer_on_a_listed_machine_is_a_candidate(tmp_path: Path) -> None:
    world = World()
    remote_peer(world)
    pool, result = await go(world, tmp_path, send_machines=["example-host"])
    assert result["status"] == "submitted" and result["action"] == "reuse"
    assert result["peer"] == f"hc:{REMOTE}"
    (sent,) = world.sent
    assert sent["selector"] == f"hc:{REMOTE}" and sent["provider"] == "hc"
    assert sent["request_id"] == f"ab-{DAY}-T-90-a-example-host-t5"
    assert not world.names("tab.new") and not world.names("hc.run")
    # A remote terminal is never a managed worker: no registry row is written for it.
    assert REMOTE not in pool.registry.load()


async def test_a_remote_peer_that_is_not_ready_is_not_a_candidate(tmp_path: Path) -> None:
    world = World()
    remote_peer(world, state="working")
    _, result = await go(world, tmp_path, send_machines=["example-host"], dry_run=True)
    assert result["action"] == "create"


async def test_the_bridge_send_ready_flag_is_required(tmp_path: Path) -> None:
    # The bridge reports remote_read_only when its own allow-list lacks the machine.
    world = World()
    remote_peer(world)
    peers = world.peers()
    for peer in peers:
        peer.update(send_ready=False, send_readiness_reason="remote_read_only")
    found = _idle_peers(policy(), peers, set(), {}, send_machines=("example-host",))
    assert found == {}


async def test_a_remote_peer_with_an_active_receipt_is_not_reused(tmp_path: Path) -> None:
    world = World()
    remote_peer(world)
    world.receipts["r0"] = {
        "id": "r0",
        "request_key": "by-hand",
        "target": f"hc:default:{REMOTE}",
        "status": "pending",
        "updated": world.clock,
    }
    _, result = await go(world, tmp_path, send_machines=["example-host"], dry_run=True)
    assert result["action"] == "create"


async def test_a_machine_not_on_the_list_is_dropped(tmp_path: Path) -> None:
    world = World()
    remote_peer(world, "studio/t5")
    _, result = await go(world, tmp_path, send_machines=["example-host"], dry_run=True)
    assert result["action"] == "create"


@pytest.mark.parametrize(
    "address", ["Example-Host/t5", "EXAMPLE-HOST/t5", "example-host2/t5", "example-host/t5 "]
)
async def test_a_look_alike_machine_name_is_dropped(tmp_path: Path, address: str) -> None:
    world = World()
    remote_peer(world, address)
    _, result = await go(world, tmp_path, send_machines=["example-host"], dry_run=True)
    assert result["action"] == "create" and result["peer"] is None


@pytest.mark.parametrize(
    "names", [["example-host "], ["example/host"], ["example-host", "example-host"], "example-host"]
)
async def test_a_malformed_allow_list_is_refused(tmp_path: Path, names: Any) -> None:
    world = World()
    remote_peer(world)
    with pytest.raises(ValueError):
        await go(world, tmp_path, send_machines=names, dry_run=True)


async def test_a_local_peer_is_chosen_before_a_remote_one(tmp_path: Path) -> None:
    world = World()
    # "a/t1" sorts before every local "tNN" by text, so this checks the explicit rule.
    remote_peer(world, "a/t1")
    local = world.owner_tab("claude")
    _, result = await go(world, tmp_path, send_machines=["a"], dry_run=True)
    assert result["peer"] == f"hc:{local}"


def test_order_inside_a_harness_is_managed_local_then_other_local_then_remote() -> None:
    found = _idle_peers(
        policy(),
        [peer("a/t1"), peer("t9"), peer("t2"), peer("b/t0"), peer("t3")],
        set(),
        {"t3": _worker("t3")},
        send_machines=("a", "b"),
    )
    assert [row["target"] for row in found["claude"]] == ["t3", "t2", "t9", "a/t1", "b/t0"]


async def test_class_rank_still_wins_over_locality(tmp_path: Path) -> None:
    # The policy ranks the primary harness over the fallback; that rule is kept.
    world = World()
    remote_peer(world, tool="claude")
    world.owner_tab("codex")
    _, result = await go(world, tmp_path, send_machines=["example-host"], dry_run=True)
    assert (result["harness"], result["peer"]) == ("claude", f"hc:{REMOTE}")


# --- the default is byte-identical to the code before T-90 --------------------------


def _legacy_idle_peers(
    policy: Policy,
    peers: list[dict[str, Any]],
    busy: set[str],
    managed: dict[str, Worker],
) -> dict[str, list[dict[str, Any]]]:
    """`route._idle_peers` exactly as it was at the base commit, kept as the oracle."""
    by_kind = {h.agent_kind: h.name for h in policy.harnesses.values() if h.creatable}
    found: dict[str, list[dict[str, Any]]] = {}
    for peer in peers:
        harness = by_kind.get(peer.get("agent_kind"))
        target = peer.get("target")
        if (
            harness is None
            or not isinstance(target, str)
            or "/" in target
            or peer.get("send_ready") is not True
            or peer.get("agent_identity_source") != "foreground_argv"
            or peer.get("project_known") is not True
            or peer.get("project") != policy.project
            or target in busy
        ):
            continue
        found.setdefault(harness, []).append(peer)
    for group in found.values():
        group.sort(key=lambda peer: (peer["target"] not in managed, peer["target"]))
    return found


def peer(target: Any, **changes: Any) -> dict[str, Any]:
    row = {
        "peer_id": f"hc:{target}",
        "provider": "hc",
        "target": target,
        "agent_kind": "claude",
        "agent_identity_source": "foreground_argv",
        "project": PROJECT,
        "project_known": True,
        "state": "idle",
        "send_ready": True,
    }
    row.update(changes)
    return row


def _worker(address: str) -> Worker:
    return Worker(address, f"w-{address}", "design-impl", "claude", "idle", 1, {}, None)


def test_default_idle_peers_match_the_base_code_on_every_combination() -> None:
    targets = ["t1", "t2", "tA", "example-host/t5", "a/t1", "t 1", "", None, 7]
    variants = [
        {},
        {"send_ready": False},
        {"send_ready": "true"},
        {"agent_kind": "codex"},
        {"agent_kind": "crew"},
        {"agent_identity_source": "reported_status"},
        {"project_known": False},
        {"project": "/work/elsewhere"},
    ]
    rows = [peer(target, **variant) for target, variant in itertools.product(targets, variants)]
    managed = {"t2": _worker("t2"), "example-host/t5": _worker("example-host/t5")}
    for busy in (set(), {"t1", "example-host/t5"}):
        for size in (1, 2, 3):
            for chosen in itertools.combinations(range(0, len(rows), 5), size):
                sample = [dict(rows[i]) for i in chosen]
                assert _idle_peers(policy(), sample, busy, managed) == _legacy_idle_peers(
                    policy(), sample, busy, managed
                )
                assert _idle_peers(
                    policy(), sample, busy, managed, send_machines=()
                ) == _legacy_idle_peers(policy(), sample, busy, managed)


Scenario = Callable[[World], None]


def _local_idle(world: World) -> None:
    world.owner_tab("claude")


def _remote_only(world: World) -> None:
    remote_peer(world)


def _local_and_remote(world: World) -> None:
    remote_peer(world, "a/t1")
    world.owner_tab("claude")


def _fallback_local_primary_remote(world: World) -> None:
    remote_peer(world, tool="claude")
    world.owner_tab("codex")


def _busy_local(world: World) -> None:
    busy = world.owner_tab("claude")
    world.receipts["r0"] = {
        "id": "r0",
        "request_key": "by-hand",
        "target": f"hc:default:{busy}",
        "status": "pending",
        "updated": world.clock,
    }
    remote_peer(world)


def _nothing(world: World) -> None:
    return None


@pytest.mark.parametrize(
    "scenario",
    [
        _local_idle,
        _remote_only,
        _local_and_remote,
        _fallback_local_primary_remote,
        _busy_local,
        _nothing,
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
async def test_default_route_decisions_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, dry_run: bool
) -> None:
    outputs: list[str] = []
    for label, legacy in (("before", True), ("after", False), ("after-empty", False)):
        world = World()
        scenario(world)
        if legacy:
            monkeypatch.setattr(
                route_module,
                "_idle_peers",
                lambda policy, peers, busy, managed, **_: _legacy_idle_peers(
                    policy, peers, busy, managed
                ),
            )
        else:
            monkeypatch.undo()
        extra: dict[str, Any] = {"send_machines": []} if label == "after-empty" else {}
        _, result = await go(world, tmp_path / label, dry_run=dry_run, **extra)
        outputs.append(
            json.dumps({"result": result, "calls": world.calls, "sent": world.sent}, sort_keys=True)
        )
    assert outputs[0] == outputs[1] == outputs[2]


# --- the command line passes hc_send_machines through (fix round 1) -------------------


def _cli_args(tmp_path: Path, config: Any, *extra: str) -> Any:
    from routing_fakes import FIXTURE

    from answerback_bridge.cli import parser

    path = tmp_path / "config.json"
    config.save(path)
    brief = tmp_path / "brief.md"
    brief.write_text(DESIGN)
    argv = ["route", str(brief), "--task", "T-90", "--routing", str(FIXTURE), *extra]
    return parser().parse_args([*argv, "--config", str(path)])


@pytest.mark.parametrize(("names", "expected"), [(["example-host"], ["example-host"]), (None, [])])
async def test_cli_route_passes_the_config_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str] | None, expected: list[str]
) -> None:
    from answerback_bridge.config import Config
    from answerback_bridge.routing import cli as routing_cli

    seen: dict[str, Any] = {}

    async def fake_route(pool: Any, bridge: Any, brief: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "planned"}

    monkeypatch.setattr(routing_cli, "_pool", lambda args: make_pool(World(), tmp_path))
    monkeypatch.setattr(routing_cli, "route", fake_route)
    config = Config(provider_selection=[]) if names is None else Config(hc_send_machines=names)
    assert await routing_cli.dispatch(_cli_args(tmp_path, config)) == {"status": "planned"}
    assert seen["send_machines"] == expected


@pytest.mark.parametrize(
    "scenario",
    [
        _local_idle,
        _remote_only,
        _local_and_remote,
        _fallback_local_primary_remote,
        _busy_local,
        _nothing,
    ],
)
async def test_cli_route_with_default_config_decides_as_at_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> None:
    from answerback_bridge.config import Config
    from answerback_bridge.routing import cli as routing_cli

    outputs: list[str] = []
    for label in ("base", "cli"):
        world = World()
        scenario(world)
        folder = tmp_path / label
        folder.mkdir()
        pool = make_pool(world, folder)
        if label == "base":
            monkeypatch.setattr(
                route_module,
                "_idle_peers",
                lambda policy, peers, busy, managed, **_: _legacy_idle_peers(
                    policy, peers, busy, managed
                ),
            )
            result = await route(pool, pool.bridge, DESIGN, task="T-90", lane="a", day=DAY)
            monkeypatch.undo()
        else:
            monkeypatch.setattr(routing_cli, "_pool", lambda args, pool=pool: pool)
            monkeypatch.setattr(routing_cli.time, "strftime", lambda _: DAY)
            result = await routing_cli.dispatch(_cli_args(folder, Config(provider_selection=[])))
        outputs.append(
            json.dumps({"result": result, "calls": world.calls, "sent": world.sent}, sort_keys=True)
        )
    assert outputs[0] == outputs[1]


async def test_cli_route_reaches_a_remote_peer_on_a_listed_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.config import Config
    from answerback_bridge.routing import cli as routing_cli

    world = World()
    remote_peer(world)
    monkeypatch.setattr(routing_cli, "_pool", lambda args: make_pool(world, tmp_path))
    args = _cli_args(tmp_path, Config(hc_send_machines=["example-host"]), "--dry-run")
    result = await routing_cli.dispatch(args)
    assert (result["action"], result["peer"]) == ("reuse", f"hc:{REMOTE}")


# --- fix round 2: gated classes stay local; bridge refusals; stale bridge lists -------


def _gated() -> Policy:
    return policy(max_concurrent_gates=1, gate_classes=["design-impl"])


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_gated_class_does_not_choose_a_remote_peer(tmp_path: Path, dry_run: bool) -> None:
    world = World()
    remote_peer(world)
    pool, result = await go(
        world, tmp_path, policy_=_gated(), send_machines=["example-host"], dry_run=dry_run
    )
    assert result["action"] == "create"
    assert result["peer"] != f"hc:{REMOTE}"
    assert any(
        reason.startswith(f"hc:{REMOTE}: remote terminal not used for gated class design-impl")
        for reason in result["skipped"]
    )
    assert not any(sent["selector"] == f"hc:{REMOTE}" for sent in world.sent)
    assert all(row["address"] != REMOTE for row in pool.registry.gates().values())
    if dry_run:
        assert not pool.registry.gates()


async def test_a_gated_class_with_a_remote_peer_takes_no_gate_slot_for_it(
    tmp_path: Path,
) -> None:
    # The new worker cannot be shown either: nothing is sent and no slot is reserved.
    world = World()
    remote_peer(world)
    world.app_down = True
    world.selected_space = None
    pool, result = await go(world, tmp_path, policy_=_gated(), send_machines=["example-host"])
    assert (result["status"], result["stage"]) == ("failed", "placement")
    assert any("remote terminal not used for gated class" in r for r in result["skipped"])
    assert not world.sent and not pool.registry.gates()


async def test_an_ungated_class_still_chooses_the_remote_peer(tmp_path: Path) -> None:
    world = World()
    remote_peer(world)
    gated_elsewhere = policy(max_concurrent_gates=1, gate_classes=["port-fix"])
    pool, result = await go(
        world, tmp_path, policy_=gated_elsewhere, send_machines=["example-host"]
    )
    assert (result["action"], result["peer"]) == ("reuse", f"hc:{REMOTE}")
    assert "gate" not in result and not pool.registry.gates()


async def test_a_gated_class_still_reuses_a_local_peer(tmp_path: Path) -> None:
    world = World()
    remote_peer(world, "a/t1")
    local = world.owner_tab("claude")
    pool, result = await go(world, tmp_path, policy_=_gated(), send_machines=["a"])
    assert (result["action"], result["peer"]) == ("reuse", f"hc:{local}")
    assert [row["address"] for row in pool.registry.gates().values()] == [local]


class RefusingBridge(FakeBridge):
    """A bridge that refuses peer_submit the way the RPC client reports it."""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "peer_submit":
            self.world.calls.append((method, dict(params)))
            # Over the socket a not-ready refusal and a fault after the task was reserved
            # both arrive as this one generic error (error_info kind "internal").
            raise BridgeError("Bridge request failed; inspect service logs and the task receipt.")
        return await super().call(method, params)


async def test_a_bridge_refusal_of_a_remote_submit_is_not_turned_into_a_refusal(
    tmp_path: Path,
) -> None:
    # The error cannot show whether a task was reserved, so route() must not claim
    # "nothing was sent": the error is raised as before, and the caller inspects receipts.
    world = World()
    remote_peer(world)
    pool = make_pool(world, tmp_path)
    pool.bridge = RefusingBridge(world)
    with pytest.raises(BridgeError, match="inspect service logs and the task receipt"):
        await route(
            pool,
            pool.bridge,
            DESIGN,
            task="T-90",
            lane="a",
            day=DAY,
            send_machines=["example-host"],
        )
    assert world.names("peer_submit")


async def test_a_listed_machine_the_bridge_still_reports_read_only_is_explained(
    tmp_path: Path,
) -> None:
    # The config file lists the machine but the running bridge was started without it.
    world = World()
    remote_peer(world)
    stale = StaleBridge(world)
    pool = make_pool(world, tmp_path)
    pool.bridge = stale
    result = await route(
        pool,
        pool.bridge,
        DESIGN,
        task="T-90",
        lane="a",
        day=DAY,
        send_machines=["example-host"],
        dry_run=True,
    )
    assert result["action"] == "create"
    assert (
        f"hc:{REMOTE}: the bridge reports remote_read_only although example-host is in "
        "hc_send_machines; restart the bridge after changing hc_send_machines"
    ) in result["warnings"]


async def test_read_only_remote_peers_add_no_warning_by_default(tmp_path: Path) -> None:
    world = World()
    remote_peer(world)
    pool = make_pool(world, tmp_path)
    pool.bridge = StaleBridge(world)
    result = await route(pool, pool.bridge, DESIGN, task="T-90", lane="a", day=DAY, dry_run=True)
    assert result["warnings"] == []


class StaleBridge(FakeBridge):
    """peer_search as a bridge whose own hc_send_machines does not list the machine."""

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await super().call(method, params)
        if method == "peer_search":
            for row in result["peers"]:
                if "/" in row["target"]:
                    row.update(send_ready=False, send_readiness_reason="remote_read_only")
        return result
