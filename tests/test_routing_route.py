"""`answerback route`: pick, create, ready-wait and dispatch against scripted peers (T-18)."""

from __future__ import annotations

from pathlib import Path

import pytest
from routing_fakes import PROJECT, SPACE, World, make_pool, policy

from answerback_bridge.routing.pool import AtCapacity
from answerback_bridge.routing.ports import LayoutError, RoutingError
from answerback_bridge.routing.route import route

DAY = "20261117"
DESIGN = "Build a new module with a schema across files."
LAUNCH = [
    str(Path(__file__).resolve().parents[1] / "src/answerback_bridge/data/harness_launch.sh"),
    "claude",
    "--dangerously-skip-permissions",
]


async def go(world: World, tmp_path: Path, brief: str = DESIGN, **kwargs):
    pool = make_pool(world, tmp_path, kwargs.pop("policy_", None))
    kwargs.setdefault("task", "T-18")
    kwargs.setdefault("lane", "a")
    return pool, await route(pool, pool.bridge, brief, day=DAY, **kwargs)


async def test_reuses_an_idle_peer_of_the_primary_harness(tmp_path: Path) -> None:
    world = World()
    idle = world.owner_tab("claude")
    _, result = await go(world, tmp_path)
    assert result["status"] == "submitted"
    assert result["action"] == "reuse"
    assert result["peer"] == f"hc:{idle}"
    assert not world.names("tab.new") and not world.names("hc.run")
    (sent,) = world.sent
    assert sent["selector"] == f"hc:{idle}" and sent["provider"] == "hc"
    assert sent["request_id"] == f"ab-{DAY}-T-18-a-{idle}"
    first, blank, rest = sent["message"].split("\n", 2)
    assert first.startswith("Routed: design-impl -> claude/claude-model, primary for design-impl")
    assert blank == "" and rest == DESIGN


@pytest.mark.parametrize(
    "spoil",
    [
        lambda row: row.update(cwd="/work/elsewhere"),  # another project
        lambda row: row.update(agent_state="working"),
        lambda row: row.update(agent_state="done"),  # T-13: finished, not deliverable
        lambda row: row.update(agent_state=None),  # unreported
        lambda row: row.update(identity_source="reported_status"),  # wrapper foreground
        lambda row: row.update(argv=["codex"]),  # wrong harness
    ],
)
async def test_a_peer_that_is_not_idle_here_is_not_reused(tmp_path: Path, spoil) -> None:
    world = World()
    spoil(world.terminals[world.owner_tab("claude")])
    _, result = await go(world, tmp_path)
    assert result["action"] == "create"
    assert len(world.names("tab.new")) == 1


async def test_a_peer_with_an_active_receipt_is_not_reused(tmp_path: Path) -> None:
    world = World()
    busy = world.owner_tab("claude")
    world.receipts["r0"] = {
        "id": "r0",
        "request_key": "by-hand",
        "target": f"hc:default:{busy}",
        "status": "pending",
        "updated": world.clock,
    }
    _, result = await go(world, tmp_path)
    assert result["action"] == "create" and result["peer"] != f"hc:{busy}"


async def test_creates_a_bare_worker_as_a_tab_in_the_project_space(tmp_path: Path) -> None:
    world = World()
    world.boot_seconds = 7  # the launch script resolves the token before exec
    pool, result = await go(world, tmp_path)
    assert result["status"] == "submitted" and result["action"] == "create"
    (made,) = world.names("tab.new")
    # Facts 2026-11-17: launch bare (no first message) and place it in the project's space.
    assert made[1] == {
        "space": SPACE,
        "folder": PROJECT,
        "name": "design-impl-claude-261117-1",
        "argv": LAUNCH,
    }
    assert not world.names("hc.run")
    worker = result["worker"]
    assert worker["placement"] == "tab.new" and worker["visible"] is True and not worker["notes"]
    address = worker["address"]
    assert world.sent[0]["selector"] == f"hc:{address}"
    assert "no idle peer, created design-impl-claude-261117-1" in result["routed"]
    row = pool.registry.load()[address]
    assert row["receipt_id"] == result["receipt_id"] == "r1"
    assert (row["task"], row["lane"], row["class"], row["harness"]) == (
        "T-18",
        "a",
        "design-impl",
        "claude",
    )
    # The scanner was polled until the harness, not the script, was in the foreground.
    assert len(world.names("peer_search")) >= 3


async def test_falls_back_to_hc_run_and_lands_in_the_project_space(tmp_path: Path) -> None:
    world = World()
    world.app_down = True  # tab.new fails; terminal.keep lands in the selected space
    _, result = await go(world, tmp_path)
    worker = result["worker"]
    assert worker["placement"] == "hc run + terminal.keep" and worker["visible"] is True
    assert result["status"] == "submitted" and len(world.sent) == 1


async def test_a_worker_outside_the_project_space_is_not_submitted_to(tmp_path: Path) -> None:
    # Review finding 5: the placement defect was an invisible worker running for tens of minutes.
    world = World()
    world.app_down = True
    world.selected_space = "spcother"  # terminal.keep lands in the window's selected space
    pool, result = await go(world, tmp_path)
    (ran,) = world.names("hc.run")
    assert ran[1:] == ("design-impl-claude-261117-1", PROJECT, LAUNCH)
    assert world.names("terminal.keep")
    worker = result["worker"]
    assert worker["placement"] == "hc run + terminal.keep"
    assert worker["visible"] is False
    assert any("spcother" in note for note in worker["notes"])
    assert (result["status"], result["stage"]) == ("failed", "placement")
    assert result["success"] is False
    assert not world.sent and not world.names("peer_submit")
    # The row stays so gc can see the worker, and it holds no gate.
    assert worker["address"] in pool.registry.load()
    assert [item.address for item in await pool.workers()] == [worker["address"]]
    assert not pool.registry.gates()


async def test_a_worker_the_app_could_not_show_at_all_is_not_submitted_to(tmp_path: Path) -> None:
    world = World()
    world.app_down = True
    world.selected_space = None  # terminal.keep fails as well
    pool, result = await go(world, tmp_path)
    assert (result["status"], result["stage"]) == ("failed", "placement")
    assert not world.sent and result["worker"]["address"] in pool.registry.load()


async def test_a_new_tab_has_its_right_clicks_sent_to_the_menu(tmp_path: Path) -> None:
    # Review finding 3.
    world = World()
    _, result = await go(world, tmp_path)
    worker = result["worker"]
    assert world.names("terminal.right_click") == [
        ("terminal.right_click", worker["address"], "menu")
    ]
    order = [call[0] for call in world.calls if call[0] in ("tab.new", "terminal.right_click")]
    assert order == ["tab.new", "terminal.right_click"]
    assert worker["pointer"] == "menu" and not worker["notes"]


async def test_a_kept_terminal_has_its_right_clicks_sent_to_the_menu(tmp_path: Path) -> None:
    world = World()
    world.app_down = True  # tab.new fails; hc run then terminal.keep
    pool = make_pool(world, tmp_path)
    worker = await pool.create("design-impl", "claude", DAY[2:])
    order = [call[0] for call in world.calls if call[0].startswith("terminal.")]
    assert order == ["terminal.keep", "terminal.right_click"]
    assert world.names("terminal.right_click")[0][1] == worker["address"]
    assert worker["pointer"] == "menu"


@pytest.mark.parametrize(
    "error", [LayoutError("no such terminal"), RoutingError("timed out; its effect is unknown")]
)
async def test_a_failed_right_click_is_reported_and_starts_nothing_else(
    tmp_path: Path, error: Exception
) -> None:
    world = World()
    world.right_click_fails = error
    pool = make_pool(world, tmp_path)
    worker = await pool.create("design-impl", "claude", DAY[2:])
    assert worker["pointer"] == "unset"
    assert any("terminal.right_click failed" in note for note in worker["notes"])
    # The tab exists and is recorded; no second worker through hc run.
    assert len(world.names("tab.new")) == 1 and not world.names("hc.run")
    assert worker["address"] in pool.registry.load()


async def test_a_terminal_the_app_does_not_show_is_not_right_clicked(tmp_path: Path) -> None:
    world = World()
    world.app_down = True
    world.selected_space = None  # terminal.keep fails too
    pool = make_pool(world, tmp_path)
    worker = await pool.create("design-impl", "claude", DAY[2:])
    assert not world.names("terminal.right_click") and worker["pointer"] == "unset"


async def test_an_unanswered_tab_new_does_not_start_a_second_worker(tmp_path: Path) -> None:
    world = World()
    world.app_hangs = True
    pool = make_pool(world, tmp_path)
    with pytest.raises(Exception, match="effect is unknown"):
        await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY)
    assert not world.names("hc.run") and not world.sent


async def test_names_count_up_past_live_workers(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    first = await pool.create("design-impl", "claude", "261117")
    second = await pool.create("design-impl", "claude", "261117")
    assert [first["name"], second["name"]] == [
        "design-impl-claude-261117-1",
        "design-impl-claude-261117-2",
    ]


async def test_refuses_when_at_capacity(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    for harness in ("claude", "claude", "codex", "codex"):
        created = await pool.create("design-impl", harness, "261117")
        world.terminals[created["address"]]["agent_state"] = "working"
    result = await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY)
    assert result["status"] == "refused" and result["success"] is False
    assert result["reason"].startswith("at_capacity")
    assert any("claude" in line and "max_workers 2" in line for line in result["skipped"])
    assert any("codex" in line and "max_workers 2" in line for line in result["skipped"])
    assert not world.sent and len(world.names("tab.new")) == 4
    with pytest.raises(AtCapacity, match="max_workers 2"):
        await pool.create("design-impl", "claude", "261117")


async def test_primary_at_capacity_goes_to_the_fallback(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    for _ in range(2):
        created = await pool.create("design-impl", "claude", "261117")
        world.terminals[created["address"]]["agent_state"] = "working"
    result = await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY)
    assert result["harness"] == "codex" and result["action"] == "create"
    assert "fallback for design-impl" in result["routed"]
    assert "passed over claude: no idle peer and at max_workers 2" in result["routed"]


async def test_max_total_caps_the_project(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path, policy(max_total=1))
    created = await pool.create("port-fix", "codex", "261117")
    world.terminals[created["address"]]["agent_state"] = "working"
    result = await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY)
    assert result["status"] == "refused"
    assert any("max_total 1" in line for line in result["skipped"])


async def test_owner_terminals_do_not_count_against_capacity(tmp_path: Path) -> None:
    world = World()
    for _ in range(8):
        world.owner_tab("claude", state="working")
    _, result = await go(world, tmp_path)
    assert result["action"] == "create" and result["status"] == "submitted"


async def test_ready_wait_times_out_without_sending(tmp_path: Path) -> None:
    world = World()
    world.never_ready = True  # for example a browser sign-in: the harness never reports idle
    pool, result = await go(world, tmp_path)
    assert result["status"] == "failed" and result["stage"] == "ready_wait"
    assert result["ready"] == "not_ready"
    assert "do not resend" in result["reason"] and result["screen"] == "Sign in"
    assert not world.sent
    assert len(world.names("hc_read")) == 1
    # The worker is left for inspection and stays registered, so gc can find it later.
    assert result["worker"]["address"] in pool.registry.load()
    assert not world.names("hc.kill")
    assert world.clock - 1_800_000_000.0 >= 120


async def test_wrong_foreground_program_is_never_dispatched_to(tmp_path: Path) -> None:
    """Fact 2026-11-17: identity is the native binary in the foreground argv."""
    world = World()
    world.foreground_override = ["codex"]  # asked for claude, something else is running
    _, result = await go(world, tmp_path)
    assert result["status"] == "failed" and result["ready"] == "identity_mismatch"
    assert "expected claude" in result["reason"]
    assert not world.sent


async def test_review_goes_to_a_harness_that_did_not_write_it(tmp_path: Path) -> None:
    world = World()
    world.owner_tab("claude")  # the author's own idle tab must not be chosen
    grok = world.owner_tab("grok")
    brief = "Second opinion: review branch task-T16."
    _, no_author = await go(world, tmp_path, brief)
    assert no_author["status"] == "refused" and "--author" in no_author["reason"]
    _, result = await go(world, tmp_path, brief, author="claude")
    assert result["harness"] == "grok" and result["peer"] == f"hc:{grok}"
    assert "not the author (claude)" in result["routed"]


async def test_review_without_an_idle_peer_takes_declared_order_or_seat_cost(
    tmp_path: Path,
) -> None:
    import json

    from routing_fakes import FIXTURE

    from answerback_bridge.routing.policy import parse_policy

    world = World()
    _, plain = await go(world, tmp_path, "security read", author="claude", dry_run=True)
    assert plain["harness"] == "codex"  # first non-author in the policy's order
    raw = json.loads(FIXTURE.read_text())
    raw["harnesses"]["grok"]["seat_cost"] = 1
    raw["harnesses"]["codex"]["seat_cost"] = 3
    _, priced = await go(
        world, tmp_path, "security read", author="claude", dry_run=True, policy_=parse_policy(raw)
    )
    assert priced["harness"] == "grok"


@pytest.mark.parametrize(
    ("brief", "owner"),
    [("Merge and tag the release.", "conductor"), ("Attended: push the tag.", "owner")],
)
async def test_owner_and_conductor_classes_are_not_dispatched(
    tmp_path: Path, brief: str, owner: str
) -> None:
    world = World()
    _, result = await go(world, tmp_path, brief)
    assert result["status"] == "refused" and owner in result["reason"]
    assert not world.sent and not world.names("tab.new")


async def test_non_core_fallback_is_skipped_with_a_reason(tmp_path: Path) -> None:
    world = World()
    _, result = await go(world, tmp_path, "Update the changelog.", dry_run=True)
    assert result["harness"] == "kiro"
    assert any(line.startswith("crew: provider crew") for line in result["skipped"])


async def test_unclassified_or_ambiguous_brief_needs_a_class(tmp_path: Path) -> None:
    world = World()
    _, nothing = await go(world, tmp_path, "Please help.")
    assert nothing["status"] == "needs_selection" and nothing["hint"] == "pass --class"
    _, tie = await go(world, tmp_path, "One schema, one refactor.")
    assert tie["status"] == "needs_selection"
    assert set(tie["candidates"]) == {"design-impl", "port-fix"}
    _, forced = await go(world, tmp_path, "Please help.", class_override="port-fix")
    assert forced["harness"] == "codex"
    assert not world.names("peer_submit")[1:]


async def test_same_task_and_lane_is_not_routed_twice(tmp_path: Path) -> None:
    world = World()
    pool, first = await go(world, tmp_path)
    again = await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY)
    assert again["status"] == "refused" and again["receipt_id"] == first["receipt_id"]
    assert len(world.sent) == 1
    other = await route(pool, pool.bridge, DESIGN, task="T-18", lane="b", day=DAY)
    assert other["status"] == "submitted"
    assert any("another lane" in warning for warning in other["warnings"])


async def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    world = World()
    pool, result = await go(world, tmp_path, dry_run=True)
    assert result["status"] == "planned" and result["action"] == "create"
    assert not world.terminals and not world.sent and pool.registry.load() == {}


async def test_force_new_ignores_an_idle_peer(tmp_path: Path) -> None:
    world = World()
    idle = world.owner_tab("claude")
    _, result = await go(world, tmp_path, force_new=True)
    assert result["action"] == "create" and result["peer"] != f"hc:{idle}"


async def test_incomplete_scan_is_reported(tmp_path: Path) -> None:
    world = World()
    world.scan_complete = False
    _, result = await go(world, tmp_path, dry_run=True)
    assert any("incomplete" in warning for warning in result["warnings"])


async def test_empty_brief_and_unknown_author_are_errors(tmp_path: Path) -> None:
    world = World()
    pool = make_pool(world, tmp_path)
    with pytest.raises(ValueError, match="empty"):
        await route(pool, pool.bridge, "  \n", task="T-18", lane="a", day=DAY)
    with pytest.raises(ValueError, match="--author"):
        await route(pool, pool.bridge, DESIGN, task="T-18", lane="a", day=DAY, author="nobody")


# --- review finding 2: at most max_concurrent_gates full gates at once --------------------

PORT = "Fix the port: apply the patch and rebase."


def _gated(**extra):
    return policy(max_concurrent_gates=1, gate_classes=["design-impl", "port-fix"], **extra)


async def test_a_second_gate_brief_is_refused_while_the_first_is_unfinished(
    tmp_path: Path,
) -> None:
    world = World()
    pool, first = await go(world, tmp_path, policy_=_gated())
    assert first["status"] == "submitted" and first["gate"] == {"held": 1, "cap": 1}
    (held,) = pool.registry.gates().values()
    assert held["address"] == first["worker"]["address"] and held["task"] == "T-18"
    assert held["class"] == "design-impl" and held["receipt_id"] == first["receipt_id"]
    made, sent = len(world.names("tab.new")), len(world.sent)
    _, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "refused" and second["success"] is False
    assert second["reason"].startswith("gate_busy: 1 of 1")
    assert second["holders"][0]["request_id"] == first["request_id"]
    assert len(world.names("tab.new")) == made and len(world.sent) == sent  # nothing started


async def test_a_dry_run_reports_the_busy_gate(tmp_path: Path) -> None:
    world = World()
    await go(world, tmp_path, policy_=_gated())
    _, plan = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19", dry_run=True)
    assert plan["status"] == "refused" and plan["reason"].startswith("gate_busy")


async def test_the_gate_frees_when_the_holder_receipt_finishes(tmp_path: Path) -> None:
    world = World()
    _, first = await go(world, tmp_path, policy_=_gated())
    world.complete(first["receipt_id"])
    pool, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "submitted"
    assert [row["task"] for row in pool.registry.gates().values()] == ["T-19"]  # pruned


async def test_a_brief_whose_class_runs_no_gate_is_not_counted(tmp_path: Path) -> None:
    world = World()
    await go(world, tmp_path, policy_=_gated())
    pool, second = await go(
        world, tmp_path, "Analyse and compare the options.", policy_=_gated(), task="T-19"
    )
    assert second["status"] == "submitted" and "gate" not in second
    assert len(pool.registry.gates()) == 1


async def test_a_reused_owner_peer_holds_the_gate_too(tmp_path: Path) -> None:
    world = World()
    idle = world.owner_tab("claude")
    pool, first = await go(world, tmp_path, policy_=_gated())
    assert first["action"] == "reuse"
    (held,) = pool.registry.gates().values()
    assert held["address"] == idle
    _, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "refused"


async def test_an_unreadable_gate_receipt_still_holds_the_gate(tmp_path: Path) -> None:
    world = World()
    _, first = await go(world, tmp_path, policy_=_gated())
    del world.receipts[first["receipt_id"]]  # aged out of the list and unreadable by id
    _, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "refused" and second["holders"][0]["status"] == "unknown"


async def test_a_submit_that_raises_leaves_the_gate_held_until_it_is_known(
    tmp_path: Path,
) -> None:
    world = World()
    world.broken = {"peer_submit"}
    with pytest.raises(Exception, match="peer_submit failed"):
        await go(world, tmp_path, policy_=_gated())
    world.broken = set()
    _, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "refused" and second["holders"][0]["status"] == "reserved"
    world.clock += 301  # no receipt ever appeared under that request id: the hold lapses
    _, third = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert third["status"] == "submitted"


async def test_a_worker_that_never_becomes_ready_does_not_hold_the_gate(tmp_path: Path) -> None:
    world = World()
    world.boot_seconds = 10_000
    pool, first = await go(world, tmp_path, policy_=_gated())
    assert first["status"] == "failed" and pool.registry.gates() == {}


async def test_workers_create_is_refused_for_a_gate_class_while_the_gate_is_held(
    tmp_path: Path,
) -> None:
    world = World()
    pool, _ = await go(world, tmp_path, policy_=_gated())
    with pytest.raises(AtCapacity, match="gate_busy: 1 of 1"):
        await pool.create("port-fix", "codex", "261117")
    assert (await pool.create("analysis", "grok", "261117"))["class"] == "analysis"


async def test_two_caps_allow_two_gates(tmp_path: Path) -> None:
    world = World()
    two = policy(max_concurrent_gates=2, gate_classes=["design-impl", "port-fix"])
    await go(world, tmp_path, policy_=two)
    _, second = await go(world, tmp_path, PORT, policy_=two, task="T-19")
    assert second["status"] == "submitted" and second["gate"] == {"held": 2, "cap": 2}
    _, third = await go(world, tmp_path, PORT, policy_=two, task="T-20")
    assert third["status"] == "refused"


async def test_a_holder_whose_terminal_is_gone_frees_the_gate(tmp_path: Path) -> None:
    world = World()
    pool, first = await go(world, tmp_path, policy_=_gated())
    await pool.terminals.kill(first["worker"]["address"])  # receipt stays `submitted`
    _, second = await go(world, tmp_path, PORT, policy_=_gated(), task="T-19")
    assert second["status"] == "submitted"


async def test_the_listing_shows_the_gate_and_the_text_nothing_enforces(tmp_path: Path) -> None:
    world = World()
    with_text = _gated(hung_action="kill and re-route once")
    pool, first = await go(world, tmp_path, policy_=with_text)
    listed = await pool.listing()
    assert listed["gates"]["cap"] == 1
    assert listed["gates"]["classes"] == ["design-impl", "port-fix"]
    assert [row["request_id"] for row in listed["gates"]["holders"]] == [first["request_id"]]
    assert listed["unenforced_lifecycle"] == ["hung_action"]
