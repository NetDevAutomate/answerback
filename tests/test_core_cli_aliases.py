"""Offline proposal tests; no Core commands, native clients or model calls."""

from typing import Any

import pytest

from answerback_bridge.providers.herdr.core import Core, CoreNotReady


def terminal(argv: list[str], app: str | None, state: str = "idle") -> dict[str, Any]:
    return {
        "address": "t1",
        "argv": argv,
        "pid": 100,
        "created_ms": 1794706375375,
        "cwd": "/work/project",
        "foreground": {
            "pid": 101,
            "name": "untrusted-display-name",
            "argv": argv,
            "cwd": "/work/project",
        },
        "state": "live",
        "status": None if app is None else {"app": app, "state": state},
        "labels": {},
    }


@pytest.mark.parametrize(
    ("argv", "app", "kind"),
    [
        (["claude"], "claude-code", "claude"),
        (["codex", "--no-daemon"], "codex", "codex"),
        (["kiro-cli"], "kiro-cli", "kiro"),
        (["kirocrew", "chat"], "kirocrew", "kirocrew"),
        (["grok"], "grok", "grok"),
    ],
)
def test_canonical_cli_identities_require_matching_idle_report(
    argv: list[str], app: str, kind: str
) -> None:
    peer = Core()._peer(terminal(argv, app), "t1")
    assert peer["agent_kind"] == kind
    assert peer["send_ready"] is True


@pytest.mark.parametrize(
    ("argv", "app", "kind"),
    [
        (["/tools/kiro"], "kiro-cli", "kiro"),
        (["/tools/grok-build"], "grok", "grok"),
    ],
)
def test_documented_cli_aliases_match_canonical_status(
    argv: list[str], app: str, kind: str
) -> None:
    peer = Core()._peer(terminal(argv, app), "t1")
    assert peer["agent_kind"] == kind
    assert peer["agent_identity_source"] == "foreground_argv"
    assert peer["send_ready"] is True


# `done` is ready by default (T-84); see test_core_send_policy.py.
@pytest.mark.parametrize("state", ["working", "blocked", "error", "unknown"])
def test_crew_non_idle_reports_never_authorize_send(state: str) -> None:
    peer = Core()._peer(terminal(["kirocrew", "chat"], "kirocrew", state), "t1")
    assert peer["send_ready"] is False
    assert peer["capabilities"] == ["read"]


def test_crew_prompt_without_native_report_is_read_only() -> None:
    row = terminal(["/tools/kirocrew", "chat"], None)
    row["foreground"]["name"] = "toolbox-exec"
    row["screen"] = "you> "
    peer = Core()._peer(row, "t1")
    assert peer["agent_kind"] == "kirocrew"
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "unreported"


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", "kirocrew"],
        ["sh", "-c", "kirocrew chat"],
        ["python3", "-m", "kirocrew_amazon", "chat"],
        ["toolbox-exec", "kirocrew", "chat"],
    ],
)
def test_incidental_names_do_not_create_foreground_identity(
    argv: list[str],
) -> None:
    try:
        peer = Core()._peer(terminal(argv, "kirocrew"), "t1")
    except CoreNotReady:
        return
    assert peer["send_ready"] is False
    assert peer["agent_identity_source"] == "reported_status"


def test_a_different_reported_client_does_not_make_crew_ready() -> None:
    peer = Core()._peer(terminal(["kirocrew", "chat"], "kiro-cli"), "t1")
    assert peer["send_ready"] is False
    assert peer["send_readiness_reason"] == "status_mismatch"
