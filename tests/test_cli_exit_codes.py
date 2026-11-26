"""The CLI exit code must tell a completed task apart from a wait that merely
expired and from a task still blocked on a permission request.

Contract (docs/NATIVE-CLI.md "Exit codes"): 0 only for a completed task; 1 for a
failed, cancelled, abandoned, uncertain or ambiguous result or any error; 2 for a
task ``blocked`` on a pending permission; 3 for a ``task --wait`` whose own deadline
expired (``wait_timed_out``) before a terminal status; 130 for Ctrl-C.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.cli import EXIT_BLOCKED, EXIT_FAILURE, EXIT_WAIT_TIMED_OUT, main


def _run_task_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: dict[str, Any]) -> None:
    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "task_wait"
        assert params == {"task_id": "fixture-task", "seconds": 5}
        return result

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "answerback",
            "--config",
            str(tmp_path / "config.json"),
            "task",
            "fixture-task",
            "--wait",
            "5",
        ],
    )
    main()


def test_exit_code_constants_are_distinct_and_leave_sigint_alone() -> None:
    assert EXIT_FAILURE == 1
    assert len({0, EXIT_FAILURE, EXIT_BLOCKED, EXIT_WAIT_TIMED_OUT, 130}) == 5


def test_task_printer_includes_permission_receipt_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = {
        "id": "fixture-task",
        "status": "completed",
        "metadata": {
            "permission_events": [
                {
                    "permission_id": "permission-1",
                    "toolCallId": "call-1",
                    "option_id": "deny",
                    "requested_at": 1.0,
                    "answered_at": 2.0,
                    "ordinal": 1,
                }
            ]
        },
    }

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "task_get"
        assert params == {"task_id": "fixture-task"}
        return result

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    monkeypatch.setattr(
        sys,
        "argv",
        ["answerback", "--config", str(tmp_path / "config.json"), "task", "fixture-task"],
    )
    main()

    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"id": "fixture-task", "status": "running", "wait_timed_out": True}, EXIT_WAIT_TIMED_OUT),
        ({"id": "fixture-task", "status": "pending", "wait_timed_out": True}, EXIT_WAIT_TIMED_OUT),
        ({"id": "fixture-task", "status": "blocked"}, EXIT_BLOCKED),
        # A blocked task that also outlived the wait: the human-action state wins, because
        # the caller must answer the permission rather than simply wait longer.
        ({"id": "fixture-task", "status": "blocked", "wait_timed_out": True}, EXIT_BLOCKED),
        ({"id": "fixture-task", "status": "failed"}, EXIT_FAILURE),
        ({"id": "fixture-task", "status": "cancelled"}, EXIT_FAILURE),
        ({"id": "fixture-task", "status": "abandoned"}, EXIT_FAILURE),
        ({"id": "fixture-task", "status": "uncertain"}, EXIT_FAILURE),
    ],
    ids=[
        "wait_timed_out_running",
        "wait_timed_out_pending",
        "blocked",
        "blocked_and_timed_out",
        "failed",
        "cancelled",
        "abandoned",
        "uncertain",
    ],
)
def test_task_wait_exits_with_the_documented_code_for_a_non_completed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: dict[str, Any],
    code: int,
) -> None:
    with pytest.raises(SystemExit) as info:
        _run_task_wait(monkeypatch, tmp_path, result)
    assert info.value.code == code
    # The receipt (including wait_timed_out and the state) still reaches stdout, so a
    # script can inspect it instead of guessing from the exit code alone.
    assert json.loads(capsys.readouterr().out) == result


def test_task_wait_exits_zero_only_for_a_completed_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = {"id": "fixture-task", "status": "completed", "result": "done"}
    _run_task_wait(monkeypatch, tmp_path, result)  # must not raise SystemExit
    assert json.loads(capsys.readouterr().out) == result


def test_a_running_receipt_that_did_not_time_out_keeps_exiting_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # wait_timed_out absent (or false) means the wait did not expire: the exit code
    # must not read a plain non-terminal receipt as a timeout (`task TASK_ID` without
    # --wait returns such receipts today and exits 0).
    _run_task_wait(monkeypatch, tmp_path, {"id": "fixture-task", "status": "running"})
    _run_task_wait(
        monkeypatch, tmp_path, {"id": "fixture-task", "status": "running", "wait_timed_out": False}
    )
