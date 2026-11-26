"""T-81: readiness and submission stay bound to the created terminal instance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from routing_fakes import World, make_pool

from answerback_bridge.cli import parser
from answerback_bridge.routing import cli as routing_cli
from answerback_bridge.routing.route import route

DAY = "20261120"
BRIEF = "Fix the port."


@pytest.mark.parametrize("fallback", [False, True])
async def test_create_returns_the_recorded_timestamp_after_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    """red_at_base: creation returns its original identity even after a placement reread."""
    world = World()
    world.app_down = fallback
    pool = make_pool(world, tmp_path)
    keep = pool.layout.keep

    async def keep_then_replace(address: str) -> None:
        await keep(address)
        world.terminals[address]["created_ms"] += 5000

    monkeypatch.setattr(pool.layout, "keep", keep_then_replace)
    created = await pool.create("port-fix", "codex", DAY[2:])
    recorded = pool.registry.load()[created["address"]]["created_ms"]
    assert created.get("created_ms") == recorded
    if fallback:
        assert recorded != world.terminals[created["address"]]["created_ms"]


@pytest.mark.parametrize("command", ["route", "workers"])
@pytest.mark.parametrize("readback", ["replacement", "gone", "unknown"])
async def test_ready_wait_refuses_a_replaced_or_unverifiable_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, readback: str
) -> None:
    """red_at_base: both callers must reread identity after a peer reports ready."""
    world = World()
    pool = make_pool(world, tmp_path)
    scan = world.peers

    def scan_then_change_instance() -> list[dict[str, Any]]:
        peers = scan()
        for peer in peers:
            address = peer["target"]
            if readback == "gone":
                world.terminals.pop(address)
            elif readback == "unknown":
                world.terminals[address]["created_ms"] = None
            else:
                world.terminals[address]["created_ms"] += 5000
        return peers

    monkeypatch.setattr(world, "peers", scan_then_change_instance)
    # Also keep the submit inert: at BASE the absent instance must produce a
    # readiness assertion failure, not an unrelated fake-terminal KeyError.
    call = pool.bridge.call

    async def record_submit(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "peer_submit":
            world.sent.append(dict(params))
            return {"id": "r1", "status": "submitted"}
        return await call(method, params)

    monkeypatch.setattr(pool.bridge, "call", record_submit)
    if command == "route":
        result = await route(
            pool, pool.bridge, BRIEF, task="T-81", lane="a", day=DAY, class_override="port-fix"
        )
        assert result.get("stage") == "ready_wait"
    else:
        monkeypatch.setattr(routing_cli, "_pool", lambda args: pool)
        args = parser().parse_args(["workers", "create", "--class", "port-fix"])
        result = await routing_cli.dispatch(args)
    assert result["status"] == "failed"
    assert result["ready"] == "identity_mismatch"
    assert result["reason"] == (
        "terminal instance changed or could not be verified; nothing was sent"
    )
    assert not world.sent
    assert not world.names("hc.kill") and not world.names("tab.close")


@pytest.mark.parametrize("reuse", [False, True])
async def test_route_forwards_the_discovered_binding(tmp_path: Path, reuse: bool) -> None:
    """red_at_base: every route submission carries the selected peer's binding."""
    world = World()
    if reuse:
        world.owner_tab("codex")
    pool = make_pool(world, tmp_path)
    result = await route(
        pool, pool.bridge, BRIEF, task="T-81", lane="a", day=DAY, class_override="port-fix"
    )
    assert result["status"] == "submitted"
    assert result["action"] == ("reuse" if reuse else "create")
    (peer,) = world.peers()
    assert world.sent[0].get("binding") == peer["binding"]


@pytest.mark.parametrize("reuse", [False, True])
async def test_route_refuses_an_instance_replaced_at_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """red_at_base: the binding still protects a replacement after the readiness read."""
    world = World()
    if reuse:
        world.owner_tab("codex")
    pool = make_pool(world, tmp_path)
    call = pool.bridge.call

    async def replace_before_submit(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "peer_submit":
            address = params["selector"].removeprefix("hc:")
            world.terminals[address]["created_ms"] += 5000
        return await call(method, params)

    monkeypatch.setattr(pool.bridge, "call", replace_before_submit)
    with pytest.raises(ValueError, match="^Selected peer changed since discovery; search again$"):
        await route(
            pool, pool.bridge, BRIEF, task="T-81", lane="a", day=DAY, class_override="port-fix"
        )
    assert not world.sent and not world.receipts


async def test_workers_create_still_waits_for_the_original_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: an unchanged instance can finish booting and become ready."""
    world = World()
    world.boot_seconds = 7
    pool = make_pool(world, tmp_path)
    monkeypatch.setattr(routing_cli, "_pool", lambda args: pool)
    args = parser().parse_args(["workers", "create", "--class", "port-fix"])
    result = await routing_cli.dispatch(args)
    assert result["status"] == result["ready"] == "ready"
    assert world.clock >= 1_800_000_007
    assert "peer" not in result and not world.sent
