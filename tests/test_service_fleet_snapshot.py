"""Contract tests for the private, read-only fleet snapshot RPC."""

import ast
import importlib
import json
import re
import sqlite3
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from composition import PLUGIN_PROVIDERS

from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.config import Config
from answerback_bridge.plugin import receipt_reasons
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import STATES, TaskStore
from answerback_bridge.transport import encode

ROW_KEYS = {
    "id",
    "request_key",
    "target",
    "status",
    "created",
    "updated",
    "title",
    "reason",
    "cause",
}
IDENT = "11111111-2222-3333-4444-555555555555"


def _module() -> ModuleType:
    return importlib.import_module("answerback_bridge.receipt_snapshot")


def _rules() -> tuple[Any, ...]:
    plugins = [p for p in builtin_plugins() if p.definition.registration not in PLUGIN_PROVIDERS]
    return receipt_reasons(plugins)


def _reason(status: str, detail: str | None) -> tuple[str | None, str | None]:
    return _module().reason(status, detail, rules=_rules())


def _seed(store: TaskStore, count: int, status: str = "uncertain", start: int = 0) -> None:
    """Insert large and hostile fixtures without the admission API's key restrictions."""
    with store.connection() as db:
        db.executemany(
            "INSERT INTO tasks (id,request_key,target,message,status,created,updated,metadata) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                (f"id-{i}", f"review-{i}", f"hc:t{i}", "private", status, i, i, "{}")
                for i in range(start, start + count)
            ),
        )


def _snapshot(store: TaskStore) -> dict[str, Any]:
    return _module().build(
        read_at=100.0,
        **store.snapshot_read(deadline=time.monotonic() + 1.5),
        bus_keys={},
    )


def _connect_as(monkeypatch: pytest.MonkeyPatch, factory: type[sqlite3.Connection]) -> None:
    original = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(database, *args, **{**kwargs, "factory": factory})

    monkeypatch.setattr(sqlite3, "connect", connect)


def _fail_queries(monkeypatch: pytest.MonkeyPatch, sections: set[str]) -> None:
    class Failed(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Fail only the selected section's query, preserving the real connection."""
            section = (
                "counts"
                if "GROUP BY status" in sql
                else "needs_person"
                if "WHERE status IN" in sql
                else "recent"
                if "ORDER BY created" in sql
                else None
            )
            if section in sections:
                raise OSError("PRIVATE_EXCEPTION")
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Failed)


def test_counts_include_every_state_and_total_with_zeros(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    assert _snapshot(store)["counts"] == {**dict.fromkeys(STATES, 0), "total": 0}
    for i, status in enumerate(STATES):
        _seed(store, i + 1, status, start=100 * i)
    assert _snapshot(store)["counts"] == {**store.counts(), "total": 55}


@pytest.mark.parametrize("status", ["uncertain", "blocked"])
def test_needs_person_includes_receipt_older_than_150_newer_receipts(
    tmp_path: Path,
    status: str,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    _seed(store, 1, status)
    _seed(store, 151, "completed", start=1)
    answer = _snapshot(store)
    assert [r["id"] for r in answer["needs_person"]] == ["id-0"]
    assert "id-0" in {r["id"] for r in answer["tasks"]}
    assert answer["needs_person_total"] == 1
    assert answer["counts"][status] == 1
    assert answer["needs_person_omitted"] == 0


def test_tasks_start_with_recent_in_task_list_order_and_have_unique_ids(tmp_path: Path) -> None:
    bridge = Bridge(Config("example"), tmp_path / "config.json")
    _seed(bridge.store, 30)
    with bridge.store.connection() as db:
        db.execute("UPDATE tasks SET updated=1000 WHERE id='id-0'")
    answer = _snapshot(bridge.store)
    assert [r["id"] for r in answer["recent"]] == [
        r["id"] for r in bridge.task_list(limit=20)["tasks"]
    ]
    assert answer["needs_person"][0]["id"] == "id-0"
    assert answer["tasks"][:20] == answer["recent"]
    assert len(answer["tasks"]) == len({r["id"] for r in answer["tasks"]}) == 30


def test_answer_contains_no_message_result_detail_metadata_turn_or_abandonment(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    marker = "PRIVATE_BODY_MARKER"
    task, _ = store.reserve("hc:t84", marker, "review", {"private": marker})
    task = store.update(task["id"], "uncertain", result=marker, detail=marker, turn_id=marker)
    assert marker not in encode(_snapshot(store)).decode()
    store.abandon(task, marker)
    answer = _snapshot(store)
    assert marker not in encode(answer).decode()
    assert answer["tasks"][0]["reason"] == "abandoned_by_person"


@pytest.mark.parametrize(
    ("key", "target", "expected"),
    [
        ("ab-20260115-review-docs-t12", "hc:t12", "review-docs"),
        ("t3a-desktop-probe-20260115-1", "hc:t12", "t3a-desktop-probe-1"),
        ("ab-20260115-harness-check-1", "hc:t12", "harness-check-1"),
        ("build-10000000-run", "hc:t84", "build-10000000-run"),
        ("ab-20261340-x", "hc:t84", "ab-20261340-x"),
        ("ab-20261132-x", "hc:t84", "ab-20261132-x"),
        ("ab-20261100-x", "hc:t84", "ab-20261100-x"),
        ("ab-19991231-x", "hc:t84", "ab-19991231-x"),
        ("ab-21000101-x", "hc:t84", "ab-21000101-x"),
        ("ab-20000101-x", "hc:t84", "x"),
        ("ab-20991231-x", "hc:t84", "x"),
        ("x" * 200, "hc:t84", "x" * 80),
        (IDENT, "hc:t84", None),
        (f"bus:{IDENT}", "hc:t84", None),
        ("ab-20261120-", "hc:t84", None),
        ("review-t84", "hc:example:worker-a/t84", "review"),
        ("review-t85", "hc:example:worker-a/t84", "review-t85"),
        ("review-t84", "desktop:t84", "review-t84"),
        ("review-202611201", "hc:t84", "review-202611201"),
        ("ab-20261120-ab-20261120-work", "hc:t84", "ab-20261120-work"),
        ("some-work-20261120-20261121", "hc:t84", "some-work-20261121"),
    ],
)
def test_title_uses_only_sender_key_and_matching_core_terminal(
    key: str,
    target: str,
    expected: str | None,
) -> None:
    assert _module().title(key, target, {}) == expected


def test_bus_title_uses_command_request_key() -> None:
    assert _module().title(f"bus:{IDENT}", "hc:t84", {IDENT: "ab-20261120-review-t84"}) == "review"


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "Bridge stopped during submission; inspect before abandoning",
            "bridge_stopped_during_submission",
        ),
        ("Bridge stopped with core delivery pending", "bridge_stopped_during_delivery"),
        ("Core delivery interrupted; inspect before retrying", "delivery_interrupted"),
        ("Submission was interrupted", "delivery_interrupted"),
        ("Crew submission was interrupted", "delivery_interrupted"),
        ("Native worker interrupted; no automatic replay", "delivery_interrupted"),
        ("Bridge interrupted Kiro; no automatic replay", "delivery_interrupted"),
        ("Core delivery unverified (OSError); inspect before retrying", "delivery_unverified"),
        (
            "Core delivery not attempted: the terminal could not be read before the send; "
            "nothing was sent",
            "delivery_not_attempted",
        ),
        (
            "Core delivery not attempted: the machine list could not be read; nothing was sent",
            "delivery_not_attempted",
        ),
        (
            "Core delivery not attempted: the machine is not connected; nothing was sent",
            "machine_not_connected",
        ),
        (
            "Core delivery not attempted: the machine list names the machine more than once; "
            "nothing was sent",
            "machine_listed_twice",
        ),
        (
            "Core delivery not attempted: the machine list gives the machine an id that is "
            "not 32 lowercase hex digits; nothing was sent",
            "machine_id_malformed",
        ),
        (
            "Core delivery not attempted: the machine's core id is not the one pinned at its "
            "first send (machine_id_changed); nothing was sent",
            "machine_id_changed",
        ),
        ("Core accepted text but did not observe the turn start", "turn_start_unobserved"),
        (
            "Core terminal needs a recognized foreground command, known directory, "
            "and reported idle",
            "recipient_not_ready",
        ),
        ("Remote Core terminals support reading only", "remote_read_only"),
        ("Kiro confirmed cancellation", "cancel_confirmed"),
        ("Awaiting a human permission decision", "awaiting_permission"),
        ("Cancelled before prompt submission", "cancelled_before_submission"),
        ("Bridge stopped before worker settled", "bridge_stopped_before_worker_settled"),
        (
            "Kiro task exceeded the 600-second active execution budget after its prompt was sent; "
            "no automatic replay",
            "kiro_deadline_exceeded",
        ),
        (
            "codex_cli task exceeded the 600-second active execution budget "
            "after its prompt was sent; no automatic replay",
            "codex_cli_deadline_exceeded",
        ),
        (
            "claude_code task exceeded the 600-second active execution budget "
            "after its prompt was sent; no automatic replay",
            "claude_code_deadline_exceeded",
        ),
        (
            "grok task exceeded the 600-second active execution budget after its prompt was sent; "
            "no automatic replay",
            "grok_deadline_exceeded",
        ),
        ("Core send requires the selected terminal identity", "recipient_identity_missing"),
        ("Core foreground agent changed before send", "recipient_changed_before_send"),
        ("Core terminal identity changed before send", "recipient_changed_before_send"),
        (
            "Core send receipt identifies a different terminal; delivery is "
            "uncertain. Do not resubmit blindly.",
            "delivery_identity_mismatch",
        ),
        (
            "Core foreground agent changed after send; delivery is uncertain. Do not "
            "resubmit blindly.",
            "recipient_changed_after_send",
        ),
        (
            "Core terminal identity changed after send; delivery is uncertain. Do "
            "not resubmit blindly.",
            "recipient_changed_after_send",
        ),
        (
            "Machine's core id was pinned to another id while this delivery was sent "
            "(machine_id_changed); delivery is uncertain. Do not resubmit blindly.",
            "machine_id_changed",
        ),
    ],
)
def test_fixed_detail_sentences_give_only_reason_codes(detail: str, expected: str) -> None:
    assert _reason("uncertain", detail) == (
        expected,
        "recipient_not_ready" if expected == "recipient_not_ready" else None,
    )


@pytest.mark.parametrize(
    "detail",
    [
        None,
        "PRIVATE_FREE_DETAIL",
        " Remote Core terminals support reading only",
        "remote core terminals support reading only",
        "Submission was interrupted ",
        "Core delivery unverified (a name); inspect before retrying",
        "Core delivery unverified (" + "E" * 65 + "); inspect before retrying",
    ],
)
def test_free_or_near_matching_details_are_unclassified(detail: str | None) -> None:
    for status in ("failed", "uncertain", "blocked", "cancelled"):
        assert _reason(status, detail) == ("unclassified", None)


@pytest.mark.parametrize("status", ["pending", "queued", "submitted", "running", "completed"])
def test_active_and_completed_receipts_have_null_reason_and_cause(status: str) -> None:
    assert _reason(status, "Submission was interrupted") == (None, None)


def test_abandoned_reason_never_uses_persons_words() -> None:
    assert _reason("abandoned", "Submission was interrupted") == (
        "abandoned_by_person",
        None,
    )


def _assert_constants(
    source: Path,
    fragments: tuple[str, ...],
    counts: tuple[int, ...],
    *,
    begins: bool = False,
) -> None:
    constants = [
        node.value
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert len(fragments) == len(counts)
    for fragment, expected in zip(fragments, counts, strict=True):
        if expected == 0:
            continue
        found = (
            sum(value.startswith(fragment) for value in constants)
            if begins
            else constants.count(fragment)
        )
        assert found >= expected, (
            f"{source.name}: expected at least {expected} of {fragment!r}, found {found}"
        )


def test_provider_owned_reason_rules_keep_historical_order() -> None:
    rules = _rules()
    assert len(rules) == 61
    assert [
        (index, rules[index].code, rules[index].parts[0])
        for index in (0, 1, 4, 5, 6, 14, 17, 22, 32, 42, 45, 56, 57, 58, 60)
    ] == [
        (
            0,
            "bridge_stopped_during_submission",
            "Bridge stopped during submission; inspect before abandoning",
        ),
        (1, "bridge_stopped_during_delivery", "Bridge stopped with core delivery pending"),
        (4, "delivery_interrupted", "Crew submission was interrupted"),
        (5, "delivery_interrupted", "Native worker interrupted; no automatic replay"),
        (6, "delivery_interrupted", "Bridge interrupted Kiro; no automatic replay"),
        (14, "turn_start_unobserved", "Core accepted text but did not observe the turn start"),
        (17, "cancel_confirmed", "Kiro confirmed cancellation"),
        (22, "codex_cli_deadline_exceeded", "codex_cli task exceeded the "),
        (32, "recipient_binding_changed", "Observed Rex terminal binding changed"),
        (
            42,
            "recipient_binding_changed",
            "Crew chat identity changed or its identity evidence is incomplete",
        ),
        (45, "native_execution_unverified", ": "),
        (56, "recipient_configuration_changed", ": "),
        (57, "permission_request_too_large", ": "),
        (58, "cancel_confirmed", " confirmed "),
        (60, "delivery_unverified", "Crew delivery is uncertain: "),
    ]
    assert rules[57].sources == {
        "providers/kiro/workers.py": (1, 0),
        "worker_runtime.py": (0, 1),
        "providers/native/workers.py": (1, 0),
    }


def test_reason_table_sentences_remain_constants_in_each_writer() -> None:
    root = Path(__file__).resolve().parents[1] / "src/answerback_bridge"
    for rule in _rules():
        for source, counts in rule.sources.items():
            _assert_constants(root / source, rule.parts, counts, begins=rule.begins)


def test_reason_table_drift_guard_rejects_sentence_no_writer_holds() -> None:
    source = Path(__file__).resolve().parents[1] / "src/answerback_bridge/tasks.py"
    with pytest.raises(AssertionError, match="expected at least 1 of .*found 0"):
        _assert_constants(source, ("This invented sentence is not a receipt detail.",), (1,))


def test_drift_guard_notices_one_of_two_write_sites_reworded(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src/answerback_bridge/providers/codex/adapter.py"
    )
    sentence = "Submission was interrupted"
    changed = tmp_path / "codex.py"
    changed.write_text(source.read_text().replace(sentence, "A reworded detail", 1))
    with pytest.raises(AssertionError, match="expected at least 2 of .*found 1"):
        _assert_constants(changed, (sentence,), (2,))


def test_ten_thousand_uncertain_receipts_have_bounded_rows_totals_and_size(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    _seed(store, 10_000)
    answer = _snapshot(store)
    assert len(answer["needs_person"]) == len(answer["tasks"]) == 200
    assert answer["needs_person_total"] == 10_000
    assert answer["needs_person_omitted"] == 9_800
    assert len(encode(answer)) < 256 * 1024


def test_hostile_strings_and_disjoint_lists_stay_below_wire_bound(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    _seed(store, 200)
    _seed(store, 20, "completed", start=200)
    with store.connection() as db:
        db.execute(
            "UPDATE tasks SET request_key=id || ?, target=?",
            ("\x1b\n\u202e😀" * 2000, "\x00\t\u0085😀" * 2000),
        )
    answer = _snapshot(store)
    assert len(answer["tasks"]) == 220
    for row in answer["tasks"]:
        assert len(row["id"]) <= 64
        assert len(row["request_key"]) <= 160
        assert len(row["target"]) <= 160
        assert row["title"] is None or len(row["title"]) <= 80
        assert all(c not in json.dumps(row, ensure_ascii=False) for c in ("\u202e", "\u0085"))
        assert all(
            c not in row[k] for k in ("request_key", "target") for c in ("\x1b", "\n", "\x00", "\t")
        )
    assert len(encode(answer)) < 256 * 1024


def test_every_row_has_exactly_nine_keys_and_reader_compatible_codes(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    for i, status in enumerate(STATES):
        _seed(store, 1, status, start=i)
    for row in _snapshot(store)["tasks"]:
        assert set(row) == ROW_KEYS
        assert row["reason"] is None or re.fullmatch(r"[a-z][a-z_]{0,63}", row["reason"])
        assert row["cause"] in (None, "peer_not_running", "recipient_not_ready")
        assert row["created"] == row["updated"] == int(row["id"][3:])


def test_rows_never_say_which_core_read_the_identity(tmp_path: Path) -> None:
    """T-226 review F2: without metadata a row cannot know an identity was read.

    A failed or never-sent hc receipt must not show `identity read by`, so the
    snapshot carries no `identity_observer` at all.
    """
    store = TaskStore(tmp_path / "tasks.sqlite")
    for i, status in enumerate(STATES):
        _seed(store, 1, status, start=i)
    assert all("identity_observer" not in row for row in _snapshot(store)["tasks"])
    assert not hasattr(_module(), "observer")


def test_store_projections_never_read_body_or_metadata_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    _seed(store, 2)

    # SQLite's authorizer observes columns actually read, including SELECT * expansion.
    def authorize(action: int, table: str, column: str, database: str, trigger: str) -> int:
        if action == sqlite3.SQLITE_READ and table == "tasks":
            assert column not in {"message", "result", "metadata", "turn_id"}
        return sqlite3.SQLITE_OK

    class Checked(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Let SQLite reject reads of excluded columns."""
            self.set_authorizer(authorize)
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Checked)
    assert len(_snapshot(store)["tasks"]) == 2


@pytest.fixture
async def rpc() -> Any:
    """Serve the real handler over a short, isolated Unix socket."""
    import asyncio
    import tempfile

    from answerback_bridge.runtime import Service
    from answerback_bridge.transport import RemoteBridge

    with tempfile.TemporaryDirectory(prefix="ab-fleet-", dir="/tmp") as folder:
        path = Path(folder) / "config.json"
        config = Config("example")
        config.save(path)
        service = Service(path)
        service.bridge = Bridge(config, path)
        service.path.parent.mkdir(mode=0o700)
        try:
            async with await asyncio.start_unix_server(service.handle, path=service.path):
                yield service, RemoteBridge(path)
        finally:
            await service.bridge.close()


async def test_status_and_task_list_keep_their_existing_fields(rpc: Any) -> None:
    service, remote = rpc
    _seed(service.bridge.store, 1)
    status = await remote.call("status", {})
    assert set(status) == {
        "version",
        "runtime",
        "running",
        "accepting",
        "pid",
        "instance_id",
        "started",
        "socket",
        "tasks",
        "bus",
        "providers",
        "clients",
        "herdr_session",
        "desktop_aliases",
    }
    assert status["tasks"] == {"uncertain": 1}
    listed = await remote.call("task_list", {})
    assert set(listed) == {"tasks", "searched_latest"}
    assert listed["searched_latest"] == 100
    assert set(listed["tasks"][0]) == {
        "id",
        "request_key",
        "target",
        "status",
        "created",
        "updated",
        "detail",
        "has_result",
        "result_available",
    }


async def test_rpc_returns_complete_schema_and_runtime_health_without_status_reads(
    rpc: Any,
) -> None:
    import time

    service, remote = rpc
    expected_health = {
        key: service.status()[key]
        for key in (
            "running",
            "accepting",
            "instance_id",
            "started",
            "version",
        )
    }
    before = time.time()
    answer = await remote.call("fleet_snapshot", {})
    assert set(answer) == {
        "schema",
        "read_at",
        "health",
        "counts",
        "needs_person",
        "needs_person_total",
        "needs_person_omitted",
        "recent",
        "tasks",
        "errors",
    }
    assert answer["schema"] == "answerback.fleet_snapshot/1"
    assert before <= answer["read_at"] <= time.time()
    assert answer["health"] == expected_health


@pytest.mark.parametrize("section", ["counts", "needs_person", "recent"])
async def test_section_read_failure_is_null_and_preserves_other_sections(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    service, remote = rpc
    _seed(service.bridge.store, 1)

    _fail_queries(monkeypatch, {section})
    answer = await remote.call("fleet_snapshot", {})
    assert answer[section] is None
    assert answer["errors"] == [{"section": section, "error": "store read failed"}]
    assert "PRIVATE_EXCEPTION" not in encode(answer).decode()
    assert answer["health"]["running"] is True
    for other in {"counts", "needs_person", "recent"} - {section}:
        assert answer[other] is not None
    if section == "needs_person":
        assert answer["needs_person_total"] is answer["needs_person_omitted"] is None
    assert len(answer["tasks"]) == 1


async def _reply(remote: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    import asyncio

    from answerback_bridge.transport import RPC_VERSION

    reader, writer = await asyncio.open_unix_connection(remote.path)
    try:
        writer.write(
            encode(
                {
                    "version": RPC_VERSION,
                    "method": "fleet_snapshot",
                    "config_path": str(remote.config_path),
                    "params": params or {},
                }
            )
        )
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


async def test_both_receipt_lists_failing_returns_ordinary_private_error(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, remote = rpc

    _fail_queries(monkeypatch, {"needs_person", "recent"})
    reply = await _reply(remote)
    assert reply == {
        "version": 1,
        "ok": False,
        "error": "Bridge request failed; inspect service logs and the task receipt.",
        "error_info": {"kind": "internal"},
    }


async def test_store_read_runs_off_loop_while_another_coroutine_makes_progress(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import time

    service, remote = rpc
    entered, left = threading.Event(), threading.Event()
    original = service.bridge.store.snapshot_read

    def held_read(**kwargs: Any) -> Any:
        entered.set()
        time.sleep(0.5)
        left.set()
        return original(**kwargs)

    monkeypatch.setattr(service.bridge.store, "snapshot_read", held_read)
    work = asyncio.create_task(remote.call("fleet_snapshot", {}))
    pulses = 0
    while not work.done():
        await asyncio.sleep(0.01)
        if entered.is_set() and not left.is_set():
            pulses += 1
    await work
    assert pulses >= 10


async def test_request_deadline_returns_ordinary_timeout_after_two_seconds(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from answerback_bridge.transport import request_seconds, timeout_message

    service, remote = rpc
    release = threading.Event()
    original = service.bridge.store.snapshot_read

    def held_read(**kwargs: Any) -> Any:
        release.wait(3.5)
        return original(**kwargs)

    monkeypatch.setattr(service.bridge.store, "snapshot_read", held_read)
    started = time.monotonic()
    try:
        reply = await _reply(remote)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert reply["ok"] is False
    assert reply["error_info"] == {"kind": "timeout"}
    assert (
        reply["error"]
        == "Read operation fleet_snapshot timed out; retry the read. No work was submitted."
    )
    assert reply["error"] == timeout_message("fleet_snapshot")
    assert request_seconds("fleet_snapshot") == 2
    assert 1.8 <= elapsed < 2.8


async def test_snapshot_answers_with_all_four_dispatch_slots_held(rpc: Any) -> None:
    import asyncio

    service, remote = rpc
    for _ in range(4):
        await service.slots.acquire()
    try:
        async with asyncio.timeout(1):
            answer = await remote.call("fleet_snapshot", {})
        assert answer["tasks"] == []
        assert service.slots.locked()
    finally:
        for _ in range(4):
            service.slots.release()


async def test_twenty_successes_add_no_request_log_line_but_failure_adds_one(rpc: Any) -> None:
    import logging

    service, remote = rpc
    path = remote.config_path.parent / "requests.log"
    handler = logging.FileHandler(path)
    service.log.addHandler(handler)
    try:
        for _ in range(20):
            await remote.call("fleet_snapshot", {})
        assert path.read_text() == ""
        reply = await _reply(remote, {"unexpected": "PRIVATE_PARAM"})
        assert reply["ok"] is False
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(lines) == 1
        assert lines[0]["operation"] == "fleet_snapshot"
        assert lines[0]["outcome"] == "error"
        assert "PRIVATE_PARAM" not in path.read_text()
    finally:
        service.log.removeHandler(handler)
        handler.close()


def _bus_receipt(service: Any, request_key: str = "ab-20261120-review-t84") -> Any:
    import sys

    from answerback_bridge.bus.config import BusConfig
    from answerback_bridge.bus.store import BusStore

    config = BusConfig.create(service.config_path, server_binary=sys.executable)
    ledger = BusStore(config.root / "ledger.sqlite")
    command = ledger.submit(
        request_id=request_key,
        peer_id="hc:t84",
        project="/example",
        message="PRIVATE_BUS_BODY",
        routing_digest="a" * 64,
    )
    task, _ = service.bridge.store.reserve(
        "hc:t84", "PRIVATE_TASK_BODY", "bus:" + command["command_id"]
    )
    return ledger, task


async def test_bus_receipt_title_is_read_from_ledger_by_command_id(rpc: Any) -> None:
    service, remote = rpc
    ledger, task = _bus_receipt(service)
    answer = await remote.call("fleet_snapshot", {})
    assert answer["tasks"][0]["id"] == task["id"]
    assert answer["tasks"][0]["title"] == "review"
    assert answer["tasks"][0]["request_key"] == task["request_key"]
    assert "PRIVATE_BUS_BODY" not in encode(answer).decode()
    assert ledger.get(task["request_key"][4:])["task_id"] is None


@pytest.mark.parametrize("ledger_state", ["no_bus", "no_command", "unreadable", "missing"])
async def test_missing_bus_titles_are_null_and_unreadable_ledger_has_one_error(
    rpc: Any,
    ledger_state: str,
) -> None:
    service, remote = rpc
    if ledger_state == "no_bus":
        service.bridge.store.reserve("hc:t84", "private", f"bus:{IDENT}")
    else:
        ledger, task = _bus_receipt(service)
        if ledger_state == "no_command":
            with ledger.connection() as db:
                db.execute("DELETE FROM commands")
        elif ledger_state == "missing":
            ledger.path.unlink()
        else:
            ledger.path.write_bytes(b"unreadable PRIVATE_EXCEPTION")
        service.bridge.store.reserve("hc:t85", "private", f"bus:{IDENT}")
    answer = await remote.call("fleet_snapshot", {})
    assert all(row["title"] is None for row in answer["tasks"])
    assert answer["errors"] == (
        [{"section": "titles", "error": "bus ledger read failed"}]
        if ledger_state in {"unreadable", "missing"}
        else []
    )
    assert "PRIVATE_EXCEPTION" not in encode(answer).decode()
    if ledger_state == "missing":
        assert not ledger.path.exists()


async def test_fifty_calls_leave_store_files_and_service_state_unchanged(rpc: Any) -> None:
    service, remote = rpc
    _bus_receipt(service)
    root = remote.config_path.parent
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    state_before = dict(service.bridge.bus_state)
    for _ in range(50):
        await remote.call("fleet_snapshot", {})
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert service.bridge.bus_state == state_before


async def test_missing_receipt_store_fails_without_recreating_a_file(rpc: Any) -> None:
    service, remote = rpc
    service.bridge.store.path.unlink()
    reply = await _reply(remote)
    assert reply["ok"] is False
    assert reply["error_info"] == {"kind": "internal"}
    assert not service.bridge.store.path.exists()


async def test_fleet_snapshot_is_rpc_only_with_no_mcp_tool(rpc: Any) -> None:
    from mcp import Client

    from answerback_bridge.mcp_server import create_server

    service, remote = rpc
    assert "fleet_snapshot" in service.bridge.METHODS
    async with Client(create_server(remote)) as client:
        assert "fleet_snapshot" not in {tool.name for tool in (await client.list_tools()).tools}
    reply = await _reply(remote, {"limit": 5})
    assert reply["ok"] is False


async def test_snapshot_health_reports_not_accepting_while_service_drains(rpc: Any) -> None:
    service, remote = rpc
    service.stopping.set()
    answer = await remote.call("fleet_snapshot", {})
    assert answer["health"]["running"] is True
    assert answer["health"]["accepting"] is False


@pytest.mark.parametrize("mask_stat_errors", [False, True])
async def test_inaccessible_bus_configuration_is_an_error_not_an_absent_bus(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
    mask_stat_errors: bool,
) -> None:
    import os

    service, remote = rpc
    _bus_receipt(service)
    config_path = str(service.config_path.parent / "bus/config.json")
    original = os.stat

    def denied(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == config_path:
            raise PermissionError("PRIVATE_EXCEPTION")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    if mask_stat_errors:
        # Path.exists suppresses all OSErrors on newer supported Python versions.
        original_exists = Path.exists

        def exists(path: Path) -> bool:
            return False if str(path) == config_path else original_exists(path)

        monkeypatch.setattr(Path, "exists", exists)
    answer = await remote.call("fleet_snapshot", {})
    assert answer["tasks"][0]["title"] is None
    assert answer["errors"] == [{"section": "titles", "error": "bus ledger read failed"}]


def test_original_store_connection_and_counts_signatures_are_unchanged() -> None:
    import inspect

    assert set(inspect.signature(TaskStore.connection).parameters) == {"self"}
    assert set(inspect.signature(TaskStore.counts).parameters) == {"self"}


async def test_one_snapshot_opens_exactly_one_receipt_connection(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, remote = rpc
    original = sqlite3.connect
    opened: list[str] = []

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(database))
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    await remote.call("fleet_snapshot", {})
    assert len(opened) == 1
    assert opened[0] == service.bridge.store.path.as_uri() + "?mode=ro"


async def test_locked_store_times_out_before_two_seconds_and_call_thread_ends_by_two_point_five(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    service, remote = rpc
    original = sqlite3.connect
    workers: set[threading.Thread] = set()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-budget-test")
    loop.set_default_executor(executor)

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        workers.add(threading.current_thread())
        return original(database, *args, **kwargs)

    with original(service.bridge.store.path) as writer:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        writer.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(sqlite3, "connect", connect)
        started = time.monotonic()
        try:
            reply = await _reply(remote)
            elapsed = time.monotonic() - started
            # Retire this test's executor. A worker still doing the old three reads
            # cannot exit until the held lock is released, even after shutdown(False).
            executor.shutdown(wait=False)
            await asyncio.sleep(max(0, started + 2.5 - time.monotonic()))
            alive = [worker.name for worker in workers if worker.is_alive()]
        finally:
            writer.rollback()
            executor.shutdown(wait=True)
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    assert reply["error_info"] == {"kind": "timeout"}
    assert elapsed < 2, elapsed
    assert workers and not alive, alive
    assert (await remote.call("fleet_snapshot", {}))["errors"] == []


async def test_three_hundred_snapshots_stay_consistent_during_concurrent_writes(rpc: Any) -> None:
    import asyncio
    import threading
    from contextlib import closing

    from answerback_bridge.transport import TransportUnavailable

    service, remote = rpc
    _seed(service.bridge.store, 12)
    stop = threading.Event()
    failures: list[Exception] = []
    changes: list[int] = []

    def churn() -> None:
        try:
            with closing(sqlite3.connect(service.bridge.store.path, timeout=0.02)) as db:
                while not stop.is_set():
                    locked_at = time.monotonic()
                    try:
                        db.execute("BEGIN EXCLUSIVE")
                        db.execute(
                            "UPDATE tasks SET status=CASE status WHEN 'uncertain' "
                            "THEN 'completed' ELSE 'uncertain' END"
                        )
                        time.sleep(0.003)
                        db.commit()
                    except sqlite3.OperationalError as error:
                        db.rollback()
                        if error.sqlite_errorcode & 0xFF not in (
                            sqlite3.SQLITE_BUSY,
                            sqlite3.SQLITE_LOCKED,
                        ):
                            raise
                        continue
                    held = time.monotonic() - locked_at
                    changes.append(1)
                    stop.wait(held)
        except Exception as error:
            failures.append(error)

    writer = threading.Thread(target=churn)
    good = calls = 0
    finished = False
    started = time.monotonic()

    def diagnostic() -> str:
        return (
            f"calls={calls}, good={good}, commits={len(changes)}, "
            f"seconds={time.monotonic() - started:.3f}"
        )

    writer.start()
    try:
        async with asyncio.timeout(20):
            while calls < 300 or len(changes) < 20:
                calls += 1
                try:
                    answer = await remote.call("fleet_snapshot", {})
                except TransportUnavailable:
                    continue
                if not answer["errors"]:
                    good += 1
                    counts = answer["counts"]
                    assert answer["needs_person_total"] == (
                        counts["uncertain"] + counts["blocked"]
                    ), diagnostic()
                    assert counts["total"] == sum(counts[state] for state in STATES) == 12, (
                        diagnostic()
                    )
        finished = True
    except TimeoutError:
        pass
    finally:
        stop.set()
        writer.join(timeout=2)
    elapsed = time.monotonic() - started
    assert not writer.is_alive() and not failures, f"{diagnostic()}, failures={failures}"
    assert finished and elapsed < 20, diagnostic()
    assert calls >= 300 and len(changes) >= 20, diagnostic()
    assert good * 6 >= calls * 5, diagnostic()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_ERROR])
async def test_lock_failures_are_classified_by_sqlite_code_not_text(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    _, remote = rpc

    class Fault(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Give contradictory text to make code-based classification observable."""
            if "GROUP BY status" in sql:
                error = sqlite3.OperationalError(
                    "database is locked" if code == sqlite3.SQLITE_ERROR else "PRIVATE_EXCEPTION"
                )
                error.sqlite_errorcode = code
                raise error
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Fault)
    reply = await _reply(remote)
    if code == sqlite3.SQLITE_ERROR:
        assert reply["ok"] is True
        assert reply["result"]["counts"] is None
        assert reply["result"]["errors"] == [{"section": "counts", "error": "store read failed"}]
    else:
        assert reply["ok"] is False
        assert reply["error_info"] == {"kind": "timeout"}


async def test_busy_ledger_is_only_a_titles_error_with_half_second_budget(rpc: Any) -> None:
    service, remote = rpc
    ledger, _ = _bus_receipt(service)
    with sqlite3.connect(ledger.path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            answer = await remote.call("fleet_snapshot", {})
        finally:
            writer.rollback()
    assert time.monotonic() - started < 0.9
    assert answer["errors"] == [{"section": "titles", "error": "bus ledger read failed"}]
    assert answer["tasks"][0]["title"] is None
    assert (await remote.call("fleet_snapshot", {}))["tasks"][0]["title"] == "review"


async def test_store_and_ledger_share_the_remaining_read_budget(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, remote = rpc
    _bus_receipt(service)
    original_read = service.bridge.store.snapshot_read
    original_connect = sqlite3.connect
    timeouts: dict[str, float] = {}

    def delayed(**kwargs: Any) -> dict[str, Any]:
        time.sleep(1.1)
        return original_read(**kwargs)

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        timeouts[str(database)] = kwargs["timeout"]
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(service.bridge.store, "snapshot_read", delayed)
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert not (await remote.call("fleet_snapshot", {}))["errors"]
    receipt_timeout = next(v for k, v in timeouts.items() if "tasks.sqlite" in k)
    ledger_timeout = next(v for k, v in timeouts.items() if "ledger.sqlite" in k)
    assert 0.05 <= ledger_timeout <= receipt_timeout < 0.45


def test_expensive_sql_is_interrupted_at_the_shared_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")

    class Expensive(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Run a real long SQLite query to exercise the progress handler."""
            if "GROUP BY status" in sql:
                sql = (
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n "
                    "WHERE x<100000000) SELECT sum(x) FROM n"
                )
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Expensive)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        store.snapshot_read(deadline=started + 0.05)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("field", ["created", "updated"])
@pytest.mark.parametrize(
    "value",
    ["PRIVATE_TIMESTAMP", True, False, None, float("inf"), float("-inf"), float("nan"), 10**400],
)
def test_non_numeric_or_non_finite_timestamps_become_null(field: str, value: Any) -> None:
    raw = {
        "id": "example",
        "request_key": "review",
        "target": "hc:t84",
        "status": "uncertain",
        "created": 1.0,
        "updated": 2.0,
        field: value,
    }
    assert _module().row(raw, {})[field] is None


@pytest.mark.parametrize(("count", "size"), [(200, 2000), (1, 300 * 1024)])
async def test_text_timestamps_cannot_leak_or_hang_the_snapshot(
    rpc: Any,
    count: int,
    size: int,
) -> None:
    import asyncio

    service, remote = rpc
    _seed(service.bridge.store, count)
    marker = "PRIVATE_TIMESTAMP_" + "x" * size
    with service.bridge.store.connection() as db:
        db.execute("UPDATE tasks SET created=?,updated=?", (marker, marker))
    # Fail safely on the old row shaper before entering its non-terminating loop.
    raw = service.bridge.store.snapshot_read(deadline=time.monotonic() + 1.5)
    assert _module().row(raw["recent"][0], {})["created"] is None
    started = time.monotonic()
    async with asyncio.timeout(1):
        answer = await remote.call("fleet_snapshot", {})
    assert time.monotonic() - started < 1
    assert "PRIVATE_TIMESTAMP" not in encode(answer).decode()
    assert all(row["created"] is row["updated"] is None for row in answer["tasks"])


@pytest.mark.parametrize("has_display_text", [False, True])
def test_byte_shrinking_stops_when_stuck_and_after_at_most_eight_passes(
    monkeypatch: pytest.MonkeyPatch,
    has_display_text: bool,
) -> None:
    module = _module()
    raw = [
        {
            "id": "😀" * 63 + chr(0x400 + i),
            "request_key": "x" * 160 if has_display_text else "",
            "target": "y" * 160 if has_display_text else "",
            "status": "😀" * 64,
            "created": 1.0,
            "updated": 2.0,
        }
        for i in range(200)
    ]
    original = json.dumps
    calls = 0

    def counted(value: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        assert calls <= 9, "byte budget loop did not stop after eight passes"
        return original(value, **kwargs)

    started = time.monotonic()
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "json", SimpleNamespace(dumps=counted))
        assert json.dumps is original
        try:
            answer = module.build(
                read_at=1.0,
                counts={"uncertain": 200},
                needs_person=(raw, 200),
                recent=raw[:20],
                bus_keys={},
                errors=[],
            )
        except ValueError as error:
            assert str(error) == "store read failed"
        else:
            assert len(original(answer, ensure_ascii=False).encode()) < 256 * 1024
    assert time.monotonic() - started < 1
    if not has_display_text:
        assert calls <= 2


@pytest.mark.parametrize("missing_token", [False, True])
async def test_bus_title_reads_neither_configuration_content_nor_token(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
    missing_token: bool,
) -> None:
    service, remote = rpc
    _bus_receipt(service)
    bus = service.config_path.parent / "bus"
    if missing_token:
        (bus / "token").unlink()
    import builtins
    import os
    from collections.abc import Callable

    attempted: list[str] = []

    def protect(original: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, str | bytes | os.PathLike):
                path = Path(os.fsdecode(file))
                if path in (bus / "config.json", bus / "token"):
                    attempted.append(path.name)
                    raise PermissionError("PRIVATE_FILE_CONTENT")
            return original(file, *args, **kwargs)

        return guarded

    monkeypatch.setattr(Path, "open", protect(Path.open))
    monkeypatch.setattr(builtins, "open", protect(builtins.open))
    monkeypatch.setattr(os, "open", protect(os.open))
    answer = await remote.call("fleet_snapshot", {})
    assert answer["errors"] == []
    assert answer["tasks"][0]["title"] == "review"
    assert attempted == []


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("Observed Rex terminal binding changed", "recipient_binding_changed"),
        ("Rex configuration changed", "recipient_configuration_changed"),
        ("Rex terminal confirmed absent", "recipient_absent"),
        ("Rex endpoint mismatch", "recipient_binding_changed"),
        ("Rex expected host mismatch", "recipient_binding_changed"),
        ("Rex server host changed", "recipient_binding_changed"),
        ("Rex session confirmed absent", "recipient_absent"),
        ("Rex terminal confirmed absent from its session", "recipient_absent"),
        ("Rex block identity mismatch", "recipient_binding_changed"),
        ("Rex block is not a terminal", "recipient_binding_changed"),
        (
            "Crew chat identity changed or its identity evidence is incomplete",
            "recipient_binding_changed",
        ),
        ("Crew delivery receipt named a different chat", "delivery_identity_mismatch"),
        ("Core returned a different terminal address", "recipient_binding_changed"),
        (
            "BridgeError: Unknown native workspace; register a named project first",
            "native_execution_unverified",
        ),
        ("BridgeError: Saved native session ownership is invalid", "native_execution_unverified"),
        (
            "BridgeError: Native session ownership or configuration profile changed",
            "native_execution_unverified",
        ),
        ("BridgeError: Native progress has an unexpected session", "native_execution_unverified"),
        ("BridgeError: Native routing changed before execution", "recipient_configuration_changed"),
        (
            "BridgeError: Native turn completed but its final response is unavailable",
            "native_result_unavailable",
        ),
        ("BridgeError: Native response exceeds its supported bound", "native_execution_unverified"),
        ("BridgeError: Native session identity is invalid", "native_execution_unverified"),
        (
            "BridgeError: Native resume changed the owned session identity",
            "native_execution_unverified",
        ),
        ("BridgeError: Native terminal event has the wrong session", "native_execution_unverified"),
        ("BridgeError: Native terminal event has an invalid status", "native_execution_unverified"),
        (
            "BridgeError: Kiro workspace changed; register a new alias",
            "recipient_configuration_changed",
        ),
        (
            "BridgeError: Permission request exceeds the supported bounds",
            "permission_request_too_large",
        ),
        ("grok confirmed cancelled", "cancel_confirmed"),
        ("claude_code confirmed cancelled", "cancel_confirmed"),
        ("grok confirmed failed", "native_failure_confirmed"),
    ],
)
def test_additional_fixed_writer_details_have_codes(detail: str, expected: str) -> None:
    assert _reason("uncertain", detail) == (expected, None)


@pytest.mark.parametrize(
    "detail",
    [
        "ValueError: PRIVATE_EXCEPTION",
        "Kiro stopped: max_tokens",
        "Delivery identity unverified: expected t84, observed t85. "
        "Submission may have occurred; inspect the target before taking action.",
        "two words confirmed cancelled",
        "x" * 65 + " confirmed cancelled",
        "grok confirmed terminated",
    ],
)
def test_unbounded_writer_details_stay_unclassified(detail: str) -> None:
    assert _reason("uncertain", detail) == ("unclassified", None)


@pytest.mark.parametrize(
    ("detail", "code"),
    [
        (
            "Crew delivery is uncertain: Crew returned an unsupported delivery receipt. "
            "Do not resubmit blindly.",
            "delivery_unverified",
        ),
    ],
)
def test_fixed_details_composed_across_local_helpers_have_codes(detail: str, code: str) -> None:
    assert _reason("uncertain", detail) == (code, None)


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
async def test_busy_receipt_connection_open_is_a_retryable_timeout(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    _, remote = rpc

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        error = sqlite3.OperationalError("PRIVATE_EXCEPTION")
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert (await _reply(remote))["error_info"] == {"kind": "timeout"}


def test_reason_guard_accepts_an_additional_copy_of_a_recorded_sentence(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src/answerback_bridge/providers/codex/adapter.py"
    )
    changed = tmp_path / "codex.py"
    changed.write_text(source.read_text() + '\nEXTRA_DETAIL = "Submission was interrupted"\n')
    _assert_constants(changed, ("Submission was interrupted",), (2,))


def test_reason_guard_does_not_check_a_recorded_zero(tmp_path: Path) -> None:
    source = tmp_path / "writer.py"
    source.write_text('EXTRA_DETAIL = ": "\n')
    _assert_constants(source, (": ",), (0,))


@pytest.mark.parametrize(
    ("detail", "expected", "cause"),
    [
        ("Remote Core terminals support reading only", "remote_read_only", None),
        (
            "Remote Core terminals support reading only unless their machine is allow-listed",
            "remote_read_only",
            None,
        ),
        (
            "Core terminal needs a recognized foreground command, known directory, "
            "and reported idle",
            "recipient_not_ready",
            "recipient_not_ready",
        ),
        (
            "Core terminal needs a recognized foreground command, known directory, "
            "and reported idle or done",
            "recipient_not_ready",
            "recipient_not_ready",
        ),
        (
            "Crew delivery is uncertain: Crew request timed out. Do not resubmit blindly.",
            "delivery_unverified",
            None,
        ),
        (
            "Crew delivery is uncertain: Crew returned an unsupported delivery receipt. "
            "Do not resubmit blindly.",
            "delivery_unverified",
            None,
        ),
    ],
)
def test_fixed_beginnings_keep_their_codes_for_all_supported_forms(
    detail: str,
    expected: str,
    cause: str | None,
) -> None:
    assert _reason("uncertain", detail) == (expected, cause)
    assert _reason("uncertain", "Quoted later: " + detail) == ("unclassified", None)


def test_only_seven_rules_match_by_their_beginning() -> None:
    rules = [rule for rule in _rules() if rule.begins]
    assert {(rule.code, rule.parts) for rule in rules} == {
        ("remote_read_only", ("Remote Core terminals support reading only",)),
        (
            "recipient_not_ready",
            (
                "Core terminal needs a recognized foreground command, known "
                "directory, and reported ",
            ),
        ),
        ("delivery_unverified", ("Crew delivery is uncertain: ",)),
        ("kiro_deadline_exceeded", ("Kiro task exceeded the ",)),
        ("codex_cli_deadline_exceeded", ("codex_cli task exceeded the ",)),
        ("claude_code_deadline_exceeded", ("claude_code task exceeded the ",)),
        ("grok_deadline_exceeded", ("grok task exceeded the ",)),
    }


def test_a_beginning_rule_requires_exactly_one_part() -> None:
    with pytest.raises(ValueError, match="exactly one part"):
        _module().ReasonRule("example", ("first", "second"), {}, begins=True)


@pytest.mark.parametrize("code", ["remote_read_only", "recipient_not_ready", "delivery_unverified"])
def test_beginning_guard_accepts_extensions_and_rejects_rewording(
    tmp_path: Path, code: str
) -> None:
    source_name = (
        "providers/crew/client.py" if code == "delivery_unverified" else "providers/herdr/core.py"
    )
    filename = Path(source_name).name
    rule = next(r for r in _rules() if r.code == code and source_name in r.sources)
    source = Path(__file__).resolve().parents[1] / "src/answerback_bridge" / source_name
    changed = tmp_path / filename
    text = source.read_text()
    if code == "remote_read_only":
        text = text.replace(
            "Remote Core terminals support reading only",
            "Remote Core terminals support reading only unless their machine is allow-listed",
            1,
        )
    elif code == "recipient_not_ready":
        text = text.replace('"and reported idle"', '"and reported " + "idle or done"', 1)
    changed.write_text(text)
    _assert_constants(changed, rule.parts, rule.sources[source_name], begins=rule.begins)
    beginning = (
        "Crew delivery is uncertain: "
        if source_name == "providers/crew/client.py"
        else ("Remote Core" if code == "remote_read_only" else "Core terminal needs")
    )
    changed.write_text(text.replace(beginning, "Changed beginning", 1))
    with pytest.raises(AssertionError, match="expected at least .*found 0"):
        _assert_constants(changed, rule.parts, rule.sources[source_name], begins=rule.begins)


async def test_ledger_is_not_opened_with_less_than_fifty_milliseconds_left(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import answerback_bridge.service as service_module

    service, remote = rpc
    _bus_receipt(service)
    now = time.monotonic()
    clock_values = iter((now, now + 1.46))
    opened: list[str] = []
    original = sqlite3.connect

    def clock() -> float:
        return next(clock_values, now + 1.46)

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(database))
        return original(database, *args, **kwargs)

    monkeypatch.setattr(service_module, "time", SimpleNamespace(time=time.time, monotonic=clock))
    monkeypatch.setattr(sqlite3, "connect", connect)
    answer = await remote.call("fleet_snapshot", {})
    assert answer["tasks"][0]["title"] is None
    assert answer["errors"] == [{"section": "titles", "error": "bus ledger read failed"}]
    assert not any("ledger.sqlite" in path for path in opened)


def test_shrink_cap_allows_exactly_nine_serializations_when_text_limits_grow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    raw = {
        "id": "example",
        "request_key": "review",
        "target": "x" * 100_000,
        "status": "uncertain",
        "created": 1.0,
        "updated": 2.0,
    }
    original = json.dumps
    calls = 0

    def unbounded(value: str, limit: int) -> str:
        return value

    def counted(value: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return original(value, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "clean", unbounded)
        scoped.setattr(module, "MAX_BYTES", 100)
        scoped.setattr(module, "json", SimpleNamespace(dumps=counted))
        assert json.dumps is original
        with pytest.raises(ValueError, match="^store read failed$"):
            module.build(
                read_at=1.0,
                counts={"uncertain": 1},
                needs_person=([raw], 1),
                recent=[],
                bus_keys={},
                errors=[],
            )
    assert calls == 9


@pytest.mark.parametrize("through_bus", [False, True])
def test_title_inspects_only_first_512_key_characters(
    monkeypatch: pytest.MonkeyPatch,
    through_bus: bool,
) -> None:
    module = _module()
    original = module.clean
    inspected: list[int] = []
    key = "x" * 100_000

    def checked(value: str, limit: int) -> str:
        inspected.append(len(value))
        assert len(value) <= 512, "title inspected more than 512 key characters"
        return original(value, limit)

    monkeypatch.setattr(module, "clean", checked)
    answer = module.title(f"bus:{IDENT}" if through_bus else key, "hc:t84", {IDENT: key})
    assert answer == "x" * 80
    assert inspected and max(inspected) <= 512


async def test_interrupt_in_recent_query_returns_the_rpc_timeout_answer(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, remote = rpc
    _seed(service.bridge.store, 1)
    original = service.bridge.store.snapshot_read
    recent_started = False

    def short_read(*, deadline: float) -> dict[str, Any]:
        return original(deadline=min(deadline, time.monotonic() + 0.1))

    class ExpensiveRecent(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Trigger a real SQLite interrupt in the last section's query."""
            nonlocal recent_started
            if "ORDER BY created" in sql:
                recent_started = True
                sql = (
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n "
                    "WHERE x<100000000) SELECT sum(x) FROM n"
                )
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(service.bridge.store, "snapshot_read", short_read)
    _connect_as(monkeypatch, ExpensiveRecent)
    reply = await _reply(remote)
    assert recent_started
    assert reply["ok"] is False
    assert reply["error_info"] == {"kind": "timeout"}


async def test_slow_ledger_query_is_interrupted_at_its_own_deadline(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, remote = rpc
    _bus_receipt(service)
    ledger_started = False

    class SlowLedger(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Run real SQL, bounded independently so a missing handler cannot strand a thread."""
            nonlocal ledger_started
            if "SELECT id,request_key FROM commands" in sql:
                ledger_started = True
                stop_at = time.monotonic() + 1.3

                def keep_working() -> bool:
                    return time.monotonic() < stop_at

                self.create_function("keep_working", 0, keep_working)
                return super().execute(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n "
                    "WHERE keep_working()) SELECT max(x), 'unused' FROM n"
                )
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, SlowLedger)
    started = time.monotonic()
    answer = await remote.call("fleet_snapshot", {})
    assert time.monotonic() - started < 2
    assert ledger_started
    assert answer["tasks"][0]["title"] is None
    assert answer["errors"] == [{"section": "titles", "error": "bus ledger read failed"}]


def test_receipt_lock_is_held_before_all_four_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import closing
    from types import SimpleNamespace

    store = TaskStore(tmp_path / "tasks.sqlite")
    _seed(store, 12)
    refused: list[bool] = []
    lock_statements: list[str] = []
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=time.time)
    monkeypatch.setattr("answerback_bridge.tasks.time", clock)
    with closing(sqlite3.connect(store.path, timeout=0, isolation_level=None)) as writer:

        class Guarded(sqlite3.Connection):
            def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
                """Try a real conflicting write immediately before each receipt query."""
                if sql.startswith("PRAGMA busy_timeout=") or sql == "PRAGMA schema_version":
                    lock_statements.append(sql)
                if sql.startswith("SELECT") and "FROM tasks" in sql:
                    try:
                        writer.execute("BEGIN EXCLUSIVE")
                    except sqlite3.OperationalError as error:
                        assert error.sqlite_errorcode & 0xFF in (
                            sqlite3.SQLITE_BUSY,
                            sqlite3.SQLITE_LOCKED,
                        )
                        refused.append(True)
                    else:
                        writer.execute(
                            "UPDATE tasks SET status=CASE status WHEN 'uncertain' "
                            "THEN 'completed' ELSE 'uncertain' END"
                        )
                        writer.execute("COMMIT")
                        refused.append(False)
                return super().execute(sql, *args, **kwargs)

        _connect_as(monkeypatch, Guarded)
        answer = store.snapshot_read(deadline=101.5)
    lock_index = lock_statements.index("PRAGMA schema_version")
    assert lock_index > 0
    assert lock_statements[lock_index - 1] == "PRAGMA busy_timeout=1500"
    assert refused == [True, True, True, True]
    assert answer["errors"] == []
    rows, total = answer["needs_person"]
    assert total == answer["counts"].get("uncertain", 0) + answer["counts"].get("blocked", 0)
    assert total == len(rows) == 12


def test_expired_receipt_budget_stops_before_lock_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    readings = iter([100.0])
    statements: list[str] = []

    def monotonic() -> float:
        return next(readings, 102.0)

    clock = SimpleNamespace(monotonic=monotonic, time=time.time)
    monkeypatch.setattr("answerback_bridge.tasks.time", clock)

    class Guarded(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """An expired budget must take precedence over an unreadable store header."""
            statements.append(sql)
            if sql == "PRAGMA schema_version":
                raise sqlite3.DatabaseError("unreadable store header")
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Guarded)
    with pytest.raises(TimeoutError):
        store.snapshot_read(deadline=101.5)
    assert "PRAGMA schema_version" not in statements
    assert not any(sql.startswith("SELECT") for sql in statements)


async def test_ledger_lock_is_held_before_the_commands_query(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import closing
    from types import SimpleNamespace

    service, _ = rpc
    ledger, _ = _bus_receipt(service)
    ledger_path = ledger.path.resolve()
    original = sqlite3.connect
    refused: list[bool] = []
    ledger_lock_statements: list[str] = []
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=time.time)
    monkeypatch.setattr("answerback_bridge.tasks.time", clock)
    monkeypatch.setattr("answerback_bridge.service.time", clock)

    class Guarded(sqlite3.Connection):
        database_path: Path | None = None

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Try to write the ledger immediately before its command query."""
            if self.database_path is None:
                self.database_path = Path(
                    super().execute("PRAGMA database_list").fetchone()[2]
                ).resolve()
            if self.database_path == ledger_path and (
                sql.startswith("PRAGMA busy_timeout=") or sql == "PRAGMA schema_version"
            ):
                ledger_lock_statements.append(sql)
            if "SELECT id,request_key FROM commands" in sql:
                with closing(original(ledger.path, timeout=0, isolation_level=None)) as writer:
                    try:
                        writer.execute("BEGIN EXCLUSIVE")
                    except sqlite3.OperationalError as error:
                        assert error.sqlite_errorcode & 0xFF in (
                            sqlite3.SQLITE_BUSY,
                            sqlite3.SQLITE_LOCKED,
                        )
                        refused.append(True)
                    else:
                        writer.rollback()
                        refused.append(False)
            return super().execute(sql, *args, **kwargs)

    _connect_as(monkeypatch, Guarded)
    answer = await service.bridge.fleet_snapshot()
    lock_index = ledger_lock_statements.index("PRAGMA schema_version")
    assert lock_index > 0
    assert ledger_lock_statements[lock_index - 1] == "PRAGMA busy_timeout=500"
    assert refused == [True]
    assert answer["errors"] == []
    assert answer["tasks"][0]["title"] == "review"


async def test_expired_ledger_deadline_after_lock_skips_commands_query(
    rpc: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = rpc
    ledger, task = _bus_receipt(service)
    service.bridge.store.update(task["id"], "uncertain")
    ledger_path = ledger.path.resolve()
    now = 100.0
    commands_queried = False

    def monotonic() -> float:
        return now

    clock = SimpleNamespace(monotonic=monotonic, time=time.time)
    monkeypatch.setattr("answerback_bridge.tasks.time", clock)
    monkeypatch.setattr("answerback_bridge.service.time", clock)

    class Guarded(sqlite3.Connection):
        database_path: Path | None = None

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            """Spend the ledger's budget only after its lock statement has run."""
            nonlocal now, commands_queried
            if self.database_path is None:
                self.database_path = Path(
                    super().execute("PRAGMA database_list").fetchone()[2]
                ).resolve()
            is_ledger = self.database_path == ledger_path
            if is_ledger and "SELECT id,request_key FROM commands" in sql:
                commands_queried = True
            cursor = super().execute(sql, *args, **kwargs)
            if is_ledger and sql == "PRAGMA schema_version":
                now = 100.6
            return cursor

    _connect_as(monkeypatch, Guarded)
    answer = await service.bridge.fleet_snapshot()
    assert now == 100.6
    assert not commands_queried
    assert answer["errors"] == [{"section": "titles", "error": "bus ledger read failed"}]
    assert answer["counts"] == {
        **{state: int(state == "uncertain") for state in STATES},
        "total": 1,
    }
    assert answer["needs_person_total"] == 1
    assert answer["needs_person_omitted"] == 0
    assert answer["needs_person"] == answer["recent"] == answer["tasks"]
    assert [row["id"] for row in answer["tasks"]] == [task["id"]]
    assert answer["tasks"][0]["title"] is None


def test_schema_version_pins_without_parsing_a_broken_schema(tmp_path: Path) -> None:
    """The pin must not parse the schema, which could require a second lock wait.

    Revisit the snapshot deadline if a SQLite upgrade changes this behavior.
    """
    from contextlib import closing

    path = tmp_path / "broken.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        db.execute("PRAGMA writable_schema=ON")
        db.execute("UPDATE sqlite_master SET sql='CREATE TABLE tasks (this is not a definition'")
        db.commit()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
        db.execute("BEGIN")
        version = db.execute("PRAGMA schema_version").fetchone()
        assert version is not None and isinstance(version[0], int)
        with pytest.raises(sqlite3.DatabaseError, match="schema"):
            db.execute("SELECT COUNT(*) FROM tasks").fetchall()


@pytest.mark.parametrize("field", ["id", "request_key", "target", "status"])
def test_row_cuts_text_before_searching_past_control_characters(field: str) -> None:
    raw = {
        "id": "example",
        "request_key": "review-docs-t12",
        "target": "hc:t12",
        "status": "uncertain",
        "created": 1.0,
        "updated": 2.0,
    }
    raw[field] = "\x00" * 600 + "abc"
    assert _module().row(raw, {})[field] == ""


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("hc:" + ":" * 600 + "t12", "review-docs-t12"),
        ("hc:t12", "review-docs"),
    ],
)
def test_title_only_uses_the_first_512_target_characters(target: str, expected: str) -> None:
    assert _module().title("review-docs-t12", target, {}) == expected


def test_row_hands_the_bounded_request_key_to_title() -> None:
    command_id = "x" * 600
    raw = {
        "id": "example",
        "request_key": "bus:" + command_id,
        "target": "hc:t12",
        "status": "uncertain",
        "created": 1.0,
        "updated": 2.0,
    }
    assert _module().row(raw, {command_id: "outside-the-bound"})["title"] is None


def test_reason_guard_message_names_the_missing_fragment(tmp_path: Path) -> None:
    source = tmp_path / "writer.py"
    source.write_text("EXAMPLE = 'other detail'\n")
    with pytest.raises(AssertionError) as error:
        _assert_constants(source, ("fixed detail",), (1,))
    assert (
        str(error.value).splitlines()[0]
        == "writer.py: expected at least 1 of 'fixed detail', found 0"
    )
