"""The bus broker's service definition lives where the OS reloads it after a reboot.

Every test runs against a fake HOME under tmp_path and a recorded fake `run`; nothing
here calls the real launchctl or systemctl, or writes under the real
~/Library/LaunchAgents. Broker reachability is modelled by the fake service manager.
"""

import json
import os
import plistlib
import socket
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")
from answerback_bridge.bus.broker import Broker
from answerback_bridge.bus.config import BusConfig
from answerback_bridge.process import BridgeError, CommandError, Output


class FakeManager:
    """Record launchctl/systemctl argv and model one broker job's registration."""

    def __init__(self, *, registered: bool = False, running: bool = False) -> None:
        self.registered = registered
        self.running = running
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        self.calls.append(list(args))
        if args[0] == "launchctl":
            verb = args[1]
            if verb == "print" and not self.registered:
                raise CommandError(113, "Could not find service")
            if verb == "bootstrap":
                self.registered = self.running = True
            elif verb == "bootout":
                self.registered = self.running = False
            elif verb == "kickstart":
                self.running = True
        else:
            assert args[:2] == ["systemctl", "--user"]
            if args[2] == "start":
                self.running = True
            elif args[2] == "stop":
                self.running = False
        return Output("", "")

    def verbs(self) -> list[str]:
        """Return the state-changing verbs, without read-only launchctl print calls."""
        return [
            call[1] if call[0] == "launchctl" else call[2]
            for call in self.calls
            if call[:2] != ["launchctl", "print"]
        ]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, manager: FakeManager
) -> Broker:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("answerback_bridge.supervision.sys.platform", platform)
    monkeypatch.setattr("answerback_bridge.supervision.run", manager)
    server = tmp_path / "bin" / "nats-server"
    server.parent.mkdir()
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    config = BusConfig(tmp_path / "cfg" / "config.json", _free_port(), str(server))
    broker = Broker(config)
    broker.supervisor.root.mkdir(parents=True)

    async def reachable() -> bool:
        return manager.running

    monkeypatch.setattr(broker, "reachable", reachable)
    return broker


def _launch_agent(broker: Broker) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{broker.supervisor.label}.plist"


def _old_plist(broker: Broker) -> Path:
    """Write the definition and marker exactly as a release before T-91 left them."""
    supervisor = broker.supervisor
    path = supervisor.root / f"{supervisor.label}.plist"
    path.write_bytes(supervisor.launchd_definition())
    supervisor.marker.write_text(json.dumps({"manager": "launchd", "label": supervisor.label}))
    return path


def _snapshot(definition: Path, marker: Path) -> tuple[bytes, int, bytes]:
    return definition.read_bytes(), definition.stat().st_mtime_ns, marker.read_bytes()


def _assert_installed(broker: Broker, path: Path, manager: str) -> None:
    assert path.is_file()
    assert path.is_relative_to(Path.home().parent), "the definition stays under tmp_path"
    assert json.loads(broker.supervisor.marker.read_text()) == {
        "manager": manager,
        "label": broker.supervisor.label,
        "definition": str(path),
    }


async def test_fresh_start_installs_a_launch_agent_and_bootstraps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)

    assert await broker.start() == {"broker_running": True, "managed": True}

    path = _launch_agent(broker)
    _assert_installed(broker, path, "launchd")
    assert plistlib.loads(path.read_bytes())["Label"] == broker.supervisor.label
    assert broker.supervisor.label == f"local.agent-bus.nats.{broker.config.namespace}"
    assert manager.verbs() == ["bootstrap"]
    bootstrap = next(call for call in manager.calls if call[1] == "bootstrap")
    assert bootstrap == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]
    assert not list(broker.supervisor.root.glob("*.plist"))


async def test_first_start_migrates_a_running_broker_from_the_old_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old release's broker is registered and answering: start must not stop at
    # "already reachable", or the definition would never move.
    manager = FakeManager(registered=True, running=True)
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    old = _old_plist(broker)

    assert await broker.start() == {"broker_running": True, "managed": True}

    assert not old.exists()
    _assert_installed(broker, _launch_agent(broker), "launchd")
    assert manager.verbs() == ["bootout", "bootstrap"]
    assert manager.registered and manager.running


async def test_first_start_after_a_reboot_migrates_an_unregistered_old_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()  # Rebooted: launchd never reloaded the old plist.
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    old = _old_plist(broker)

    await broker.start()

    assert not old.exists()
    _assert_installed(broker, _launch_agent(broker), "launchd")
    assert manager.verbs() == ["bootstrap"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_second_start_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    manager = FakeManager()
    broker = _broker(tmp_path, monkeypatch, platform, manager)
    await broker.start()
    path = Path(json.loads(broker.supervisor.marker.read_text())["definition"])
    before = _snapshot(path, broker.supervisor.marker)
    manager.calls.clear()

    assert await broker.start() == {"broker_running": True, "managed": True}

    assert manager.calls == []
    assert _snapshot(path, broker.supervisor.marker) == before


async def test_stop_removes_the_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    await broker.start()
    path = _launch_agent(broker)
    manager.calls.clear()

    assert await broker.stop() == {"broker_running": False, "coordinator_kept_running": True}

    assert manager.verbs() == ["bootout"]
    assert not path.exists()
    assert not broker.supervisor.marker.exists()
    assert not list(broker.supervisor.root.glob("*.plist"))


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("location", ["old", "new"])
async def test_definition_for_another_executable_is_refused_in_either_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, running: bool
) -> None:
    manager = FakeManager(registered=running, running=running)
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    supervisor = broker.supervisor
    if location == "old":
        path = _old_plist(broker)
    else:
        path = _launch_agent(broker)
        path.parent.mkdir(parents=True)
        path.write_bytes(supervisor.launchd_definition())
        supervisor.marker.write_text(
            json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(path)})
        )
    document = plistlib.loads(path.read_bytes())
    document["ProgramArguments"][0] = "/unrelated/nats-server"
    path.write_bytes(plistlib.dumps(document))
    original = (path.read_bytes(), supervisor.marker.read_bytes())

    with pytest.raises(BridgeError, match="ownership"):
        await broker.start()

    assert [call for call in manager.calls if call[:2] != ["launchctl", "print"]] == []
    assert (path.read_bytes(), supervisor.marker.read_bytes()) == original


async def test_occupied_unverified_port_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", broker.config.port))
        listener.listen()
        with pytest.raises(BridgeError, match="occupied"):
            await broker.start()
    assert manager.calls == []
    assert not broker.supervisor.marker.exists()
    assert not _launch_agent(broker).exists()


async def test_unmanaged_reachable_broker_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager(running=True)
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    assert await broker.start() == {"broker_running": True, "managed": False}
    assert manager.calls == []
    assert not _launch_agent(broker).exists()


def _unit_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line
            sections[current] = []
        elif line:
            sections[current].append(line)
    return sections


async def test_linux_unit_is_enabled_for_the_next_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager()
    broker = _broker(tmp_path, monkeypatch, "linux", manager)

    await broker.start()

    unit = broker.supervisor.root / f"{broker.supervisor.label}.service"
    _assert_installed(broker, unit, "systemd")
    assert manager.calls == [
        ["systemctl", "--user", "enable", str(unit)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "start", unit.name],
    ]
    # Without an [Install] section `systemctl enable` only links the unit and warns
    # that it has no installation config; WantedBy is what makes it start at login.
    assert _unit_sections(unit.read_text())["[Install]"] == ["WantedBy=default.target"]


async def test_linux_running_broker_with_an_unenableable_unit_is_reinstalled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FakeManager(running=True)
    broker = _broker(tmp_path, monkeypatch, "linux", manager)
    supervisor = broker.supervisor
    unit = supervisor.root / f"{supervisor.label}.service"
    old = supervisor.systemd_definition().split("\n[Install]\n")[0] + "\n"
    unit.write_text(old)
    supervisor.marker.write_text(
        json.dumps({"manager": "systemd", "label": supervisor.label, "definition": str(unit)})
    )

    await broker.start()

    assert "[Install]" in unit.read_text()
    assert manager.verbs() == ["enable", "daemon-reload", "start"]
    manager.calls.clear()
    await broker.start()
    assert manager.calls == []


async def test_down_launchd_broker_reloads_a_changed_restart_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loaded pre-fix job must not keep SuccessfulExit=false after upgrade."""
    manager = FakeManager(registered=True, running=False)
    broker = _broker(tmp_path, monkeypatch, "darwin", manager)
    path = _launch_agent(broker)
    path.parent.mkdir(parents=True)
    document = plistlib.loads(broker.supervisor.launchd_definition())
    document["KeepAlive"] = {"SuccessfulExit": False}
    path.write_bytes(plistlib.dumps(document))
    broker.supervisor.marker.write_text(
        json.dumps(
            {
                "manager": "launchd",
                "label": broker.supervisor.label,
                "definition": str(path),
            }
        )
    )

    assert await broker.start() == {"broker_running": True, "managed": True}

    assert manager.verbs() == ["bootout", "bootstrap"]
    assert plistlib.loads(path.read_bytes())["KeepAlive"] is True
    assert manager.registered and manager.running
