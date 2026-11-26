import asyncio
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import Output


@pytest.fixture
def short_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="hb-live-", dir="/tmp") as directory:
        yield Path(directory).resolve()


@pytest.fixture(autouse=True)
def _isolated_default_bridge_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's default report fallback inside tmp_path, never ~/.config/herdr-bridge."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def module() -> Any:
    assert importlib.util.find_spec("codex_herdr_bridge.live_test") is not None, (
        "Reusable live test wrapper is missing"
    )
    from codex_herdr_bridge import live_test

    return live_test


def owned(directory: Path) -> dict[str, Any]:
    directory.mkdir()
    workspace = directory / "workspace"
    workspace.mkdir()
    Config(
        "bridge-tests",
        herdr_binary="/tools/herdr",
        codex_binary="/tools/codex",
        kiro_binary="/tools/kiro-cli",
        kiro_workspaces={"test-worker": str(workspace)},
    ).save(directory / "config.json")
    (directory / "herdr.toml").write_text("[experimental]\nallow_nested=true\n")
    manifest = {
        "schema": 1,
        # T-144: the old owner value, on purpose. A new environment is written with
        # "answerback-live-test"; this fixture pins that an environment made before
        # that change is still accepted.
        "owner": "herdr-bridge-live-test",
        "session": "bridge-tests",
        "root": str(directory),
        "workspace": str(workspace),
        "pane_id": "w1:p1",
        "terminal_id": "term-owned",
        "agent": "test-orchestrator",
        "agent_snapshot": {
            "agent": "codex",
            "name": "test-orchestrator",
            "pane_id": "w1:p1",
            "terminal_id": "term-owned",
        },
    }
    (directory / "environment.json").write_text(json.dumps(manifest))
    return manifest


class Peers:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.commands: list[list[str]] = []
        self.launches = 0
        self.bridge_starts = 0
        self.harness_calls = 0
        self.running = True
        self.collision = False
        self.agent_status = "idle"
        self.terminal_id = "term-owned"
        self.duplicate = False

    async def command(self, args: list[str], **kwargs: Any) -> Output:
        self.commands.append(args)
        assert args[1] == "--session"
        for key in ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_SESSION"):
            assert key not in kwargs["env"]
        command = args[3:]
        result: dict[str, Any]
        if command[:2] == ["session", "list"]:
            result = {
                "sessions": [
                    {
                        "name": args[2],
                        "running": self.running,
                        "socket_path": str(self.directory / "herdr.sock"),
                    }
                ]
            }
        elif command == ["pane", "list"]:
            result = {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "terminal_id": self.terminal_id,
                        "cwd": str(self.directory / "workspace"),
                        "agent": "codex",
                    }
                ]
            }
        elif command == ["agent", "list"]:
            agent = {
                "pane_id": "w1:p1",
                "terminal_id": self.terminal_id,
                "name": "test-orchestrator",
                "agent": "codex",
                "agent_status": self.agent_status,
                "interactive_ready": True,
                "foreground_cwd": str(self.directory / "workspace"),
            }
            result = {"agents": [agent, dict(agent)] if self.duplicate else [agent]}
        else:
            raise AssertionError(f"Unexpected mutation: {args}")
        return Output(json.dumps({"result": result}), "")

    def launch(self, *_: Any) -> Any:
        self.launches += 1
        self.running = True
        return SimpleNamespace(pid=12345, poll=lambda: None)

    async def bridge(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.bridge_starts += 1
        return {"accepting": True}

    async def harness(self, config_path: Path, **kwargs: Any) -> dict[str, Any]:
        self.harness_calls += 1
        assert config_path == self.directory / "config.json"
        assert kwargs["mode"] == "live"
        assert kwargs["herdr_peer"] == "herdr:test-orchestrator"
        assert kwargs["kiro_peer"] == "kiro:test-worker"
        return {"mode": "live", "success": True, "phases": [], "outstanding_task_ids": []}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        live = module()
        monkeypatch.setattr(live, "run", self.command)
        monkeypatch.setattr(live, "_launch_server", self.launch)
        monkeypatch.setattr(live, "_socket_peer_pid", lambda _: 12345, raising=False)
        monkeypatch.setattr(live.lifecycle, "start", self.bridge)
        monkeypatch.setattr(live, "run_harness", self.harness)
        monkeypatch.setattr(
            live.shutil, "which", lambda name: name if name.startswith("/") else "/tools/" + name
        )
        return live


async def test_reuses_owned_agent_and_saves_unique_reports(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "unrelated")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/unrelated.sock")
    monkeypatch.setenv("HERDR_SESSION", "unrelated")
    first = await live.run_live_test(directory)
    second = await live.run_live_test(directory)
    assert first["success"] is second["success"] is True
    assert first["report_path"] != second["report_path"]
    for report in (first, second):
        assert json.loads(await asyncio.to_thread(Path(report["report_path"]).read_text)) == report
        assert report["resources_retained"] is True
    assert peers.launches == 0
    assert peers.harness_calls == 2
    assert os.environ["HERDR_PANE_ID"] == "unrelated"


@pytest.mark.parametrize(
    "problem", ["unowned", "config", "workspace", "identity", "busy", "blocked", "ambiguous"]
)
async def test_refuses_unsafe_reuse_without_dispatch_or_replacement(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    if problem == "unowned":
        (directory / "environment.json").unlink()
    elif problem == "config":
        config = Config.load(directory / "config.json")
        config.herdr_session = "unrelated"
        config.save(directory / "config.json")
    elif problem == "workspace":
        manifest["workspace"] = str(short_path / "other")
        (directory / "environment.json").write_text(json.dumps(manifest))
    elif problem == "identity":
        peers.terminal_id = "term-replacement"
    elif problem in ("busy", "blocked"):
        peers.agent_status = "working" if problem == "busy" else "blocked"
    else:
        peers.duplicate = True
    report = await live.run_live_test(directory)
    assert report["success"] is False
    assert await asyncio.to_thread(Path(report["report_path"]).is_file)
    assert peers.launches == peers.bridge_starts == peers.harness_calls == 0
    assert not any("start" in args or "create" in args for args in peers.commands)
    if problem == "unowned":
        assert not (directory / "reports").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"herdr_session": "unrelated"},
        {"kiro_workspaces": {"test-worker": "/different/workspace"}},
        {"desktop_threads": {"other": "00000000-0000-0000-0000-000000000001"}},
        {"crew_enabled": True},
        {"herdr_session": "unrelated", "crew_enabled": True},
    ],
)
async def test_stale_config_names_every_changed_field_and_recovery(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    config = Config.load(directory / "config.json")
    for name, value in changes.items():
        setattr(config, name, value)
    config.save(directory / "config.json")
    before = (directory / "config.json").read_bytes()

    report = await live.run_live_test(directory)

    assert report["success"] is False
    detail = report["phases"][0]["detail"]
    for name in changes:
        assert name in detail
    assert str(directory / "config.json") in detail
    assert "--reset-test-env" in detail
    assert "unrelated" not in detail  # Only field names, never private configuration values.
    assert (directory / "config.json").read_bytes() == before
    assert peers.commands == []


async def test_digest_drift_names_the_changed_field(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    config = Config.load(directory / "config.json")
    manifest["config_digest"] = live._digest(config)
    (directory / "environment.json").write_text(json.dumps(manifest))
    config.heartbeat_interval = 23
    config.save(directory / "config.json")

    report = await live.run_live_test(directory)

    assert report["success"] is False
    assert "heartbeat_interval" in report["phases"][0]["detail"]
    assert "--reset-test-env" in report["phases"][0]["detail"]
    assert peers.commands == []


@pytest.mark.parametrize(
    "changed_field,value",
    [
        ("heartbeat_misses", 8),
        ("herdr_binary", "/changed/herdr"),
        ("codex_binary", "/changed/codex"),
        ("kiro_binary", "/changed/kiro"),
    ],
)
async def test_recorded_field_fingerprints_distinguish_drift_from_existing_custom_values(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, changed_field: str, value: Any
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    config = Config.load(directory / "config.json")
    config.heartbeat_interval = 23
    config.save(directory / "config.json")
    manifest.update(
        config_digest=live._digest(config), config_field_digests=live._field_digests(config)
    )
    (directory / "environment.json").write_text(json.dumps(manifest))
    setattr(config, changed_field, value)
    config.save(directory / "config.json")

    report = await live.run_live_test(directory)

    assert report["success"] is False
    assert changed_field in report["phases"][0]["detail"]
    assert "heartbeat_interval" not in report["phases"][0]["detail"]
    assert peers.commands == []


@pytest.mark.parametrize("changed_binary", [None, "herdr_binary", "codex_binary", "kiro_binary"])
async def test_legacy_digest_without_field_history_does_not_guess_the_cause(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, changed_binary: str | None
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    manifest["config_digest"] = "older-schema-digest"
    (directory / "environment.json").write_text(json.dumps(manifest))
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    if changed_binary is not None:
        config = Config.load(directory / "config.json")
        setattr(config, changed_binary, "/changed/executable")
        config.save(directory / "config.json")

    report = await live.run_live_test(directory)

    assert report["success"] is False
    detail = report["phases"][0]["detail"]
    assert "aggregate digest changed" in detail
    assert "per-field history unavailable" in detail
    assert "schema/defaults changed" not in detail
    assert "--reset-test-env" in detail
    if changed_binary is not None:
        assert changed_binary not in detail
    assert peers.commands == []


async def test_reset_backs_up_config_and_preserves_owned_state_without_probes(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    expected = asdict(Config.load(directory / "config.json"))
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    config = Config.load(directory / "config.json")
    config.herdr_session = "unrelated"
    config.crew_enabled = True
    config.save(directory / "config.json")
    before = (directory / "config.json").read_bytes()
    (directory / "tasks.sqlite").write_bytes(b"receipt evidence")
    (directory / "workspace" / "result.txt").write_text("retained result")

    report = await live.run_live_test(directory, reset_test_env=True)

    assert report["success"] is True, report
    assert report["reset_only"] is True
    backup = Path(report["config_backup"])
    assert await asyncio.to_thread(backup.read_bytes) == before
    assert (await asyncio.to_thread(backup.stat)).st_mode & 0o777 == 0o600
    assert asdict(Config.load(directory / "config.json")) == expected
    saved = json.loads((directory / "environment.json").read_text())
    for key, value in manifest.items():
        assert saved[key] == value
    assert saved["config_digest"] == live._digest(Config.load(directory / "config.json"))
    assert (directory / "tasks.sqlite").read_bytes() == b"receipt evidence"
    assert (directory / "workspace" / "result.txt").read_text() == "retained result"
    assert peers.commands == []
    assert peers.launches == peers.bridge_starts == peers.harness_calls == 0
    assert (await live.run_live_test(directory))["success"] is True


@pytest.mark.parametrize("problem", ["unowned", "busy_bridge", "symlink", "concurrent"])
async def test_reset_refuses_unverified_or_active_environment(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    import fcntl
    from contextlib import ExitStack

    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    before = (directory / "config.json").read_bytes()
    with ExitStack() as stack:
        if problem == "unowned":
            (directory / "environment.json").unlink()
        elif problem in ("busy_bridge", "concurrent"):
            lock_path = directory / "run.lock"
            if problem == "busy_bridge":
                (directory / "runtime").mkdir(mode=0o755)
                (directory / "runtime").chmod(0o755)
                lock_path = directory / "runtime" / "owner.lock"
            handle = stack.enter_context(lock_path.open("w"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            elsewhere = short_path / "foreign-config.json"
            (directory / "config.json").rename(elsewhere)
            (directory / "config.json").symlink_to(elsewhere)
        report = await live.run_live_test(directory, reset_test_env=True)

    assert report["success"] is False
    assert (directory / "config.json").read_bytes() == before
    assert not list(directory.glob("config.before-reset-*.json"))
    assert peers.commands == []
    assert peers.launches == peers.bridge_starts == peers.harness_calls == 0
    if problem == "busy_bridge":
        assert (directory / "runtime").stat().st_mode & 0o777 == 0o755


async def test_reset_flag_is_wired_through_cli(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.cli import dispatch, parser

    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    peers.install(monkeypatch)
    args = parser().parse_args(["live-test", "--directory", str(directory), "--reset-test-env"])
    report = await dispatch(args)
    assert report["success"] is True
    assert report["reset_only"] is True
    assert peers.harness_calls == 0


async def test_reset_does_not_provision_a_missing_environment(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "absent"
    peers = Peers(directory)
    live = peers.install(monkeypatch)

    report = await live.run_live_test(directory, reset_test_env=True)

    assert report["success"] is False
    assert not directory.exists()
    assert peers.commands == []
    assert peers.launches == peers.bridge_starts == peers.harness_calls == 0


async def test_owned_stopped_server_can_restart_but_must_keep_saved_identity(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    peers.running = False
    live = peers.install(monkeypatch)
    report = await live.run_live_test(directory)
    assert report["success"] is True
    assert peers.launches == 1
    assert peers.harness_calls == 1


async def test_unknown_creation_is_not_retried(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    manifest.pop("pane_id")
    manifest.pop("terminal_id")
    manifest["workspace_creation"] = "uncertain"
    (directory / "environment.json").write_text(json.dumps(manifest))
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    report = await live.run_live_test(directory)
    assert report["success"] is False
    assert peers.launches == peers.bridge_starts == peers.harness_calls == 0
    assert not any("create" in args for args in peers.commands)


async def test_concurrent_run_is_refused(short_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    with (directory / "run.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = await live.run_live_test(directory)
    assert report["success"] is False
    assert "running" in report["phases"][-1]["detail"].lower()
    assert peers.commands == []


async def test_invalid_timeout_has_no_provisioning_effects(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "absent"
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    report = await live.run_live_test(directory, timeout=0)
    assert report["success"] is False
    assert not directory.exists()
    assert peers.commands == []


async def test_new_directory_refuses_unowned_named_session_collision(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "new"
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    report = await live.run_live_test(directory)
    assert report["success"] is False
    assert "already exists" in report["phases"][0]["detail"]
    assert not directory.exists()
    assert peers.launches == peers.harness_calls == 0


async def test_provisions_once_and_records_creation_before_mutation(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "new"

    class NewPeers(Peers):
        created_workspace = False
        created_agent = False

        async def command(self, args: list[str], **kwargs: Any) -> Output:
            command = args[3:]
            if command[:2] == ["session", "list"] and not self.launches:
                return Output(json.dumps({"sessions": []}), "")
            if command == ["pane", "list"] and not self.created_workspace:
                return Output(json.dumps({"result": {"panes": []}}), "")
            if command[:2] == ["workspace", "create"]:
                assert not self.created_workspace
                state = json.loads((directory / "environment.json").read_text())
                assert state["workspace_creation"] == "uncertain"
                assert "--no-focus" in command
                self.created_workspace = True
                self.commands.append(args)
                return Output(
                    json.dumps(
                        {"result": {"root_pane": {"pane_id": "w1:p1", "terminal_id": "term-owned"}}}
                    ),
                    "",
                )
            if command == ["agent", "list"] and not self.created_agent:
                return Output(json.dumps({"result": {"agents": []}}), "")
            if command[:2] == ["agent", "start"]:
                assert not self.created_agent
                state = json.loads((directory / "environment.json").read_text())
                assert state["agent_creation"] == "uncertain"
                assert "-s" not in command
                settings = [
                    command[index + 1] for index, item in enumerate(command) if item == "-c"
                ]
                parsed = tomllib.loads("\n".join(settings))
                profile = parsed["permissions"]["herdr_test"]
                assert profile["extends"] == ":workspace"
                assert profile["network"]["unix_sockets"] == {
                    str(directory / "runtime" / "bridge.sock"): "allow"
                }
                assert profile["network"]["enabled"] is True
                assert profile["network"]["domains"] == {}
                assert profile["network"]["dangerously_allow_all_unix_sockets"] is False
                assert profile["network"]["allow_local_binding"] is False
                assert command[command.index("--enable") + 1] == "network_proxy"
                assert parsed["default_permissions"] == "herdr_test"
                assert command[command.index("-a") + 1] == "on-request"
                self.created_agent = True
                self.commands.append(args)
                return Output(json.dumps({"result": {}}), "")
            return await super().command(args, **kwargs)

    peers = NewPeers(directory)
    live = peers.install(monkeypatch)
    first = await live.run_live_test(directory)
    second = await live.run_live_test(directory)
    assert first["success"] is second["success"] is True, (first, second)
    assert peers.launches == 1
    assert sum(args[3:5] == ["workspace", "create"] for args in peers.commands) == 1
    assert sum(args[3:5] == ["agent", "start"] for args in peers.commands) == 1
    assert peers.harness_calls == 2


class ProvisioningPeers(Peers):
    """Peers of an environment that does not exist yet: nothing runs until it is launched."""

    created_workspace = False
    created_agent = False

    async def command(self, args: list[str], **kwargs: Any) -> Output:
        command = args[3:]
        if command[:2] == ["session", "list"] and not self.launches:
            return Output(json.dumps({"sessions": []}), "")
        if command == ["pane", "list"] and not self.created_workspace:
            return Output(json.dumps({"result": {"panes": []}}), "")
        if command[:2] == ["workspace", "create"]:
            self.created_workspace = True
            self.commands.append(args)
            pane = {"pane_id": "w1:p1", "terminal_id": "term-owned"}
            return Output(json.dumps({"result": {"root_pane": pane}}), "")
        if command == ["agent", "list"] and not self.created_agent:
            return Output(json.dumps({"result": {"agents": []}}), "")
        if command[:2] == ["agent", "start"]:
            self.created_agent = True
            self.commands.append(args)
            return Output(json.dumps({"result": {}}), "")
        return await super().command(args, **kwargs)


@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077], ids=["002", "022", "077"])
async def test_provisions_into_a_missing_parent_with_private_folders_whatever_the_umask(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    """T-77 (fix round): the environment folder and the folder above it are both missing.

    The folder above got the default mode (0775 under umask 002), and the save of the
    configuration a few lines later refused it. The suite runs under umask 022, so the
    test sets the umask itself.
    """
    directory = short_path / "n" / "e"
    peers = ProvisioningPeers(directory)
    live = peers.install(monkeypatch)
    os.umask(mask)

    first = await live.run_live_test(directory)
    second = await live.run_live_test(directory)

    assert first["success"] is second["success"] is True, (first, second)
    assert peers.launches == 1
    assert sum(args[3:5] == ["workspace", "create"] for args in peers.commands) == 1
    assert sum(args[3:5] == ["agent", "start"] for args in peers.commands) == 1
    assert peers.harness_calls == 2
    assert Config.load(directory / "config.json").herdr_session
    assert directory.parent.stat().st_mode & 0o777 == 0o700
    assert directory.stat().st_mode & 0o777 == 0o700


async def test_new_agent_start_waits_for_readiness_without_starting_twice(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    manifest.pop("agent_snapshot")
    manifest["agent_creation"] = "not_started"
    (directory / "environment.json").write_text(json.dumps(manifest))

    class Starting(Peers):
        started = False
        observations = 0

        async def command(self, args: list[str], **kwargs: Any) -> Output:
            command = args[3:]
            if command[:2] == ["agent", "start"]:
                assert not self.started
                self.started = True
                self.commands.append(args)
                return Output(json.dumps({"result": {}}), "")
            if command == ["agent", "list"]:
                if not self.started:
                    return Output(json.dumps({"result": {"agents": []}}), "")
                self.observations += 1
                self.agent_status = "working" if self.observations == 1 else "idle"
            return await super().command(args, **kwargs)

    peers = Starting(directory)
    live = peers.install(monkeypatch)
    result = await live.run_live_test(directory)
    assert result["success"] is True, result
    assert sum(args[3:5] == ["agent", "start"] for args in peers.commands) == 1


async def test_default_session_is_never_accepted_as_owned_test_environment(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    manifest["session"] = "default"
    (directory / "environment.json").write_text(json.dumps(manifest))
    config = Config.load(directory / "config.json")
    config.herdr_session = "default"
    config.save(directory / "config.json")
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    result = await live.run_live_test(directory)
    assert result["success"] is False
    assert peers.commands == []


async def test_symlinked_server_log_cannot_be_used(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    owned(directory)
    other = short_path / "other-log"
    other.write_text("untouched")
    (directory / "server.log").symlink_to(other)
    peers = Peers(directory)
    peers.running = False
    live = peers.install(monkeypatch)
    result = await live.run_live_test(directory)
    assert result["success"] is False
    assert peers.launches == 0
    assert other.read_text() == "untouched"


def test_fallback_report_directory_is_private(tmp_path: Path) -> None:
    """Without a destination, the report must land somewhere only this user can read.

    Ported from 32eff3f, which secured a bridge-runtime fallback root this line did not
    have at the time; 8dcb517-A (this port) gives ``_save_report`` that bounded fallback
    root, so the intent kept is that the fallback root and the ``reports/`` directory
    beneath it are both ``0o700``, whatever umask the process happens to run with.
    """
    live = module()
    report: dict[str, Any] = {"mode": "live-test"}
    fallback_root = tmp_path / "bridge" / "runtime"
    original_umask = os.umask(0o022)  # permissive, so a mode that leaks the umask is visible
    try:
        live._save_report(report, None, fallback_root)
    finally:
        os.umask(original_umask)
    saved = Path(report["report_path"])
    assert saved.is_file()
    assert saved.parent == fallback_root / "reports"
    assert saved.parent.stat().st_mode & 0o777 == 0o700
    assert fallback_root.stat().st_mode & 0o777 == 0o700


def test_symlinked_report_destination_is_refused(tmp_path: Path) -> None:
    live = module()
    destination = tmp_path / "destination"
    destination.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (destination / "reports").symlink_to(elsewhere)
    report: dict[str, Any] = {"mode": "live-test"}
    with pytest.raises(live.LiveTestFailure, match="symlinked report destination"):
        live._save_report(report, destination, tmp_path / "unused-fallback")
    assert "report_path" not in report
    assert list(elsewhere.iterdir()) == []
    assert not (tmp_path / "unused-fallback").exists()


def _tmp_report_dir_names() -> set[str]:
    return {entry.name for entry in Path("/tmp").glob("hb-live-report-*")}


async def test_refused_run_without_report_dir_uses_bridge_runtime_directory(
    short_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused run must never mint an unbounded, never-removed /tmp directory."""
    live = module()
    directory = short_path / "absent"
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda *a, **k: pytest.fail("refused run minted a fresh temporary directory"),
    )
    before = _tmp_report_dir_names()
    report = await live.run_live_test(directory, timeout=0)
    assert _tmp_report_dir_names() == before
    assert report["success"] is False
    assert not directory.exists()
    expected_root = live.runtime_dir(live.default_config_path())
    assert expected_root.is_relative_to(tmp_path), expected_root  # the hermetic fixture held
    assert Path(report["report_path"]).is_relative_to(expected_root)
    assert json.loads(await asyncio.to_thread(Path(report["report_path"]).read_text)) == report


async def test_refused_run_honors_explicit_report_dir(short_path: Path, tmp_path: Path) -> None:
    live = module()
    directory = short_path / "absent"
    report_dir = tmp_path / "reports-destination"
    report = await live.run_live_test(directory, timeout=0, report_dir=report_dir)
    assert report["success"] is False
    assert not directory.exists()
    assert Path(report["report_path"]).is_relative_to(report_dir)
    assert Path(report["report_path"]).parent == report_dir / "reports"


async def test_two_refused_runs_reuse_one_report_directory(short_path: Path) -> None:
    live = module()
    directory = short_path / "absent"
    first = await live.run_live_test(directory, timeout=0)
    second = await live.run_live_test(directory, timeout=0)
    first_path, second_path = Path(first["report_path"]), Path(second["report_path"])
    assert first_path != second_path
    assert first_path.parent == second_path.parent
    assert first_path.parent == live.runtime_dir(live.default_config_path()) / "reports"
    assert {first_path.name, second_path.name} == {
        entry.name for entry in await asyncio.to_thread(lambda: list(first_path.parent.iterdir()))
    }


async def test_unavailable_owned_destination_falls_back_to_the_same_bounded_root(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the owned destination cannot take the report, the fallback is still one place.

    Writing into the owned environment fails (an OSError from the filesystem), so the
    report goes to the fallback root instead: both runs must land in that same bounded
    ``runtime/reports`` directory, never in a fresh directory per run.
    """
    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    real_atomic_write = live.atomic_write

    def failing_inside_owned(path: Path, text: str) -> None:
        if path.is_relative_to(directory / "reports"):
            raise OSError("owned report destination is read-only")
        real_atomic_write(path, text)

    monkeypatch.setattr(live, "atomic_write", failing_inside_owned)
    first = await live.run_live_test(directory)
    second = await live.run_live_test(directory)
    expected = live.runtime_dir(live.default_config_path()) / "reports"
    for report in (first, second):
        assert report["success"] is True
        assert "saved privately elsewhere" in " ".join(report["limitations"])
        assert Path(report["report_path"]).parent == expected
    assert first["report_path"] != second["report_path"]


async def test_live_test_refuses_a_symlinked_report_dir(short_path: Path, tmp_path: Path) -> None:
    live = module()
    directory = short_path / "absent"
    real = tmp_path / "real-reports"
    real.mkdir()
    report_dir = tmp_path / "linked-reports"
    report_dir.symlink_to(real)
    report = await live.run_live_test(directory, timeout=0, report_dir=report_dir)
    assert report["success"] is False
    assert "symlinked report directory" in report["phases"][0]["detail"]
    # The refused report still lands somewhere owned -- the default fallback -- not nowhere.
    expected_root = live.runtime_dir(live.default_config_path())
    assert Path(report["report_path"]).is_relative_to(expected_root)
    assert not any(real.iterdir())


def test_live_test_cli_threads_report_dir_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.cli import dispatch, parser

    captured: dict[str, Any] = {}

    async def fake_run_live_test(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(args=args, kwargs=kwargs)
        return {"success": False}

    monkeypatch.setattr("codex_herdr_bridge.live_test.run_live_test", fake_run_live_test)
    args = parser().parse_args(
        [
            "live-test",
            "--directory",
            str(tmp_path / "env"),
            "--timeout",
            "7",
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert asyncio.run(dispatch(args)) == {"success": False}
    assert captured == {
        "args": (tmp_path / "env", 7, tmp_path / "reports"),
        "kwargs": {"reset_test_env": False},
    }
    assert parser().parse_args(["live-test"]).report_dir is None


async def test_detected_startup_collision_cannot_be_adopted_on_next_run(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"

    class Racing(Peers):
        inventories = 0

        async def command(self, args: list[str], **kwargs: Any) -> Output:
            if args[3:5] == ["session", "list"]:
                self.inventories += 1
                if self.inventories == 1:
                    return Output(json.dumps({"sessions": []}), "")
            return await super().command(args, **kwargs)

    peers = Racing(directory)
    live = peers.install(monkeypatch)
    first = await live.run_live_test(directory)
    second = await live.run_live_test(directory)
    assert first["success"] is second["success"] is False
    assert peers.launches == peers.harness_calls == 0
    assert not any(args[3:5] == ["workspace", "create"] for args in peers.commands)


async def test_uncertain_server_launch_never_blindly_retries(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = short_path / "live"
    manifest = owned(directory)
    manifest["server_launch"] = "uncertain"
    (directory / "environment.json").write_text(json.dumps(manifest))
    peers = Peers(directory)
    live = peers.install(monkeypatch)
    result = await live.run_live_test(directory)
    assert result["success"] is False
    assert peers.commands == []


@pytest.mark.parametrize("child_exited", [False, True])
async def test_foreign_server_winning_startup_race_is_never_adopted(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, child_exited: bool
) -> None:
    directory = short_path / "live"
    owned(directory)
    peers = Peers(directory)
    peers.running = False
    live = peers.install(monkeypatch)
    monkeypatch.setattr(live, "_socket_peer_pid", lambda _: 98765)
    if child_exited:
        launch = peers.launch

        def exited(*args: Any) -> Any:
            launch(*args)
            return SimpleNamespace(pid=12345, poll=lambda: 1)

        monkeypatch.setattr(live, "_launch_server", exited)
    report = await live.run_live_test(directory)
    assert report["success"] is False
    assert peers.launches == 1
    assert peers.bridge_starts == peers.harness_calls == 0
    assert not any(args[3:5] == ["pane", "list"] for args in peers.commands)
    saved = json.loads((directory / "environment.json").read_text())
    assert saved["server_launch"] in ("uncertain", "collision")
    rerun = await live.run_live_test(directory)
    assert rerun["success"] is False
    assert peers.launches == 1


def test_socket_peer_pid_matches_local_listener_owner(short_path: Path) -> None:
    live = module()
    assert hasattr(live, "_socket_peer_pid"), "OS peer identity verification is missing"
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("Unix peer PID is implemented only for macOS/Linux")
    path = short_path / "peer.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        assert live._socket_peer_pid(path) == os.getpid()


@pytest.mark.parametrize("platform", ["darwin", "linux", "unsupported"])
def test_socket_peer_pid_uses_platform_credentials_and_fails_closed(
    short_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    live = module()
    assert hasattr(live, "_socket_peer_pid"), "OS peer identity verification is missing"
    options: list[tuple[int, int, int]] = []

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def settimeout(self, seconds: float) -> None:
            assert seconds <= 3

        def connect(self, path: str) -> None:
            assert path == str(short_path / "peer.sock")

        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            options.append((level, option, size))
            return (
                struct.pack("i", 12345)
                if platform == "darwin"
                else struct.pack("3i", 12345, 501, 20)
            )

    monkeypatch.setattr(live.sys, "platform", platform)
    monkeypatch.setattr(live.socket, "socket", lambda *args: Connection())
    if platform == "unsupported":
        with pytest.raises(live.LiveTestFailure, match="unsupported"):
            live._socket_peer_pid(short_path / "peer.sock")
        assert options == []
    else:
        assert live._socket_peer_pid(short_path / "peer.sock") == 12345
        expected = (
            [(0, 2, 4)]
            if platform == "darwin"
            else [(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)]
        )
        assert options == expected
