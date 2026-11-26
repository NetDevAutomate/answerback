import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config


def harness_module() -> Any:
    assert importlib.util.find_spec("codex_herdr_bridge.harness") is not None, (
        "The built-in collaboration harness is missing"
    )
    from codex_herdr_bridge import harness

    return harness


def test_live_mode_kiro_instructions_are_immune_to_the_peers_working_directory(
    tmp_path: Path,
) -> None:
    import shlex

    from codex_herdr_bridge.process import module_launch_argv

    harness = harness_module()
    config_path = tmp_path / "config.json"
    run = harness.Run(config_path, "live", 60)
    instruction = run.message(
        "delegate",
        "nonce-1",
        "req-1",
        {"provider": "kiro", "peer_id": "kiro:worker"},
        "child-req",
    )
    lines = instruction.splitlines()
    send_command = shlex.split(lines[1])
    assert send_command[:8] == module_launch_argv(config_path, "send", "kiro:worker")
    wait_command = shlex.join(module_launch_argv(config_path, "task"))
    assert f"{wait_command} CHILD_TASK_ID --wait 2." in lines[2]


def _kiro_poll_line(tmp_path: Path) -> str:
    """The line of the live-mode kiro instruction that explains the `task --wait` poll."""
    harness = harness_module()
    run = harness.Run(tmp_path / "config.json", "live", 60)
    instruction = run.message(
        "delegate", "nonce-1", "req-1", {"provider": "kiro", "peer_id": "kiro:worker"}, "child-req"
    )
    return instruction.splitlines()[2]


def _exit_code_clauses(poll_line: str) -> dict[int, str]:
    """Map each exit code the poll sentence names (`N means ...`) to its own clause.

    A clause runs to the next `;` or `.`, so the action it prescribes cannot be
    satisfied by words that belong to a neighbouring code's clause.
    """
    return {int(code): clause for code, clause in re.findall(r"\b(\d+) means ([^;.]+)", poll_line)}


def test_live_mode_child_instruction_explains_the_task_wait_exit_codes(tmp_path: Path) -> None:
    from codex_herdr_bridge.exit_codes import EXIT_BLOCKED, EXIT_FAILURE, EXIT_WAIT_TIMED_OUT

    poll_line = _kiro_poll_line(tmp_path)
    assert "--wait 2." in poll_line
    assert "0 means the child completed" in poll_line
    assert f"{EXIT_WAIT_TIMED_OUT} means the wait ran out, run the same poll again" in poll_line
    assert f"{EXIT_BLOCKED} means a human permission is pending, stop" in poll_line
    # A failed child exits EXIT_FAILURE (cli.exit_code_for); the instruction must give
    # the peer an action for it, and that action is to stop, never to poll again.
    assert f"{EXIT_FAILURE} means the child failed" in poll_line
    failure_clause = _exit_code_clauses(poll_line)[EXIT_FAILURE]
    for outcome in ("failed", "cancelled", "abandoned", "uncertain", "ambiguous"):
        assert outcome in failure_clause
    assert "stop" in failure_clause
    assert "report the receipt" in failure_clause
    assert "poll again" not in failure_clause


def test_live_mode_child_instruction_names_every_task_wait_exit_code(tmp_path: Path) -> None:
    from codex_herdr_bridge import exit_codes

    # Every code `task --wait` can exit with: 0 plus each EXIT_* constant. A code added
    # to exit_codes.py that the instruction does not explain fails here.
    every_code = {0, *(v for name, v in vars(exit_codes).items() if name.startswith("EXIT_"))}
    assert every_code == {
        0,
        exit_codes.EXIT_FAILURE,
        exit_codes.EXIT_BLOCKED,
        exit_codes.EXIT_WAIT_TIMED_OUT,
    }
    named = _exit_code_clauses(_kiro_poll_line(tmp_path))
    assert set(named) == every_code
    assert all(clause.strip() for clause in named.values())


async def test_simulated_harness_crosses_both_transports_and_cleans_up(tmp_path: Path) -> None:
    harness = harness_module()
    missing = tmp_path / "not-configured.json"
    report = await harness.run_harness(missing, timeout=45)
    assert report["mode"] == "simulated"
    assert report["success"] is True, report
    phases = {phase["name"]: phase for phase in report["phases"]}
    for name in ("herdr_to_kiro", "kiro_to_herdr"):
        phase = phases[name]
        assert phase["status"] == "passed"
        assert phase["parent_task_id"] != phase["child_task_id"]
        assert phase["parent_status"] == phase["child_status"] == "completed"
        assert phase["nonce_match"] is True
    assert phases["idempotency"]["status"] == "passed"
    assert phases["idempotency"]["prompt_counts"] == {"herdr": 2, "acp": 2}
    assert phases["restart"]["receipts_verified"] == 4
    assert phases["restart"]["instance_changed"] is True
    assert phases["cleanup"]["service_stopped"] is True
    assert phases["cleanup"]["temporary_directory_removed"] is True
    assert not missing.exists()
    for pid in report["fixture_pids"]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert "live" in " ".join(report["limitations"]).lower()
    json.dumps(report)


async def test_simulated_harness_reports_nonce_failure_and_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = harness_module()
    prepare = harness._prepare_simulation

    def faulty(root: Path) -> Path:
        path = prepare(root)
        (root / "fault.json").write_text(json.dumps({"wrong_nonce": True}))
        return path

    monkeypatch.setattr(harness, "_prepare_simulation", faulty)
    report = await harness.run_harness(tmp_path / "absent.json", timeout=45)
    assert report["success"] is False
    phases = {phase["name"]: phase for phase in report["phases"]}
    assert phases["herdr_to_kiro"]["status"] == "failed"
    assert "nonce" in phases["herdr_to_kiro"]["detail"].lower()
    assert "kiro_to_herdr" not in phases
    assert phases["cleanup"]["service_stopped"] is True
    assert phases["cleanup"]["temporary_directory_removed"] is True


async def test_live_harness_requires_explicit_targets_without_reading_config(
    tmp_path: Path,
) -> None:
    harness = harness_module()
    report = await harness.run_harness(tmp_path / "missing.json", mode="live")
    assert report["success"] is False
    assert report["phases"][0]["name"] == "preflight"
    assert "explicit" in report["phases"][0]["detail"].lower()
    assert report["outstanding_task_ids"] == []


@pytest.mark.parametrize("problem", ["ambiguous", "wrong_kind", "busy", "partial"])
async def test_live_preflight_never_dispatches_when_targets_are_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    harness = harness_module()
    path = tmp_path / "config.json"
    Config("test", kiro_workspaces={"reviewer": str(tmp_path)}).save(path)
    calls: list[str] = []
    herdr = {
        "peer_id": "herdr:worker",
        "provider": "herdr",
        "target": "worker",
        "kind": "interactive",
        "state": "idle",
        "name": "worker",
    }
    kiro = {
        "peer_id": "kiro:reviewer",
        "provider": "kiro",
        "target": "reviewer",
        "kind": "bridge_managed",
        "name": "reviewer",
        "project": str(tmp_path),
    }
    if problem == "wrong_kind":
        kiro["kind"] = "interactive"
    if problem == "busy":
        herdr["state"] = "working"

    class Remote:
        def __init__(self, _: Path) -> None:
            pass

        async def status(self) -> dict[str, Any]:
            return {"running": True, "accepting": True}

        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(method)
            if method == "peer_search":
                peers = [dict(herdr)] if params["provider"] == "herdr" else [dict(kiro)]
                if problem == "ambiguous" and params["provider"] == "kiro":
                    peers.append({**kiro, "peer_id": "kiro:reviewer-two"})
                return {"peers": peers, "complete": problem != "partial", "errors": {}}
            if method == "task_list":
                return {"tasks": []}
            raise AssertionError(f"Unexpected operation: {method}")

    monkeypatch.setattr(harness, "RemoteBridge", Remote)
    report = await harness.run_harness(path, mode="live", herdr_peer="worker", kiro_peer="reviewer")
    assert report["success"] is False
    assert report["outstanding_task_ids"] == []
    assert "peer_submit" not in calls


async def test_wait_stops_immediately_when_nested_child_needs_permission(tmp_path: Path) -> None:
    harness = harness_module()
    check = harness.Run(tmp_path / "config.json", "live", 10)
    check.begin("herdr_to_kiro")
    parent = {
        "id": "parent",
        "status": "submitted",
        "metadata": {"kind": "herdr", "target": "worker"},
    }
    child = {
        "id": "child",
        "status": "blocked",
        "request_key": "child-request",
        "metadata": {"kind": "kiro", "alias": "reviewer"},
    }
    check.phase.update(parent_task_id="parent", child_request_id="child-request")

    class Remote:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "task_wait":
                return parent
            if method == "task_list":
                return {"tasks": [{key: child[key] for key in ("id", "status", "request_key")}]}
            if method == "task_get":
                return child
            if method == "peer_search":
                return {
                    "peers": [
                        {
                            "peer_id": "herdr:worker",
                            "provider": "herdr",
                            "target": "worker",
                            "kind": "interactive",
                            "state": "working",
                        }
                    ],
                    "complete": True,
                }
            raise AssertionError(method)

    check.remote = Remote()
    with pytest.raises(harness.HarnessFailure, match="blocked"):
        await check.wait(parent)
    assert check.phase["parent_status"] == "submitted"
    assert check.phase["child_status"] == "blocked"
    assert check.phase["child_task_id"] == "child"


async def test_simulated_deadline_still_stops_temporary_service(tmp_path: Path) -> None:
    harness = harness_module()
    report = await harness.run_harness(tmp_path / "unused.json", timeout=1)
    assert report["success"] is False
    cleanup = report["phases"][-1]
    assert cleanup["name"] == "cleanup"
    assert cleanup["service_stopped"] is True
    assert cleanup["temporary_directory_removed"] is True


async def test_wait_rejects_receipt_for_another_task(tmp_path: Path) -> None:
    harness = harness_module()
    check = harness.Run(tmp_path / "config.json", "live", 10)
    check.begin("herdr_to_kiro")
    check.phase["parent_task_id"] = "expected"

    class Remote:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "task_wait"
            return {"id": "another-task", "status": "completed", "result": "nonce"}

    check.remote = Remote()
    with pytest.raises(harness.HarnessFailure, match="different task"):
        await check.wait({"id": "expected", "status": "submitted"})


async def test_unverified_cleanup_cannot_report_harness_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    harness = harness_module()
    original = harness._stop_simulation

    async def unverified(remote: Any) -> bool:
        await original(remote)
        return False

    monkeypatch.setattr(harness, "_stop_simulation", unverified)
    report = await harness.run_harness(tmp_path / "missing.json", timeout=45)
    recovery = Path(report["phases"][-1]["recovery_config"]).parent
    try:
        assert report["success"] is False
        assert report["phases"][-1]["status"] == "failed"
    finally:
        assert recovery.name.startswith("hb-harness-")
        shutil.rmtree(recovery)


async def test_reverse_probe_waits_for_worker_to_finish_after_its_receipt(tmp_path: Path) -> None:
    harness = harness_module()
    check = harness.Run(tmp_path / "config.json", "live", 10)
    expected = {
        "peer_id": "herdr:worker",
        "provider": "herdr",
        "target": "worker",
        "kind": "interactive",
        "terminal_id": "original",
        "state": "working",
    }
    states = iter(["working", "idle"])
    calls = []

    class Remote:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(method)
            assert method == "peer_search"
            return {"complete": True, "peers": [{**expected, "state": next(states)}]}

    check.remote = Remote()
    ready = await check.settled_herdr(expected)
    assert ready["state"] == "idle"
    assert calls == ["peer_search", "peer_search"]


@pytest.mark.parametrize("change", ["blocked", "replacement"])
async def test_reverse_probe_does_not_wait_through_block_or_replacement(
    tmp_path: Path, change: str
) -> None:
    harness = harness_module()
    check = harness.Run(tmp_path / "config.json", "live", 10)
    expected = {
        "peer_id": "herdr:worker",
        "provider": "herdr",
        "target": "worker",
        "kind": "interactive",
        "terminal_id": "original",
        "state": "working",
    }

    class Remote:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "peer_search"
            current = {**expected, "state": "blocked" if change == "blocked" else "idle"}
            if change == "replacement":
                current["terminal_id"] = "different"
            return {"complete": True, "peers": [current]}

    check.remote = Remote()
    with pytest.raises(harness.HarnessFailure):
        await check.settled_herdr(expected)
