"""T-227 gate: `workers create` waits out provider backoff; `route --dry-run` shows it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from routing_fakes import World, make_pool, policy

from answerback_bridge.cli import parser
from answerback_bridge.config import Config
from answerback_bridge.routing import cli as routing_cli
from answerback_bridge.routing.ports import RoutingError
from answerback_bridge.routing.route import route

DESIGN = "Build a new module with a schema across files."

DAY = "20261124"


def bedrock(until: float, level: int = 2) -> dict[str, Any]:
    return {
        "bedrock:default": {
            "provider": "bedrock",
            "account_hint": "default",
            "level": level,
            "until": until,
        },
        "kiro:default": {"provider": "kiro", "account_hint": "default", "level": 0, "until": None},
    }


def launches(world: World) -> int:
    return len(world.names("tab.new")) + len(world.names("hc.run"))


async def test_create_waits_until_the_pause_ends_then_launches(tmp_path: Path) -> None:
    world = World()
    until = world.clock + 100
    world.pressure = bedrock(until)
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", DAY[2:])
    assert world.clock >= until
    assert launches(world) == 1
    assert created["notes"][0] == "waited 105 s for bedrock provider backoff"
    asked = world.names("pressure_status")
    assert len(asked) == 8  # at 0 s, then every 15 s until the look at 105 s


async def test_the_rpc_answer_shape_is_read(tmp_path: Path) -> None:
    world = World()
    until = world.clock + 30
    world.pressure = {
        "pressure": bedrock(until),
        "settings": {"base_seconds": 60},
        "providers": ["kiro", "bedrock"],
        "signals": [],
        "error": None,
    }
    pool = make_pool(world, tmp_path)
    await pool.create("port-fix", "claude", DAY[2:])
    assert world.clock >= until
    assert launches(world) == 1


async def test_create_for_an_unheld_provider_does_not_wait(tmp_path: Path) -> None:
    world = World()
    start = world.clock
    world.pressure = bedrock(world.clock + 100)
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "kiro", DAY[2:])
    assert world.clock == start
    assert launches(world) == 1
    assert not any("backoff" in note for note in created["notes"])


async def test_a_pause_that_never_clears_gives_up_after_until_plus_60_s(tmp_path: Path) -> None:
    world = World()
    first_until = world.clock + 120
    # Every look reports a fresh pause: it never clears.
    world.pressure = lambda world: bedrock(world.clock + 120)
    pool = make_pool(world, tmp_path)
    with pytest.raises(RoutingError, match="provider backoff did not clear"):
        await pool.create("port-fix", "codex", DAY[2:])
    assert world.clock == first_until + 60
    assert launches(world) == 0


async def test_a_bridge_without_pressure_status_holds_nothing(tmp_path: Path) -> None:
    world = World()
    world.broken.add("pressure_status")
    pool = make_pool(world, tmp_path)
    await pool.create("port-fix", "codex", DAY[2:])
    assert launches(world) == 1


async def test_route_dry_run_prints_the_hold_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    start = world.clock
    world.pressure = bedrock(world.clock + 100)
    pool = make_pool(world, tmp_path)
    monkeypatch.setattr(routing_cli, "_pool", lambda args: pool)
    config = tmp_path / "config.json"
    Config().save(config)
    brief = tmp_path / "brief.md"
    brief.write_text("Fix the port.")
    args = parser().parse_args(
        ["--config", str(config), "route", str(brief), "--task", "T-227", "--dry-run"]
    )
    result = await routing_cli.dispatch(args)
    assert result["status"] == "planned"
    assert result["harness"] == "codex"
    assert result["hold"] == {
        "provider": "bedrock",
        "account_hint": "default",
        "level": 2,
        "until": start + 100,
        "retry_after_s": 100.0,
    }
    assert world.clock == start
    assert launches(world) == 0
    assert world.sent == []


async def test_route_dry_run_without_pressure_prints_no_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    monkeypatch.setattr(routing_cli, "_pool", lambda args: pool)
    config = tmp_path / "config.json"
    Config().save(config)
    brief = tmp_path / "brief.md"
    brief.write_text("Fix the port.")
    args = parser().parse_args(
        ["--config", str(config), "route", str(brief), "--task", "T-227", "--dry-run"]
    )
    result = await routing_cli.dispatch(args)
    assert result["status"] == "planned"
    assert result["hold"] is None


BACKOFF = {
    "status": "provider_backoff",
    "provider": "bedrock",
    "account_hint": "default",
    "level": 1,
    "until": 1_800_000_060.0,
    "retry_after_s": 60.0,
}


async def test_route_releases_its_gate_reservation_on_provider_backoff(tmp_path: Path) -> None:
    """A `provider_backoff` answer leaves no receipt: the slot is free for the next route
    at once, not after the reservation's 300 s grace period."""
    world = World()
    world.owner_tab("claude")
    gated = policy(max_concurrent_gates=1, gate_classes=["design-impl", "port-fix"])
    world.backoff = BACKOFF
    pool = make_pool(world, tmp_path, gated)
    first = await route(pool, pool.bridge, DESIGN, day="20261124", task="T-18", lane="a")
    assert first["status"] == "provider_backoff" and first["receipt"] == BACKOFF
    assert first["receipt_id"] is None and first["gate"] == {"held": None, "cap": 1}
    assert world.sent == [] and pool.registry.gates() == {}
    # The pause ends: another gated brief is admitted now, with the only slot.
    world.backoff = None
    second = await route(pool, pool.bridge, DESIGN, day="20261124", task="T-19", lane="a")
    assert second["status"] == "submitted" and second["gate"] == {"held": 1, "cap": 1}
    assert [row["task"] for row in pool.registry.gates().values()] == ["T-19"]
