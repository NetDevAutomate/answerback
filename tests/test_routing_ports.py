"""The real routing ports: exact argv to hc and herdr-app, status files, registry, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.cli import parser
from answerback_bridge.process import CommandError, CommandTimeout, Output
from answerback_bridge.routing import ports
from answerback_bridge.routing.pool import Registry
from answerback_bridge.routing.ports import (
    HcTerminals,
    HerdrApp,
    LayoutError,
    QueueHandoffs,
    RoutingError,
    TasksLedger,
)


class Script:
    """Stands in for process.run: records argv, returns the next scripted reply."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *replies: Any) -> None:
        self.replies = list(replies)
        self.argv: list[list[str]] = []
        self.env: list[dict[str, str] | None] = []
        monkeypatch.setattr(ports, "run", self)

    async def __call__(self, argv: list[str], *, max_seconds: float, env=None) -> Output:
        self.argv.append(argv)
        self.env.append(env)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Output(reply if isinstance(reply, str) else json.dumps(reply), "")


async def test_hc_run_is_background_parentless_named_and_bare(monkeypatch) -> None:
    monkeypatch.setenv("HC_TERMINAL_ID", "t5r")
    script = Script(monkeypatch, {"address": "t60"})
    launch = ["/repo/tools/harness_launch.sh", "codex", "--yolo"]
    address = await HcTerminals("hc", "main").run(
        name="port-fix-codex-261117-1", cwd="/repo", argv=launch
    )
    assert address == "t60"
    assert script.argv == [
        [
            "hc",
            "run",
            "--bg",
            "--no-parent",
            "--json",
            "--name",
            "port-fix-codex-261117-1",
            "--cwd",
            "/repo",
            *launch,  # the launch argv ends the command: no first message follows it
        ]
    ]
    env = script.env[0]
    assert env is not None and "HC_TERMINAL_ID" not in env and env["HC_SESSION"] == "main"


async def test_hc_list_get_kill_rm(monkeypatch) -> None:
    script = Script(
        monkeypatch,
        {"terminals": [{"address": "t1"}, {"address": "vps/t2"}, "junk"]},
        {"address": "t1", "state": "live"},
        CommandError(1, "no such terminal"),
        {"address": "t9"},
        {},
        {"error": "not finished"},
    )
    core = HcTerminals()
    assert await core.live() == [{"address": "t1"}]  # remote terminals are out of scope
    assert (await core.get("t1")) == {"address": "t1", "state": "live"}
    assert await core.get("t1") is None  # gone
    assert await core.get("t1") is None  # a reply about another terminal is not this one
    await core.kill("t1")
    with pytest.raises(RoutingError, match="refused"):
        await core.remove("t1")
    assert script.argv[-2:] == [["hc", "kill", "--json", "t1"], ["hc", "rm", "--json", "t1"]]
    with pytest.raises(RoutingError, match="local core terminals"):
        await core.kill("vps/t2")


async def test_hc_failures_are_named(monkeypatch) -> None:
    Script(monkeypatch, CommandError(1, "x"), CommandTimeout("slow"), "not json")
    core = HcTerminals()
    with pytest.raises(RoutingError, match="no worker was confirmed"):
        await core.run(name="n", cwd="/repo", argv=["codex"])
    with pytest.raises(RoutingError, match="effect is unknown"):
        await core.live()
    with pytest.raises(RoutingError, match="not JSON"):
        await core.live()


async def test_tab_new_targets_the_space_without_a_task_or_focus(monkeypatch) -> None:
    script = Script(monkeypatch, {"pane": "p1", "terminal": "t61"})
    launch = ["/repo/tools/harness_launch.sh", "claude", "--dangerously-skip-permissions"]
    made = await HerdrApp().tab_new(space="spc1", folder="/repo", name="w-1", argv=launch)
    assert made["terminal"] == "t61"
    (argv,) = script.argv
    assert argv[:3] == ["herdr-app", "do", "tab.new"]
    assert json.loads(argv[3]) == {
        "space": "spc1",
        "folder": "/repo",
        "name": "w-1",
        "argv": launch,
        "focus": False,
    }


async def test_right_click_sends_the_pane_to_the_menu(monkeypatch) -> None:
    # Review finding 3: the 2026-11-17 interruption was a pointer report in a worker prompt.
    script = Script(monkeypatch, {}, {"error": "no such terminal"})
    app = HerdrApp()
    await app.right_click("t61")
    (argv,) = script.argv
    assert argv[:3] == ["herdr-app", "do", "terminal.right_click"]
    assert json.loads(argv[3]) == {"terminal": "t61", "to": "menu"}
    with pytest.raises(LayoutError, match="no such terminal"):
        await app.right_click("t61")


async def test_herdr_app_keep_ls_close_and_errors(monkeypatch) -> None:
    layout = {
        "spaces": [
            {"id": "spc1", "tabs": [{"id": "tb1", "terminals": [{"terminal": "t1"}]}]},
            {"id": "spc2", "tabs": [{"id": "tb2", "terminals": []}]},
            {"id": "spc3", "tabs": [{"id": "tb3", "terminals": []}, {"id": "tb4"}]},
        ]
    }
    script = Script(
        monkeypatch,
        {},
        layout,
        layout,
        layout,
        layout,
        layout,
        {},
        {"error": "no such space"},
        CommandError(1, "window closed"),
        CommandTimeout("slow"),
        {"pane": "p1"},
    )
    app = HerdrApp()
    await app.keep("t1")
    assert json.loads(script.argv[0][3]) == {"terminal": "t1", "place": "tab"}
    assert await app.tab_terminals("tb1") == ["t1"]
    assert await app.tab_terminals("tb-missing") is None
    assert await app.space_tabs("tb1") == ["tb1"]  # the last tab of spc1
    assert await app.space_tabs("tb-missing") is None
    assert await app.space_tabs("tb4") == ["tb3", "tb4"]
    await app.tab_close("tb1")
    assert json.loads(script.argv[6][3]) == {"id": "tb1", "confirmed": True}
    with pytest.raises(LayoutError, match="no such space"):
        await app.tab_new(space="x", folder="/repo", name="w", argv=["codex"])
    with pytest.raises(LayoutError, match="is the App open"):
        await app.keep("t1")
    # A timeout is not a LayoutError: the command may have run, so no fallback may follow.
    with pytest.raises(RoutingError) as caught:
        await app.keep("t1")
    assert not isinstance(caught.value, LayoutError)
    with pytest.raises(LayoutError, match="no terminal id"):
        await app.tab_new(space="x", folder="/repo", name="w", argv=["codex"])


def test_handoff_state_reads_the_apply_queue(tmp_path: Path) -> None:
    """red_at_base: a valid record settles, but its filename alone does not."""
    queue = tmp_path / "status" / "apply-queue"
    (queue / "done").mkdir(parents=True)
    (queue / "rejected").mkdir()
    (queue / "done" / "T-04-release-prep.json").write_text(
        json.dumps(settled_record(tmp_path, task="T-04-release-prep"))
    )
    (queue / "done" / "T-04-release-prep.just-check.log").write_text("log")
    (queue / "rejected" / "T-07.json").write_text(
        json.dumps(settled_record(tmp_path, task="T-07", state="rejected"))
    )
    (queue / "done" / "T-09-named-by-file.json").write_text("{}")
    (queue / "done" / "broken.json").write_text("{")
    handoffs = QueueHandoffs(tmp_path)
    assert handoffs.state("T-04") == "merged"
    assert handoffs.state("T-07") == "rejected"
    assert handoffs.state("T-09") == "open"  # a filename cannot stand in for a record
    assert handoffs.state("T-0") == "open"  # a prefix of another id is not that id
    assert handoffs.state("T-18") == "open"
    assert QueueHandoffs(tmp_path / "nowhere").state("T-04") == "open"


def test_ledger_returns_todo_notes(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.mkdir()
    assert TasksLedger(tmp_path).queued() == []
    rows = [
        {"id": "T-1", "lane": "todo", "note": "refactor the adapter"},
        {"id": "T-2", "lane": "done", "note": "schema"},
        {"id": "T-3", "lane": "todo"},
    ]
    (status / "tasks.json").write_text(json.dumps({"tasks": rows}))
    assert TasksLedger(tmp_path).queued() == ["refactor the adapter", ""]
    (status / "tasks.json").write_text(json.dumps(rows))
    assert TasksLedger(tmp_path).queued() == ["refactor the adapter", ""]


def test_registry_round_trip_is_private_and_strict(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state" / "routing-workers.json")
    assert registry.load() == {}
    registry.put("t1", {"name": "w-1", "created_ms": 5})
    registry.update("t1", receipt_id="r1")
    registry.update("t-absent", receipt_id="r2")
    assert registry.load() == {"t1": {"name": "w-1", "created_ms": 5, "receipt_id": "r1"}}
    assert registry.path.stat().st_mode & 0o077 == 0
    registry.drop("t1")
    registry.drop("t1")
    assert registry.load() == {}
    registry.path.write_text("[]")
    with pytest.raises(RoutingError, match="unsupported shape"):
        registry.load()
    registry.path.write_text("{")
    with pytest.raises(RoutingError, match="unreadable"):
        registry.load()


def test_cli_registers_route_and_workers() -> None:
    cli = parser()
    args = cli.parse_args(
        ["route", "brief.md", "--task", "T-18", "--class", "port-fix", "--author", "claude"]
    )
    assert (args.command, args.task, args.lane, args.task_class) == (
        "route",
        "T-18",
        "a",
        "port-fix",
    )
    assert args.routing == Path("status/routing.json") and not args.dry_run and not args.new
    gc = cli.parse_args(["workers", "gc"])
    assert gc.workers_action == "gc" and gc.apply is False  # preview unless --apply
    assert cli.parse_args(["workers", "gc", "--apply"]).apply is True
    assert cli.parse_args(["workers", "list"]).workers_action == "list"
    created = cli.parse_args(["workers", "create", "--class", "analysis", "--harness", "grok"])
    assert (created.task_class, created.harness) == ("analysis", "grok")
    with pytest.raises(SystemExit):
        cli.parse_args(["route", "brief.md"])  # --task is required


async def test_cli_dispatch_wires_the_real_ports(tmp_path: Path, monkeypatch) -> None:
    from routing_fakes import FIXTURE

    from answerback_bridge.routing import cli as routing_cli

    seen: dict[str, Any] = {}

    class StubPool:
        policy = None
        bridge = object()

        async def gc(self, *, apply: bool) -> dict[str, Any]:
            seen["apply"] = apply
            return {"status": "planned"}

    def build(args):
        seen["routing"] = args.routing
        return StubPool()

    monkeypatch.setattr(routing_cli, "_pool", build)
    args = parser().parse_args(["workers", "gc", "--routing", str(FIXTURE)])
    assert await routing_cli.dispatch(args) == {"status": "planned"}
    assert seen == {"routing": FIXTURE, "apply": False}

    big = tmp_path / "brief.md"
    big.write_bytes(b"x" * (routing_cli.MAX_BRIEF_BYTES + 1))
    with pytest.raises(ValueError, match="Brief exceeds"):
        routing_cli._brief(big)


def test_ledger_bounds_near_limit_notes_before_classification(tmp_path: Path) -> None:
    """red_at_base: keyword tails beyond 4,096 characters do not affect queued work."""
    from routing_fakes import policy

    from answerback_bridge.routing.policy import classify

    (tmp_path / "status").mkdir()
    note = " " * (ports._MAX_STATUS_BYTES - 256) + "schema"
    (tmp_path / "status/tasks.json").write_text(json.dumps([{"lane": "todo", "note": note}]))
    (text,) = TasksLedger(tmp_path).queued()
    assert text == note[:4096]
    assert classify(policy(), text).name is None
    note = " " * (4096 - len("schema")) + "schema" + " refactor refactor"
    (tmp_path / "status/tasks.json").write_text(
        json.dumps({"tasks": [{"lane": "todo", "note": note}]})
    )
    (text,) = TasksLedger(tmp_path).queued()
    assert len(text) == 4096 and classify(policy(), text).name == "design-impl"


# T-72 card 3


def settled_record(project: Path, *, task: str = "T-72", state: str = "applied") -> dict[str, Any]:
    """Build a queue-contract record backed by local dummy evidence files."""
    evidence = project / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "handoff.md").write_text("Dummy handoff\n")
    (evidence / "review.md").write_text("Dummy review\n")
    return {
        "task": task,
        "root": "merge-only",
        "branch": "task-T72-test",
        "handoff": "evidence/handoff.md",
        "reviewer": "test-reviewer",
        "reviewer_verdict": "PASS",
        "review_evidence": "evidence/review.md#gate",
        "expected_shape": "merge-only: no infrastructure change",
        "requested_at": "2026-11-20T00:00:00Z",
        "state": state,
        "apply_result": "Dummy merge completed",
        "reason": "Dummy rejection reason",
    }


def queue_record(project: Path, record: Any, folder: str = "done") -> Path:
    directory = project / "status/apply-queue" / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "T-72.json"
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize(
    "record",
    [
        {"task": "T-72"},
        {},
        [],
        None,
        {"task": "T-72", "state": "applied", "reviewer_verdict": "PASS"},
    ],
)
def test_handoff_incomplete_record_stays_open(tmp_path: Path, record: Any) -> None:
    """red_at_base: neither a filename nor a self-reported verdict proves settlement."""
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize(
    "field",
    [
        "task",
        "root",
        "branch",
        "handoff",
        "reviewer",
        "reviewer_verdict",
        "review_evidence",
        "expected_shape",
        "requested_at",
        "state",
        "apply_result",
    ],
)
def test_handoff_requires_every_contract_field(tmp_path: Path, field: str) -> None:
    """red_at_base: all required fields must be present and nonempty strings."""
    record = settled_record(tmp_path)
    record.pop(field)
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "reviewed"),
        ("reviewer_verdict", "FAIL"),
        ("reviewer_verdict", []),
        ("apply_result", {}),
        ("handoff", "../outside.md"),
        ("review_evidence", ""),
        ("follow_up_plans", [None]),
    ],
)
def test_handoff_invalid_shapes_and_references_stay_open(
    tmp_path: Path, field: str, value: Any
) -> None:
    """red_at_base: settlement uses a checked lifecycle and confined evidence paths."""
    record = settled_record(tmp_path)
    record[field] = value
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize("where", ["entry", "done", "apply-queue", "status", "evidence"])
def test_handoff_does_not_follow_symlinks(tmp_path: Path, where: str) -> None:
    """red_at_base: valid-looking data through any untrusted path component stays open."""
    record = settled_record(tmp_path)
    path = queue_record(tmp_path, record)
    target = {
        "entry": path,
        "done": path.parent,
        "apply-queue": path.parents[1],
        "status": path.parents[2],
        "evidence": tmp_path / "evidence",
    }[where]
    moved = tmp_path / "relocated"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=where != "entry")
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


def test_handoff_rejected_requires_reason_and_matching_lifecycle(tmp_path: Path) -> None:
    """red_at_base: rejection cannot be inferred just from its directory."""
    record = settled_record(tmp_path, state="rejected")
    record["reason"] = " "
    path = queue_record(tmp_path, record, "rejected")
    assert QueueHandoffs(tmp_path).state("T-72") == "open"
    record["reason"] = "Review failed"
    record["reviewer_verdict"] = "FAIL"
    path.write_text(json.dumps(record))
    assert QueueHandoffs(tmp_path).state("T-72") == "rejected"


def test_handoff_checks_plan_scalar_contract(tmp_path: Path) -> None:
    """red_at_base: primary and follow-up plan fields must satisfy scalar syntax."""
    record = settled_record(tmp_path)
    (tmp_path / "terraform/test").mkdir(parents=True)
    record.update(
        root="test",
        plan_file="evidence/plan.bin",
        plan_sha256="invalid",
        planned_shape="1 to add, 0 to change, 0 to destroy",
    )
    path = queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"
    record["plan_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    assert QueueHandoffs(tmp_path).state("T-72") == "merged"
    record["follow_up_plans"] = [
        {key: record[key] for key in ("plan_file", "plan_sha256", "planned_shape", "apply_result")}
    ]
    record["follow_up_plans"][0]["planned_shape"] = "unknown"
    path.write_text(json.dumps(record))
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


def test_handoff_conflicting_settlements_stay_open(tmp_path: Path) -> None:
    """red_at_base: glob ordering must not resolve contradictory records."""
    queue_record(tmp_path, settled_record(tmp_path))
    queue_record(tmp_path, settled_record(tmp_path, state="rejected"), "rejected")
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


def test_status_reader_rejects_deep_json_and_nonregular_files(tmp_path: Path) -> None:
    """red_at_base: excessively nested JSON must be refused."""
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 2000 + "]" * 2000)
    assert ports._read_json(deep) is None
    assert ports._read_json(tmp_path) is None


def test_status_reader_rejects_oversize_and_leaf_symlink(tmp_path: Path) -> None:
    """red_at_base: a symlink is refused; the existing byte cap stays enforced."""
    source = tmp_path / "source.json"
    source.write_text('{"task": "T-72"}')
    link = tmp_path / "link.json"
    link.symlink_to(source)
    assert ports._read_json(link) is None
    source.write_bytes(b" " * (ports._MAX_STATUS_BYTES + 1))
    assert ports._read_json(source) is None


# Field inventories of the eleven tracked done/ records, with dummy values below.
_CONTRACT_SHAPE = (
    "task root branch handoff reviewer reviewer_verdict review_evidence expected_shape "
    "requested_at state notes apply_result applied_by"
)
_MERGE_SHAPE = "id branch branch_tip merge_commit target review served_by served_at"
_RECORDED_SHAPES = {
    "R2-integration": _CONTRACT_SHAPE,
    "T-04-release-prep": _CONTRACT_SHAPE,
    "T-04-release": _CONTRACT_SHAPE,
    "T-05-live-checklist": _CONTRACT_SHAPE,
    "T-16-events": _CONTRACT_SHAPE + " tip",
    "T-ledger-reconcile": _CONTRACT_SHAPE,
    "T-17-tracing": _MERGE_SHAPE + " rebased gate",
    "T-18-routing": _MERGE_SHAPE + " gate conflicts",
    "T-34-ci": _MERGE_SHAPE + " gate_macos gate_linux",
    "T-40-debug": _MERGE_SHAPE + " gate_macos gate_linux",
    "T-38-release": (
        "id branch release_commit tag gate wheel scratch_qualification install served_by "
        "served_at state"
    ),
}


def recorded_shape(project: Path, shape: str) -> dict[str, Any]:
    """Use recorded field names with synthetic content and a local hand-off note."""
    fields = _RECORDED_SHAPES[shape].split()
    record = dict.fromkeys(fields, "dummy")
    if "task" in fields:
        record.update({k: v for k, v in settled_record(project).items() if k in fields})
        record["review_evidence"] = "status/apply-queue/moved-review.log#gate"
    else:
        record["id"] = "T-72-example"
        record["served_at"] = "2026-11-20T00:00:00Z"
        if "merge_commit" in fields:
            record["merge_commit"] = "1" * 40
        else:
            record.update(release_commit="2" * 40, state="applied")
    return record


@pytest.mark.parametrize("shape", _RECORDED_SHAPES)
@pytest.mark.parametrize(("folder", "expected"), [("done", "merged"), ("rejected", "rejected")])
def test_handoff_checks_recorded_contract_shapes(
    tmp_path: Path, shape: str, folder: str, expected: str
) -> None:
    """Served-only shapes are red_at_base; contract shapes pin existing behavior."""
    record = recorded_shape(tmp_path, shape)
    if folder == "rejected" and "state" in record:
        record["state"] = "rejected"
        if "task" in record:
            record["reason"] = "Dummy rejection"
    queue_record(tmp_path, record, folder)
    assert QueueHandoffs(tmp_path).state("T-72") == (expected if "task" in record else "open")
    assert QueueHandoffs(tmp_path).state("T-7") == "open"


@pytest.mark.parametrize("shape", _RECORDED_SHAPES)
@pytest.mark.parametrize(
    "damage", ["missing-name", "wrong-name-type", "empty-commit", "wrong-commit-type", "bad-time"]
)
def test_handoff_recorded_shapes_require_typed_content(
    tmp_path: Path, shape: str, damage: str
) -> None:
    """red_at_base except task wrong-name-type (pins_existing): content must settle."""
    record = recorded_shape(tmp_path, shape)
    identity = "task" if "task" in record else "id"
    commit = next(k for k in ("apply_result", "merge_commit", "release_commit") if k in record)
    if damage == "missing-name":
        record.pop(identity)
    elif damage == "wrong-name-type":
        record[identity] = ["T-72"]
    elif damage == "empty-commit":
        record[commit] = " "
    elif damage == "wrong-commit-type":
        record[commit] = {"value": "dummy"}
    else:
        record["requested_at" if identity == "task" else "served_at"] = []
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize(
    "changes",
    [
        {"review_evidence": "missing.md#gate"},
        {"review_evidence": "Dummy review reference"},
        {"plan_file": "evidence/absent.bin"},
        {"plan_sha256": "0" * 64},
        {"planned_shape": "0 to add, 0 to change, 0 to destroy"},
        {
            "root": "test",
            "plan_file": "evidence/absent.bin",
            "plan_sha256": "0" * 64,
            "planned_shape": "0 to add, 0 to change, 0 to destroy",
        },
        {
            "follow_up_plans": [
                {
                    "plan_file": "evidence/absent.bin",
                    "plan_sha256": "0" * 64,
                    "planned_shape": "0 to add, 0 to change, 0 to destroy",
                    "apply_result": "Dummy follow-up",
                }
            ]
        },
    ],
)
def test_handoff_uses_validate_without_optional_file_verification(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    """pins_existing at BASE; red at reviewed tip: validate is distinct from verify."""
    import runpy

    validate = runpy.run_path(str(Path(__file__).parents[1] / "status/apply_queue.py"))["validate"]
    record = settled_record(tmp_path)
    (tmp_path / "terraform/test").mkdir(parents=True)
    record.update(changes)
    assert validate(record, root=tmp_path) == []
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "merged"


@pytest.mark.parametrize("field", ["handoff", "plan_file"])
def test_handoff_parent_traversal_to_existing_file_stays_open(tmp_path: Path, field: str) -> None:
    """red_at_base: a real sibling file cannot be used as a project evidence path."""
    project = tmp_path / "project"
    project.mkdir()
    record = settled_record(project)
    outside = tmp_path / "outside.md"
    outside.write_text("Dummy hand-off outside the project\n")
    path = queue_record(project, record)
    assert QueueHandoffs(project).state("T-72") == "merged"
    record[field] = "../outside.md"
    path.write_text(json.dumps(record))
    assert QueueHandoffs(project).state("T-72") == "open"


def test_handoff_conflicting_recorded_shapes_stay_open(tmp_path: Path) -> None:
    """red_at_base: contradictory contracts do not acquire precedence from scan order."""
    queue_record(tmp_path, settled_record(tmp_path))
    rejection = recorded_shape(tmp_path, "T-34-ci")
    rejection.update(settled_record(tmp_path, state="rejected"))
    queue_record(tmp_path, rejection, "rejected")
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize("folder", ["done", "rejected"])
def test_handoff_minimal_served_merge_receipt_stays_open(tmp_path: Path, folder: str) -> None:
    """red_at_base: three receipt strings cannot replace the queue contract."""
    record = {"id": "T-72", "merge_commit": "1" * 40, "served_at": "2026-11-20T00:00:00Z"}
    queue_record(tmp_path, record, folder)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize("folder", ["done", "rejected"])
def test_handoff_unverified_receipt_probe_stays_open(tmp_path: Path, folder: str) -> None:
    """pins_existing at BASE; red at review tip: id alone never names a task."""
    record = {"id": "T-ATTACK", "served_at": "2026-11-20", "merge_commit": "deadbeef"}
    path = queue_record(tmp_path, record, folder)
    path.rename(path.with_name("anyone-can-write-this.json"))
    assert QueueHandoffs(tmp_path).state("T-ATTACK") == "open"


def test_handoff_served_only_rejection_cannot_override_contract(tmp_path: Path) -> None:
    """pins_existing at BASE; red at review tip: only contract records can conflict."""
    queue_record(tmp_path, settled_record(tmp_path))
    queue_record(tmp_path, recorded_shape(tmp_path, "T-34-ci"), "rejected")
    assert QueueHandoffs(tmp_path).state("T-72") == "merged"


def test_handoff_contract_ignores_receipt_metadata(tmp_path: Path) -> None:
    """pins_existing: extra receipt metadata cannot override the contract's task."""
    record = settled_record(tmp_path)
    record.update(id="T-OTHER", merge_commit=None, branch_tip=[], gate={})
    queue_record(tmp_path, record)
    handoffs = QueueHandoffs(tmp_path)
    assert handoffs.state("T-72") == "merged"
    assert handoffs.state("T-OTHER") == "open"


def test_handoff_t49_contract_superset_requires_task(tmp_path: Path) -> None:
    """pins_existing: the 21-key T-49 shape settles only with its task field."""
    import runpy

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "dummy-handoff.md").write_text("Dummy handoff\n")
    record = {
        "applied_by": "dummy-applier",
        "apply_result": "Dummy merge completed",
        "branch": "task-dummy-contract",
        "branch_tip": "1" * 40,
        "expected_shape": "merge-only: no infrastructure change",
        "gate_linux": "evidence/dummy-linux.log",
        "gate_macos": "evidence/dummy-macos.log",
        "handoff": "evidence/dummy-handoff.md",
        "id": "T-DUMMY-49",
        "merge_commit": "2" * 40,
        "notes": "Dummy contract with receipt metadata",
        "requested_at": "2026-01-01T00:00:00Z",
        "review_evidence": "evidence/dummy-review.log",
        "reviewer": "dummy-reviewer",
        "reviewer_verdict": "PASS",
        "root": "merge-only",
        "served_at": "2026-01-01T01:00:00Z",
        "served_by": "dummy-server",
        "state": "applied",
        "target": "dummy-target",
        "task": "T-DUMMY-49",
    }
    assert len(record) == 21 and all(isinstance(value, str) for value in record.values())
    validate = runpy.run_path(str(Path(__file__).parents[1] / "status/apply_queue.py"))["validate"]
    assert validate(record, root=tmp_path) == []
    path = queue_record(tmp_path, record)
    handoffs = QueueHandoffs(tmp_path)
    assert handoffs.state("T-DUMMY-49") == "merged"
    assert handoffs.state("T-72") == "open"
    del record["task"]
    path.write_text(json.dumps(record))
    assert handoffs.state("T-DUMMY-49") == "open"


@pytest.mark.parametrize("field", ["plan_sha256", "planned_shape"])
@pytest.mark.parametrize("follow_up", [False, True], ids=["primary", "follow-up"])
def test_handoff_plan_scalar_trailing_newline_stays_open(
    tmp_path: Path, field: str, follow_up: bool
) -> None:
    """red_at_base: the entire primary or follow-up scalar must match its syntax."""
    record = settled_record(tmp_path)
    plan = {
        "plan_file": "evidence/plan.bin",
        "plan_sha256": "a" * 64,
        "planned_shape": "1 to add, 0 to change, 0 to destroy",
        "apply_result": "Dummy plan applied",
    }
    if follow_up:
        record["follow_up_plans"] = [plan]
    else:
        record.update(plan)
        plan = record
    path = queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "merged"
    plan[field] += "\n"
    path.write_text(json.dumps(record))
    assert QueueHandoffs(tmp_path).state("T-72") == "open"


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "reviewed"},
        {"state": "rejected"},
        {"state": []},
        {"task": []},
        {"merge_commit": ""},
    ],
)
def test_handoff_release_receipt_cannot_mask_invalid_fields(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    """red_at_base except ill-typed task (pins_existing): formats cannot bypass checks."""
    record = recorded_shape(tmp_path, "T-38-release")
    record.update(changes)
    queue_record(tmp_path, record)
    assert QueueHandoffs(tmp_path).state("T-72") == "open"
