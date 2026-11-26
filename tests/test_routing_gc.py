"""`answerback workers gc`: every never_kill rule, the holds, teardown, and one end-to-end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from routing_fakes import PROJECT, World, make_pool, policy

from answerback_bridge.routing.pool import Facts, Worker, plan_gc
from answerback_bridge.routing.route import route

DAY = "20261117"
NOW = 1_800_000_000.0
MINUTE = 60.0


def worker(address: str = "t1", *, state: str = "done", cls: str = "port-fix", harness="codex"):
    return Worker(address, f"{cls}-{harness}-261117-1", cls, harness, state, 1, {}, {"name": "x"})


def facts(
    receipt: str = "completed",
    *,
    handoff: str = "merged",
    age_min: float = 60,
    reusable: bool = False,
    **kw,
) -> Facts:
    return Facts(worker(**kw), receipt, NOW - age_min * MINUTE, handoff, reusable)


def decide(fact: Facts, queued: dict[str, int] | None = None, policy_=None):
    (verdict,), _ = plan_gc(policy_ or policy(), [fact], queued or {}, NOW)
    return verdict.action, verdict.reason


# --- never_kill -----------------------------------------------------------------


def test_never_kill_a_working_worker_even_with_a_merged_handoff() -> None:
    action, reason = decide(facts(state="working"))
    assert action == "keep" and reason == "never_kill: state=working"


@pytest.mark.parametrize("receipt", ["pending", "submitted"])
def test_never_kill_while_the_receipt_is_pending_or_submitted(receipt: str) -> None:
    action, reason = decide(facts(receipt, state="idle"))
    assert action == "keep" and reason == f"never_kill: receipt {receipt}"


@pytest.mark.parametrize("receipt", ["queued", "running", "blocked", "uncertain", "unknown"])
def test_any_other_unfinished_or_unreadable_receipt_also_keeps_it(receipt: str) -> None:
    assert decide(facts(receipt))[0] == "keep"


def test_never_kill_states_come_from_the_policy() -> None:
    import json

    from routing_fakes import FIXTURE

    from answerback_bridge.routing.policy import parse_policy

    raw = json.loads(FIXTURE.read_text())
    assert decide(facts(state="blocked"))[0] == "remove"
    raw["lifecycle"]["never_kill"].append("state=blocked")
    assert decide(facts(state="blocked"), policy_=parse_policy(raw))[0] == "keep"


# --- teardown_after and the holds -------------------------------------------------


@pytest.mark.parametrize(("handoff", "action"), [("open", "keep"), ("merged", "remove")])
def test_completed_receipt_waits_for_the_handoff(handoff: str, action: str) -> None:
    assert decide(facts(handoff=handoff))[0] == action


def test_rejected_handoff_also_releases_the_worker() -> None:
    action, reason = decide(facts(handoff="rejected"))
    assert action == "remove" and reason == "receipt completed, hand-off rejected"


def test_a_worker_between_create_and_dispatch_is_kept() -> None:
    action, reason = decide(facts("none", age_min=1, state="idle", reusable=True))
    assert action == "keep" and "not dispatched yet" in reason
    assert decide(facts("none", age_min=21, state="idle", reusable=True))[0] == "remove"


@pytest.mark.parametrize("receipt", ["failed", "cancelled", "abandoned"])
def test_a_dead_receipt_gets_keep_idle_min_then_goes(receipt: str) -> None:
    assert decide(facts(receipt, handoff="open", age_min=5))[0] == "keep"
    assert decide(facts(receipt, handoff="open", age_min=25))[0] == "remove"


def test_keep_idle_min_holds_a_reusable_worker_only_while_its_class_has_work() -> None:
    reusable = facts(age_min=5, state="idle", reusable=True, cls="design-impl", harness="claude")
    assert decide(reusable, {"design-impl": 1})[0] == "keep"
    assert decide(reusable, {"port-fix": 3})[0] == "remove"  # other class
    assert decide(reusable, {})[0] == "remove"
    old = facts(age_min=25, state="idle", reusable=True, cls="design-impl", harness="claude")
    assert decide(old, {"design-impl": 1})[0] == "remove"
    # T-13: a finished worker reports done, cannot take work, so holding it buys nothing.
    done = facts(age_min=5, state="done", reusable=False, cls="design-impl", harness="claude")
    assert decide(done, {"design-impl": 1})[0] == "remove"


def test_warm_pool_keeps_the_newest_idle_worker_of_its_class() -> None:
    older = facts(age_min=50, state="idle", reusable=True, address="t1")
    newer = facts(age_min=30, state="idle", reusable=True, address="t2")
    verdicts, deficit = plan_gc(policy(), [older, newer], {"port-fix": 2}, NOW)
    assert {v.worker.address: v.action for v in verdicts} == {"t1": "remove", "t2": "keep"}
    assert verdicts[1].reason.startswith("warm_pool")
    assert deficit == {}
    verdicts, _ = plan_gc(policy(), [older, newer], {}, NOW)  # nothing queued: no pool
    assert [v.action for v in verdicts] == ["remove", "remove"]


def test_warm_pool_deficit_is_reported_not_created() -> None:
    _, deficit = plan_gc(policy(), [], {"port-fix": 1}, NOW)
    assert deficit == {"port-fix": 1}


def test_holds_yield_to_capacity_but_never_kill_does_not() -> None:
    tight = policy(max_total=1)
    held = facts(age_min=5, state="idle", reusable=True, address="t1")
    busy = facts("submitted", state="working", address="t2")
    verdicts, _ = plan_gc(tight, [held, busy], {"port-fix": 1}, NOW)
    assert {v.worker.address: v.action for v in verdicts} == {"t1": "remove", "t2": "keep"}
    assert verdicts[0].reason.startswith("over capacity")
    two_busy = [busy, facts("pending", state="working", address="t3")]
    assert [v.action for v in plan_gc(tight, two_busy, {}, NOW)[0]] == ["keep", "keep"]


# --- which terminals are ours -------------------------------------------------------


async def test_owner_opened_terminals_are_never_candidates(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    # A tab the owner opened and happened to name like a worker: no registry row.
    lookalike = world.owner_tab("claude", name="design-impl-claude-261117-1", state="done")
    plain = world.owner_tab("codex", state="done")
    shell = world.spawn(["/bin/zsh", "-l"], state=None)
    world.clock += 3 * 3600
    result = await pool.gc(apply=True)
    assert result["workers"] == [] and result["remove"] == 0
    assert set(world.terminals) == {lookalike, plain, shell}
    assert not world.names("hc.kill") and not world.names("tab.close")


async def test_a_reused_address_is_not_mistaken_for_the_old_worker(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    address = created["address"]
    # The worker ends; the owner later gets a terminal at the same address.
    stale = world.terminals.pop(address)
    world.terminals[address] = {**stale, "created_ms": stale["created_ms"] + 5000, "name": None}
    world.clock += 3 * 3600
    result = await pool.gc(apply=True)
    assert result["workers"] == []
    assert address in world.terminals and not world.names("tab.close")
    assert address not in pool.registry.load()  # the stale row is pruned, the terminal is not


async def test_a_bare_hc_run_worker_is_managed_by_its_name_alone(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    bare = world.spawn(["codex", "--yolo"], name="port-fix-codex-261117-3", state="idle")
    elsewhere = world.spawn(
        ["codex"], name="port-fix-codex-261117-4", cwd="/work/other", state="idle"
    )
    world.clock += 3 * 3600
    result = await pool.gc(apply=True)
    assert [item["address"] for item in result["workers"]] == [bare]
    assert result["workers"][0]["outcome"] == "removed (hc kill, hc rm)"
    assert bare not in world.terminals and elsewhere in world.terminals


async def test_the_name_is_read_from_hc_get_because_hc_ls_rows_have_none(tmp_path: Path) -> None:
    # Review finding 4: a real `hc ls --json` row has no name, so a bare worker was never seen.
    world = World()
    pool = make_pool(world, tmp_path)
    bare = world.spawn(["codex"], name="port-fix-codex-261117-3", state="idle")
    owner = world.owner_tab("codex", name="port-fix-codex-261117-4")  # pattern name, but a tab
    world.spawn(["zsh"], name=None, state=None)
    assert all("name" not in row for row in await pool.terminals.live())
    found = await pool.workers()
    assert [(item.address, item.name) for item in found] == [(bare, "port-fix-codex-261117-3")]
    assert owner in world.terminals


async def test_a_bare_worker_with_a_hand_sent_receipt_is_protected(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    bare = world.spawn(["codex"], name="port-fix-codex-261117-3", state="idle")
    world.receipts["r9"] = {
        "id": "r9",
        "request_key": "by-hand",
        "target": f"hc:default:{bare}",
        "status": "pending",
        "updated": world.clock,
    }
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: receipt pending"
    assert bare in world.terminals


def _receipt(world: World, receipt_id: str, address: str, status: str) -> None:
    world.receipts[receipt_id] = {
        "id": receipt_id,
        "request_key": f"key-{receipt_id}",
        "target": f"hc:default:{address}",
        "status": status,
        "created": world.clock,
        "updated": world.clock,
    }


async def _finished_worker(world: World, pool) -> str:
    """A registered worker whose recorded receipt is completed and whose hand-off merged."""
    address = (await pool.create("port-fix", "codex", "261117"))["address"]
    _receipt(world, "r1", address, "completed")
    pool.registry.update(address, task="T-18", receipt_id="r1")
    world.handoffs["T-18"] = "merged"
    return address


@pytest.mark.parametrize("status", ["pending", "submitted", "running", "something-new"])
async def test_a_newer_unfinished_receipt_outranks_the_recorded_completed_one(
    tmp_path: Path, status: str
) -> None:
    # Review finding 1: the registry's receipt_id is a cache; the peer's receipts decide.
    world = World()
    pool = make_pool(world, tmp_path)
    address = await _finished_worker(world, pool)
    _receipt(world, "r2", address, status)  # sent by hand, or by a route the row never saw
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    shown = status if status != "something-new" else "unknown"
    assert item["action"] == "keep" and item["reason"] == f"never_kill: receipt {shown}"
    assert address in world.terminals and not world.names("tab.close")


async def test_an_older_unfinished_receipt_behind_a_finished_one_still_keeps(
    tmp_path: Path,
) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    address = (await pool.create("port-fix", "codex", "261117"))["address"]
    _receipt(world, "r0", address, "pending")
    _receipt(world, "r1", address, "completed")
    pool.registry.update(address, task="T-18", receipt_id="r1")
    world.handoffs["T-18"] = "merged"
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: receipt pending"


async def test_gc_keeps_a_worker_when_the_receipt_list_is_unreadable(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    address = await _finished_worker(world, pool)
    world.clock += 3 * 3600
    world.broken = {"task_list"}
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: receipt unknown"
    assert address in world.terminals


@pytest.mark.parametrize("status", ["submitted", "pending"])
async def test_teardown_rechecks_receipts_right_before_it_acts(tmp_path: Path, status: str) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    address = await _finished_worker(world, pool)
    world.clock += 3 * 3600
    (target,) = await pool.workers()
    _receipt(world, "r2", address, status)  # arrived after the plan; the agent is still idle
    assert await pool._teardown(target) == f"skipped: receipt became {status}"
    assert address in world.terminals and address in pool.registry.load()
    assert not world.names("tab.close") and not world.names("hc.kill")


async def test_teardown_is_skipped_when_receipts_cannot_be_read(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    address = await _finished_worker(world, pool)
    world.clock += 3 * 3600
    (target,) = await pool.workers()
    world.broken = {"task_list"}
    assert await pool._teardown(target) == "skipped: receipt became unknown"
    assert address in world.terminals


# --- teardown -------------------------------------------------------------------------


async def test_preview_touches_nothing(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    world.clock += 3 * 3600
    result = await pool.gc(apply=False)
    assert result["status"] == "planned" and result["remove"] == 1
    assert "outcome" not in result["workers"][0]
    assert created["address"] in world.terminals and created["address"] in pool.registry.load()


async def test_a_tab_shared_with_another_terminal_is_not_closed(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    address = created["address"]
    tab = world.terminals[address]["labels"]["herdr.tab"]
    split = world.spawn(["/bin/zsh"], tab=tab, state=None)  # the owner split a pane into it
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["outcome"] == "removed (hc kill, hc rm)"
    assert not world.names("tab.close")
    assert split in world.terminals and address not in world.terminals


async def test_the_last_tab_of_a_space_is_not_closed(tmp_path: Path) -> None:
    # Review finding 6: tab.close is sent confirmed, and the App closes a space with its
    # last tab. The worker's terminal is ended instead and the tab is left.
    world = World()
    world.owner_tabs = {}
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    address = created["address"]
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["outcome"] == "removed (hc kill, hc rm)"
    assert not world.names("tab.close") and not world.closed_spaces
    assert address not in world.terminals and address not in pool.registry.load()


async def test_a_tab_with_a_neighbour_in_its_space_is_closed(tmp_path: Path) -> None:
    world = World()
    world.owner_tabs = {}
    world.owner_tab()  # the owner's own tab in the project space
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["outcome"] == "removed (tab closed)"
    assert not world.closed_spaces and created["address"] not in world.terminals


async def test_a_tab_is_not_closed_when_the_layout_cannot_be_read(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    world.clock += 3 * 3600
    world.ls_fails = True
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["outcome"] == "removed (hc kill, hc rm)"
    assert not world.names("tab.close") and created["address"] not in world.terminals


async def test_teardown_rechecks_state_right_before_it_acts(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    address = created["address"]
    world.clock += 3 * 3600
    (target,) = await pool.workers()
    world.terminals[address]["agent_state"] = "working"  # picked up work after the plan
    assert await pool._teardown(target) == "skipped: state became working"
    assert address in world.terminals and address in pool.registry.load()


async def test_a_terminal_that_survives_teardown_is_reported(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    address = created["address"]

    async def stubborn(tab: str) -> None:  # a kept terminal that comes back as a shell
        world.calls.append(("tab.close", tab))

    pool.layout.tab_close = stubborn  # type: ignore[method-assign]
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["outcome"].startswith("unconfirmed")
    assert address in pool.registry.load()


async def test_listing_shows_workers_with_their_verdict(tmp_path: Path) -> None:
    world = World()
    world.todo = ["Refactor the adapter (bugfix)."]
    pool = make_pool(world, tmp_path)
    created = await pool.create("port-fix", "codex", "261117")
    listed = await pool.listing()
    assert listed["status"] == "listed" and listed["queued_by_class"] == {"port-fix": 1}
    (item,) = listed["workers"]
    assert item["name"] == created["name"] and item["tab"] and item["registered"] is True
    assert item["action"] == "keep"


# --- end to end ------------------------------------------------------------------------


async def test_route_then_completed_receipt_then_gc_removes_the_worker(tmp_path: Path) -> None:
    world = World()
    world.boot_seconds = 5
    pool = make_pool(world, tmp_path)
    brief = "Build a new module with a schema across files."

    routed = await route(pool, pool.bridge, brief, task="T-18", lane="a", day=DAY)
    assert routed["status"] == "submitted" and routed["action"] == "create"
    address = routed["worker"]["address"]
    assert world.terminals[address]["labels"]["herdr.space"] == pool.policy.project_space

    # While it works: untouchable, and the receipt is still open.
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: state=working"

    world.clock += 40 * MINUTE
    world.complete(routed["receipt_id"])
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "hand-off not merged or rejected yet"

    # T-13: the finished worker is done, so the next brief gets a new worker, not this one.
    again = await route(pool, pool.bridge, brief, task="T-21", lane="a", day=DAY)
    assert again["action"] == "create" and again["worker"]["address"] != address
    assert again["worker"]["name"] == "design-impl-claude-261117-2"

    world.handoffs["T-18"] = "merged"  # the conductor filed it under apply-queue/done
    result = await pool.gc(apply=True)
    outcomes = {item["address"]: item.get("outcome") for item in result["workers"]}
    assert outcomes[address] == "removed (tab closed)"
    assert address not in world.terminals and address not in pool.registry.load()
    # The second worker is still working on T-21 and was left alone.
    assert again["worker"]["address"] in world.terminals
    assert outcomes[again["worker"]["address"]] is None
    assert PROJECT == pool.policy.project


class _NewestHundredBridge:
    """The real Bridge.task_list shape: the newest 100 receipts of every peer, not one."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        result = await self.inner.call(method, params)
        if method == "task_list":
            rows = sorted(result["tasks"], key=lambda row: row["created"], reverse=True)
            return {"tasks": rows[:100], "searched_latest": 100}
        return result


def _crowd(world: World, count: int = 100) -> None:
    """Add `count` finished receipts of other peers, one second apart, with fresh ids."""
    for _ in range(count):
        world.clock += 1
        index = len(world.receipts)
        _receipt(world, f"r{index:03}", f"other{index}", "completed")


async def test_run6_an_unfinished_receipt_outside_the_newest_100_still_keeps(
    tmp_path: Path,
) -> None:
    """red_at_base (run-6 routing.pool.Pool._receipt:latest-100-receipt-window-reports-none)."""
    world = World()
    pool = make_pool(world, tmp_path)
    pool.bridge = _NewestHundredBridge(pool.bridge)
    bare = world.spawn(["codex"], name="port-fix-codex-261117-3", state="idle")
    world.clock += 1
    _receipt(world, "r-old", bare, "submitted")
    _crowd(world)
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: receipt unknown"
    assert bare in world.terminals and not world.names("hc.kill")
    (target,) = await pool.workers()
    assert await pool._teardown(target) == "skipped: receipt became unknown"


@pytest.mark.parametrize("older", [0, 50], ids=["window-newer-than-worker", "window-spans-worker"])
async def test_run6_a_miss_in_a_full_window_is_unknown_regardless_of_timestamps(
    tmp_path: Path, older: int
) -> None:
    """red_at_base (run-6 routing.pool.Pool._receipt:latest-100-receipt-window-reports-none).

    A full window proves nothing about a worker it does not name, whichever way its
    timestamps fall against the worker's: receipts and worker registration are stamped
    by different clocks, so a skewed or back-dated receipt may still sit outside the
    window. Only an uncapped target-scoped query could prove "none", and there is none,
    so the miss is unknown and gc keeps.
    """
    world = World()
    pool = make_pool(world, tmp_path)
    pool.bridge = _NewestHundredBridge(pool.bridge)
    _crowd(world, older)
    world.clock += 120
    bare = world.spawn(["codex"], name="port-fix-codex-261117-3", state="idle")
    world.clock += 120
    _crowd(world, 100 - older)
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "keep" and item["reason"] == "never_kill: receipt unknown"
    assert bare in world.terminals and not world.names("hc.kill")
    (target,) = await pool.workers()
    assert await pool._teardown(target) == "skipped: receipt became unknown"


async def test_run6_a_miss_in_a_short_list_is_still_none(tmp_path: Path) -> None:
    """pins_existing: a list shorter than the window is the whole store, so a miss is none."""
    world = World()
    pool = make_pool(world, tmp_path)
    pool.bridge = _NewestHundredBridge(pool.bridge)
    bare = world.spawn(["codex"], name="port-fix-codex-261117-3", state="idle")
    _crowd(world, 99)
    world.clock += 3 * 3600
    (item,) = (await pool.gc(apply=True))["workers"]
    assert item["action"] == "remove"
    assert item["reason"] == "never dispatched and idle past keep_idle_min"
    assert bare not in world.terminals
