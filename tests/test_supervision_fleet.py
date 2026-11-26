from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.fleet.settings import FileSettings, default_path, write
from answerback_bridge.process import CommandError, Output, module_launch_argv


class RecordedLaunchctl:
    def __init__(self) -> None:
        self.registered: set[str] = set()
        self.calls: list[list[str]] = []
        self.print_output = "state = running\npid = 4321\nlast exit code = 7\n"

    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        self.calls.append(list(args))
        assert args[0] == "launchctl"
        verb = args[1]
        if verb == "print":
            if args[2] not in self.registered:
                raise CommandError(113, "fixture service absent")
            return Output(self.print_output, "")
        if verb == "bootout":
            self.registered.discard(args[2])
            return Output("", "")
        if verb == "bootstrap":
            self.registered.add(f"{args[2]}/{Path(args[3]).stem}")
            return Output("", "")
        assert verb == "kickstart"
        return Output("", "")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if call[1] != "print"]


def configured_path(tmp_path: Path) -> Path:
    config_path = tmp_path / "cfg" / "config.json"
    Config("invented-session").save(config_path)
    write(default_path(config_path), FileSettings(machines=("build-a", "build-b")))
    return config_path


def darwin_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launchctl: RecordedLaunchctl):
    from answerback_bridge.fleet.supervision import FleetWriterSupervisor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("answerback_bridge.supervision.sys.platform", "darwin")
    monkeypatch.setattr("answerback_bridge.supervision.run", launchctl)
    monkeypatch.setattr("answerback_bridge.fleet.supervision.sys.platform", "darwin")
    monkeypatch.setattr("answerback_bridge.fleet.supervision.run", launchctl)
    return FleetWriterSupervisor(configured_path(tmp_path))


def test_fleet_writer_launchd_definition_uses_settings_and_bridge_digest(
    tmp_path: Path,
) -> None:
    from answerback_bridge.fleet.supervision import FleetWriterSupervisor
    from answerback_bridge.supervision import NativeSupervisor

    config_path = configured_path(tmp_path)
    bridge = NativeSupervisor(config_path)
    writer = FleetWriterSupervisor(config_path)
    document = plistlib.loads(writer.launchd_definition())

    digest = bridge.label.removeprefix("local.answerback-bridge.")
    assert writer.label == f"local.answerback-fleet.{digest}"
    assert document["ProgramArguments"] == module_launch_argv(
        config_path, "fleet", "serve", "--settings", str(default_path(config_path))
    )
    assert document["WorkingDirectory"] == str(config_path.parent)
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["ThrottleInterval"] == 10
    assert document["StandardOutPath"].startswith(str(config_path.parent / "runtime"))
    assert document["StandardErrorPath"] == document["StandardOutPath"]
    encoded = writer.launchd_definition()
    assert b"build-a" not in encoded and b"build-b" not in encoded

    compatible = ["/usr/bin/python3", *writer.command()[2:]]
    assert writer._matches_command(compatible)
    compatible[-1] = str(config_path.parent / "other-settings.json")
    assert not writer._matches_command(compatible)


def test_fleet_writer_systemd_unit_restarts_only_after_failure(tmp_path: Path) -> None:
    from answerback_bridge.fleet.supervision import FleetWriterSupervisor

    config_path = configured_path(tmp_path)
    definition = FleetWriterSupervisor(config_path).systemd_definition()

    assert "Description=Answerback fleet writer" in definition
    assert "Restart=on-failure\n" in definition
    assert "Restart=always\n" not in definition
    assert f'"{str(default_path(config_path))}"' in definition
    assert "build-a" not in definition and "build-b" not in definition
    assert definition.endswith("\n[Install]\nWantedBy=default.target\n")


async def test_fleet_writer_linux_status_and_stop_remove_owned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.fleet.supervision import FleetWriterSupervisor

    calls: list[list[str]] = []

    async def systemctl(args: list[str], **kwargs: Any) -> Output:
        calls.append(list(args))
        assert args[:2] == ["systemctl", "--user"]
        if args[2] == "show":
            return Output(
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=987\nExecMainStatus=4\n",
                "",
            )
        return Output("", "")

    monkeypatch.setattr("answerback_bridge.supervision.sys.platform", "linux")
    monkeypatch.setattr("answerback_bridge.supervision.run", systemctl)
    monkeypatch.setattr("answerback_bridge.fleet.supervision.sys.platform", "linux")
    monkeypatch.setattr("answerback_bridge.fleet.supervision.run", systemctl)
    writer = FleetWriterSupervisor(configured_path(tmp_path))

    await writer.start()
    unit = writer.root / f"{writer.label}.service"
    assert unit.is_file() and writer.marker.is_file()
    assert await writer.status() == {
        "installed": True,
        "running": True,
        "pid": 987,
        "settings_path": str(writer.settings_path),
        "last_exit": 4,
        "failure_reason": None,
    }

    await writer.stop_if_installed()

    assert not unit.exists()
    assert not writer.marker.exists()
    assert [call[2] for call in calls if call[2] != "show"] == [
        "enable",
        "daemon-reload",
        "start",
        "stop",
        "disable",
    ]


async def test_fleet_writer_changed_launchd_definition_is_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchctl = RecordedLaunchctl()
    writer = darwin_writer(tmp_path, monkeypatch, launchctl)
    await writer.start()
    path = Path.home() / "Library" / "LaunchAgents" / f"{writer.label}.plist"
    document = plistlib.loads(path.read_bytes())
    document["ThrottleInterval"] = 99
    path.write_bytes(plistlib.dumps(document))
    launchctl.calls.clear()

    await writer.start()

    assert launchctl.verbs() == ["bootout", "bootstrap"]
    assert plistlib.loads(path.read_bytes())["ThrottleInterval"] == 10


async def test_fleet_writer_status_reads_only_named_launchd_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchctl = RecordedLaunchctl()
    writer = darwin_writer(tmp_path, monkeypatch, launchctl)
    await writer.start()
    launchctl.print_output = (
        "state = running\n"
        "pid = 4321\n"
        "last exit code = 7\n"
        "path = /private/value-that-must-not-escape\n"
    )

    status = await writer.status()

    assert status == {
        "installed": True,
        "running": True,
        "pid": 4321,
        "settings_path": str(writer.settings_path),
        "last_exit": 7,
        "failure_reason": None,
    }
    assert "/private/value-that-must-not-escape" not in json.dumps(status)


async def test_start_skips_writer_without_settings_and_reports_exact_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import lifecycle

    config_path = tmp_path / "cfg" / "config.json"
    Config("invented-session").save(config_path)

    class ReadyBridge:
        def __init__(self, path: Path) -> None:
            assert path == config_path.resolve()

        async def status(self) -> dict[str, Any]:
            return {"running": True, "accepting": True}

    class WriterMustNotStart:
        def __init__(self, path: Path) -> None:
            self.settings_path = default_path(path)

        async def start(self) -> None:
            pytest.fail("an unconfigured fleet writer was started")

        async def status(self) -> dict[str, Any]:
            return {
                "installed": False,
                "running": False,
                "pid": None,
                "settings_path": str(self.settings_path),
                "last_exit": None,
                "failure_reason": None,
            }

    monkeypatch.setattr(lifecycle, "RemoteBridge", ReadyBridge)
    monkeypatch.setattr(
        "answerback_bridge.fleet.supervision.FleetWriterSupervisor", WriterMustNotStart
    )

    status = await lifecycle.start(config_path)

    assert status["fleet_writer"]["message"] == (
        "fleet writer not configured; run `answerback fleet configure`"
    )
    assert status["fleet_writer"]["installed"] is False


async def test_start_skips_writer_whose_settings_file_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R3: a present but unusable settings file must not start a writer that exits 2
    and is relaunched by launchd every ten seconds for good."""
    from answerback_bridge import lifecycle

    config_path = tmp_path / "cfg" / "config.json"
    Config("invented-session").save(config_path)
    settings_file = default_path(config_path)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{not json")

    class ReadyBridge:
        def __init__(self, path: Path) -> None:
            pass

        async def status(self) -> dict[str, Any]:
            return {"running": True, "accepting": True}

    class WriterMustNotStart:
        def __init__(self, path: Path) -> None:
            self.settings_path = default_path(path)

        async def start(self) -> None:
            pytest.fail("a fleet writer with invalid settings was started")

        async def status(self) -> dict[str, Any]:
            return {
                "installed": False,
                "running": False,
                "pid": None,
                "settings_path": str(self.settings_path),
                "last_exit": None,
                "failure_reason": None,
            }

    monkeypatch.setattr(lifecycle, "RemoteBridge", ReadyBridge)
    monkeypatch.setattr(
        "answerback_bridge.fleet.supervision.FleetWriterSupervisor", WriterMustNotStart
    )

    status = await lifecycle.start(config_path)

    assert status["fleet_writer"]["message"] == (
        "fleet writer settings invalid; run `answerback fleet configure`"
    )


async def test_start_waits_for_bridge_readiness_before_starting_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import lifecycle

    config_path = configured_path(tmp_path)
    events: list[str] = []
    statuses = iter(
        [
            {"running": False, "accepting": False},
            {"running": True, "accepting": True},
        ]
    )

    class Bridge:
        def __init__(self, path: Path) -> None:
            pass

        async def status(self) -> dict[str, Any]:
            status = next(statuses)
            if status["accepting"]:
                events.append("bridge-ready")
            return status

    class BridgeSupervisor:
        def __init__(self, path: Path) -> None:
            pass

        def legacy_installed(self) -> bool:
            return False

        async def start(self) -> None:
            events.append("bridge-start")

    class WriterSupervisor:
        def __init__(self, path: Path) -> None:
            self.settings_path = default_path(path)

        async def start(self) -> None:
            events.append("writer-start")

        async def status(self) -> dict[str, Any]:
            return {
                "installed": True,
                "running": True,
                "pid": 55,
                "settings_path": str(self.settings_path),
                "last_exit": None,
                "failure_reason": None,
            }

    monkeypatch.setattr(lifecycle, "RemoteBridge", Bridge)
    monkeypatch.setattr("answerback_bridge.supervision.NativeSupervisor", BridgeSupervisor)
    monkeypatch.setattr(
        "answerback_bridge.fleet.supervision.FleetWriterSupervisor", WriterSupervisor
    )

    status = await lifecycle.start(config_path)

    assert events == ["bridge-start", "bridge-ready", "writer-start"]
    assert status["fleet_writer"]["running"] is True


async def test_stop_removes_writer_before_bridge_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import lifecycle

    config_path = configured_path(tmp_path)
    events: list[str] = []

    class StoppedBridge:
        def __init__(self, path: Path) -> None:
            pass

        async def status(self) -> dict[str, Any]:
            return {"running": False}

    class BridgeSupervisor:
        def __init__(self, path: Path) -> None:
            pass

        async def stop_if_installed(self) -> None:
            events.append("bridge-stop")

    class WriterSupervisor:
        def __init__(self, path: Path) -> None:
            pass

        async def stop_if_installed(self) -> None:
            events.append("writer-stop")

    monkeypatch.setattr(lifecycle, "RemoteBridge", StoppedBridge)
    monkeypatch.setattr("answerback_bridge.supervision.NativeSupervisor", BridgeSupervisor)
    monkeypatch.setattr(
        "answerback_bridge.fleet.supervision.FleetWriterSupervisor", WriterSupervisor
    )

    await lifecycle.stop(config_path)

    assert events == ["writer-stop", "bridge-stop"]


async def test_cli_status_includes_fleet_writer_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import cli, lifecycle

    config_path = tmp_path / "cfg" / "config.json"

    class Bridge:
        def __init__(self, path: Path) -> None:
            assert path == config_path

        async def status(self) -> dict[str, Any]:
            return {"running": True, "accepting": True}

    expected = {
        "installed": True,
        "running": False,
        "pid": None,
        "settings_path": str(default_path(config_path)),
        "last_exit": 0,
        "failure_reason": None,
    }

    async def writer_status(path: Path) -> dict[str, Any]:
        assert path == config_path
        return expected

    monkeypatch.setattr(cli, "RemoteBridge", Bridge)
    monkeypatch.setattr(lifecycle, "fleet_writer_status", writer_status)
    args = cli.parser().parse_args(["--config", str(config_path), "status", "--json"])

    status = await cli.dispatch(args)

    assert status["fleet_writer"] == expected
