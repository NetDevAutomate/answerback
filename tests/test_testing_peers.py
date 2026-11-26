"""The scripted Herdr peer polls `task ID --wait 1` through the real CLI, so it is an
in-tree consumer of the CLI exit-code contract (docs/NATIVE-CLI.md "Exit codes"): a
timed-out wait is a receipt to poll again, a blocked task is a receipt that stops the
fixture, and any other non-zero exit is still a broken fixture.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.exit_codes import EXIT_BLOCKED, EXIT_FAILURE, EXIT_WAIT_TIMED_OUT
from answerback_bridge.testing import peers


def _fake_cli_run(returncode: int, receipt: dict[str, Any]) -> Any:
    def run(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, returncode, stdout=json.dumps(receipt), stderr="")

    return run


def test_wait_receipt_exits_are_exactly_the_two_not_yet_terminal_codes() -> None:
    assert peers.WAIT_RECEIPT_EXITS == {EXIT_WAIT_TIMED_OUT, EXIT_BLOCKED}


@pytest.mark.parametrize("code", [EXIT_WAIT_TIMED_OUT, EXIT_BLOCKED], ids=["timed_out", "blocked"])
def test_cli_returns_the_receipt_for_an_accepted_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    receipt = {"id": "child", "status": "running", "wait_timed_out": True}
    monkeypatch.setattr(peers.subprocess, "run", _fake_cli_run(code, receipt))
    assert (
        peers.cli(
            tmp_path,
            time.monotonic() + 30,
            "task",
            "child",
            "--wait",
            "1",
            accepted_exits=peers.WAIT_RECEIPT_EXITS,
        )
        == receipt
    )


@pytest.mark.parametrize("code", [EXIT_WAIT_TIMED_OUT, EXIT_BLOCKED, EXIT_FAILURE])
def test_cli_still_raises_on_any_non_zero_exit_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    monkeypatch.setattr(peers.subprocess, "run", _fake_cli_run(code, {"id": "child"}))
    with pytest.raises(RuntimeError, match="Fixture CLI operation failed"):
        peers.cli(tmp_path, time.monotonic() + 30, "task", "child")


def test_cli_raises_on_a_real_error_even_when_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(peers.subprocess, "run", _fake_cli_run(EXIT_FAILURE, {"error": "x"}))
    with pytest.raises(RuntimeError, match="Fixture CLI operation failed"):
        peers.cli(
            tmp_path,
            time.monotonic() + 30,
            "task",
            "child",
            "--wait",
            "1",
            accepted_exits=peers.WAIT_RECEIPT_EXITS,
        )


def test_check_receipt_stops_the_fixture_on_a_blocked_receipt_and_polls_a_running_one() -> None:
    peers.check_receipt({"id": "child", "status": "running", "wait_timed_out": True})  # no raise
    with pytest.raises(RuntimeError, match="blocked"):
        peers.check_receipt({"id": "child", "status": "blocked"})
