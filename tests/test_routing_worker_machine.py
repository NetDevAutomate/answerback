"""A worker row records the machine its worker runs on; an older row reads as this machine."""

from __future__ import annotations

import json
from pathlib import Path

from routing_fakes import World, make_pool

from answerback_bridge.placement.probe import LOCAL
from answerback_bridge.routing.pool import Worker, row_machine

DAY = "261122"
# What a row held before the machine was recorded, plus `tab`, which the App may add.
OLD_KEYS = {"name", "class", "harness", "project", "created_ms", "created_at", "placement", "tab"}


async def test_a_new_row_carries_the_machine(tmp_path: Path) -> None:
    pool = make_pool(World(), tmp_path)
    created = await pool.create("port-fix", "codex", DAY)
    row = pool.registry.load()[created["address"]]
    assert row["machine"] == LOCAL == "local"
    assert row_machine(row) == LOCAL
    (worker,) = await pool.workers()
    assert worker.machine == LOCAL


async def test_an_old_row_without_the_field_reads_as_this_machine(tmp_path: Path) -> None:
    pool = make_pool(World(), tmp_path)
    created = await pool.create("port-fix", "codex", DAY)
    path = pool.registry.path
    data = json.loads(path.read_text())
    del data["workers"][created["address"]]["machine"]
    path.write_text(json.dumps(data))
    row = pool.registry.load()[created["address"]]
    assert "machine" not in row and row_machine(row) == LOCAL
    (worker,) = await pool.workers()
    assert worker.machine == LOCAL
    # Reading an old row does not rewrite it.
    assert json.loads(path.read_text()) == data
    # A value that is no machine name reads as this machine too; a recorded name is kept.
    for value in (None, "", 7, ["mac-a"]):
        assert row_machine({"machine": value}) == LOCAL
    assert row_machine({"machine": "mac-a"}) == "mac-a"
    assert Worker("t01", "w", "port-fix", "codex", "idle", 1, {}, None).machine == LOCAL


async def test_nothing_else_in_a_row_changed(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", DAY)
    row = pool.registry.load()[created["address"]]
    assert set(row) - {"machine"} <= OLD_KEYS
    assert {"name", "class", "harness", "project", "created_ms", "created_at", "placement"} <= set(
        row
    )
    assert row["placement"] == created["placement"] != row["machine"]
    assert (row["name"], row["class"], row["harness"]) == (
        created["name"],
        "port-fix",
        "codex",
    )
    assert row["created_ms"] == created["created_ms"] and row["created_at"] == world.clock
    # What `workers create` and `workers list` answer is as it was: no machine in either.
    assert "machine" not in created
    (worker,) = await pool.workers()
    assert "machine" not in worker.public()
    assert set(json.loads(pool.registry.path.read_text())) == {"version", "workers", "gates"}
