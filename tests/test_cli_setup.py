from pathlib import Path
from typing import Any

import pytest
from composition import contributed

from codex_herdr_bridge.cli import dispatch, parser
from codex_herdr_bridge.config import Config


@pytest.mark.parametrize("command", [["workspace", "add"], ["kiro-register"]])
async def test_workspace_paths_are_resolved_in_the_calling_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {}

    monkeypatch.setattr("codex_herdr_bridge.cli.RemoteBridge.call", call)
    args = parser().parse_args(
        ["--config", str(tmp_path / "config.json"), *command, "demo", "--cwd", "./demo"]
    )
    await dispatch(args)
    assert captured["cwd"] == str(tmp_path / "demo")


@pytest.mark.parametrize("command", [["workspace", "add"], ["kiro-register"], ["crew-configure"]])
async def test_relative_binary_path_is_resolved_before_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {}

    monkeypatch.setattr("codex_herdr_bridge.cli.RemoteBridge.call", call)
    extra = [] if command == ["crew-configure"] else ["demo", "--cwd", str(tmp_path)]
    args = parser().parse_args(
        ["--config", str(tmp_path / "config.json"), *command, *extra, "--binary", "./custom-agent"]
    )
    await dispatch(args)
    assert captured["binary"] == str(tmp_path / "custom-agent")


async def test_mcp_config_launch_is_immune_to_the_registering_clients_cwd(
    tmp_path: Path,
) -> None:
    from codex_herdr_bridge.process import module_launch_argv

    config_path = tmp_path / "config.json"
    args = parser().parse_args(["--config", str(config_path), "mcp-config"])
    entry = (await dispatch(args))["mcpServers"]["answerback_bridge"]
    assert [entry["command"], *entry["args"]] == module_launch_argv(config_path, "mcp")


async def test_mcp_config_keeps_the_client_identity_after_the_safe_launch(
    tmp_path: Path,
) -> None:
    from codex_herdr_bridge.process import module_launch_argv

    config_path = tmp_path / "config.json"
    args = parser().parse_args(["--config", str(config_path), "mcp-config", "--client", "codex"])
    entry = (await dispatch(args))["mcpServers"]["answerback_bridge"]
    assert [entry["command"], *entry["args"]] == module_launch_argv(
        config_path, "mcp", "--client", "codex"
    )


PENDING_REQUEST = {
    "id": "perm-reviewed",
    "tool_call": {"toolCallId": "call-1", "kind": "execute", "title": "rm -rf build/"},
    "options": [
        {"optionId": "allow", "kind": "allow_once", "name": "Allow once"},
        {"optionId": "reject", "kind": "reject_once", "name": "Reject once"},
    ],
}


def fake_bridge(
    monkeypatch: pytest.MonkeyPatch, pending: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Answer task_permissions with ``pending``; record every RPC the CLI makes."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "task_permissions":
            return {"task_id": params["task_id"], "permissions": pending}
        return {"task_id": params["task_id"], "answered": True}

    monkeypatch.setattr("codex_herdr_bridge.cli.RemoteBridge.call", call)
    return calls


def test_permission_answer_without_permission_id_is_a_usage_error(capsys: Any) -> None:
    """Even with exactly one request pending, the human must name the one they reviewed."""
    with pytest.raises(SystemExit) as refusal:
        parser().parse_args(["permission-answer", "task-1", "--decision", "allow-once"])
    assert refusal.value.code == 2
    assert "--permission-id" in capsys.readouterr().err


async def test_permission_answer_refuses_once_the_reviewed_request_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale id (the task moved on and a different request is pending) is never answered."""
    replacement = {**PENDING_REQUEST, "id": "perm-replacement"}
    calls = fake_bridge(monkeypatch, [replacement])
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "permission-answer",
            "task-1",
            "--permission-id",
            "perm-reviewed",
            "--decision",
            "allow-once",
        ]
    )
    with pytest.raises(ValueError, match="no longer pending.*`permissions`"):
        await dispatch(args)
    assert [method for method, _ in calls] == ["task_permissions"]


async def test_permission_answer_keeps_refusing_an_ambiguous_pending_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two pending items carrying the same id are not disambiguated by guessing."""
    calls = fake_bridge(monkeypatch, [PENDING_REQUEST, dict(PENDING_REQUEST)])
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "permission-answer",
            "task-1",
            "--permission-id",
            "perm-reviewed",
            "--decision",
            "allow-once",
        ]
    )
    with pytest.raises(ValueError, match="Select one pending permission with --permission-id"):
        await dispatch(args)
    assert [method for method, _ in calls] == ["task_permissions"]


async def test_permission_answer_approves_the_reviewed_request_and_echoes_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_bridge(monkeypatch, [PENDING_REQUEST])
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "permission-answer",
            "task-1",
            "--permission-id",
            "perm-reviewed",
            "--decision",
            "allow-once",
        ]
    )
    result = await dispatch(args)
    assert calls[-1] == (
        "permission_answer",
        {"task_id": "task-1", "permission_id": "perm-reviewed", "option_id": "allow"},
    )
    assert result["answered"] is True
    assert result["approved_tool_call"] == PENDING_REQUEST["tool_call"]


def test_config_help_text_reflects_the_live_resolved_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # default_config_path() prefers an existing legacy config over the new
    # default (config.py's deliberate migration order); the --config help
    # text must report that same resolved path, not a hardcoded literal.
    # Read the action's own `help` string, not format_help()'s rendering:
    # argparse line-wraps long help text (a tmp_path is long enough to
    # wrap), which would split the path across lines and make a plain
    # substring check on the wrapped output flaky regardless of this fix.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "herdr-bridge" / "config.json"
    Config().save(legacy)
    help_text = parser()._option_string_actions["--config"].help
    assert help_text is not None
    assert str(legacy) in help_text
    assert "answerback-bridge/config.json" not in help_text


@pytest.mark.parametrize("group", ["clients", "skill"])
def test_clients_install_choices_track_client_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    """red_at_base: both install parsers accept an added client without a second list."""
    from codex_herdr_bridge import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    existing = ("codex", "claude", "grok", "kiro", "crew", *contributed("CLIENTS", ()))
    assert tuple(name for name in cli.CLIENT_NAMES if name != "kiro-crew") == existing
    # CLIENT_NAMES supplies parser choices; this synthetic client models a future addition.
    for names in (cli.CLIENT_NAMES, (*cli.CLIENT_NAMES, "future-client")):
        monkeypatch.setattr(cli, "CLIENT_NAMES", names)
        selected_parser = parser()
        for name in names:
            arguments = [group, "install", "--client", name]
            if name == "kiro-crew":
                with pytest.raises(SystemExit) as refusal:
                    selected_parser.parse_args(arguments)
                assert refusal.value.code == 2
            else:
                assert selected_parser.parse_args(arguments).client == [name]
                assert selected_parser.parse_args(
                    ["clients", "migrate", "--client", name]
                ).client == [name]
