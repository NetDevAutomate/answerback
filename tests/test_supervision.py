import json
import os
import plistlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import Output

if TYPE_CHECKING:
    from codex_herdr_bridge.supervision import NativeSupervisor


def test_launchd_definition_preserves_paths_and_does_not_restart_a_clean_stop(
    tmp_path: Path,
) -> None:
    from codex_herdr_bridge.supervision import NativeSupervisor

    config = tmp_path / "path with spaces" / "config.json"
    Config("explicit-session").save(config)
    supervisor = NativeSupervisor(config)
    value = plistlib.loads(supervisor.launchd_definition())
    assert value["ProgramArguments"][-3:] == ["--config", str(config.resolve()), "serve"]
    assert value["KeepAlive"] == {"SuccessfulExit": False}
    assert value["Umask"] == 0o077
    assert value["RunAtLoad"] is True
    assert value["StandardErrorPath"].endswith("/runtime/launcher.log")


def test_systemd_definition_quotes_percent_and_spaces(tmp_path: Path) -> None:
    from codex_herdr_bridge.supervision import NativeSupervisor

    config = tmp_path / "path %name" / "config.json"
    Config("explicit-session").save(config)
    definition = NativeSupervisor(config).systemd_definition()
    exec_line = next(line for line in definition.splitlines() if line.startswith("ExecStart="))
    assert f'"{str(config.resolve()).replace("%", "%%")}"' in exec_line
    assert "Restart=on-failure" in definition
    assert "UMask=0077" in definition
    assert "KillMode=control-group" in definition


async def test_failed_native_start_does_not_claim_supervisor_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.supervision import NativeSupervisor

    async def no_user_bus(*args, **kwargs):
        raise BridgeError("No user bus")

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "linux")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", no_user_bus)
    supervisor = NativeSupervisor(tmp_path / "config.json")
    with pytest.raises(BridgeError, match="No user bus"):
        await supervisor.start()
    assert not supervisor.marker.exists()
    # Without false ownership, stop needs no unavailable native user bus.
    await supervisor.stop_if_installed()


def _working_directory_values(definition: str) -> list[str]:
    # split("\n"), not splitlines(): only a line feed ends a line of the generated text.
    lines = [line for line in definition.split("\n") if line.startswith("WorkingDirectory=")]
    return [line[len("WorkingDirectory=") :] for line in lines]


def test_systemd_working_directory_is_a_bare_absolute_path(tmp_path: Path) -> None:
    """red_at_pretip. On a Linux machine with systemd 259, a unit whose WorkingDirectory= value
    is a quoted path is refused with "path is not absolute" and "bad unit file setting". The
    value is the folder with '%' doubled and '/.' appended, with no quotes around it."""
    from codex_herdr_bridge.supervision import NativeSupervisor

    config = tmp_path / "path %name" / "config.json"
    values = _working_directory_values(NativeSupervisor(config).systemd_definition())
    assert values == [str(config.resolve().parent).replace("%", "%%") + "/."]
    assert values[0].startswith("/")


def test_systemd_working_directory_preserves_literal_dollars(tmp_path: Path) -> None:
    """red_at_pretip. A '$' in the folder's name is written once, as it is."""
    from codex_herdr_bridge.supervision import NativeSupervisor

    config = tmp_path / "$USER" / "config.json"
    values = _working_directory_values(NativeSupervisor(config).systemd_definition())
    assert values == [str(config.resolve().parent).replace("%", "%%") + "/."]
    assert values[0].endswith("/$USER/.")


# One folder of every class of the two measurements with systemd 259.9: T-146-matrix.json (fix
# round 1) and, for the nine rows whose last name holds a dot, group B of T-146-fix2-dots.json
# (rows 7 to 15, fix round 2). The last name of every folder here is the last name of a
# folder of its measurement, which stood under the measurement's own folder; the root folder
# was measured as itself. For each of those folders systemd parsed a value of this form to the
# folder, byte for byte. The paths are written out here, so no value depends on the name of a
# temporary folder.
WORKING_DIRECTORY_VALUES = [
    ("/srv/folder", "/srv/folder/."),
    ("/srv/a b", "/srv/a b/."),
    ("/srv/a%b", "/srv/a%%b/."),
    ("/srv/a%hb", "/srv/a%%hb/."),
    ("/srv/a%", "/srv/a%%/."),
    ("/srv/a%%b", "/srv/a%%%%b/."),
    ("/srv/a$b", "/srv/a$b/."),
    ("/srv/a$HOME", "/srv/a$HOME/."),
    ('/srv/a"b', '/srv/a"b/.'),
    ("/srv/a'b", "/srv/a'b/."),
    ("/srv/a\\b", "/srv/a\\b/."),
    ("/srv/a\\", "/srv/a\\/."),
    ("/srv/a\\\\", "/srv/a\\\\/."),
    ("/srv/a\\ ", "/srv/a\\ /."),
    ("/srv/a#b", "/srv/a#b/."),
    ("/srv/a;b", "/srv/a;b/."),
    ("/srv/a\tb", "/srv/a\tb/."),
    ("/srv/a\t", "/srv/a\t/."),
    ("/srv/a ", "/srv/a /."),
    ("/srv/a  ", "/srv/a  /."),
    ("/srv/ ", "/srv/ /."),
    ("/srv/café", "/srv/café/."),
    ("/srv/a\x1bb", "/srv/a\x1bb/."),
    ("/srv/a\x0c", "/srv/a\x0c/."),
    ("/srv/a\x7f", "/srv/a\x7f/."),
    ("/srv/a\u2028b", "/srv/a\u2028b/."),
    ("/srv/a\u2028", "/srv/a\u2028/."),
    ("/srv/...", "/srv/.../."),
    ("/srv/....", "/srv/..../."),
    ("/srv/..a", "/srv/..a/."),
    ("/srv/a..", "/srv/a../."),
    ("/srv/a..b", "/srv/a..b/."),
    ("/srv/.a", "/srv/.a/."),
    ("/srv/.. ", "/srv/.. /."),
    ("/srv/..\t", "/srv/..\t/."),
    ("/srv/ ..", "/srv/ ../."),
    ("/", "/."),
]


@pytest.mark.parametrize(("folder", "value"), WORKING_DIRECTORY_VALUES)
def test_systemd_working_directory_value_of_a_folder(folder: str, value: str) -> None:
    """red_at_pretip (d2146a5 has no such function). The exact value for one folder of every class
    of the measurements. The nine rows with dots pass against the modules of ca48926: a name that
    holds dots and is not '..' was and is written like any other name."""
    from codex_herdr_bridge.supervision import systemd_working_directory

    assert systemd_working_directory(Path(folder)) == value


@pytest.mark.parametrize(
    ("folder", "rule"),
    [
        ("/srv/a\nb", "the path holds a line feed or a carriage return"),
        ("/srv/a\n", "the path holds a line feed or a carriage return"),
        ("/srv/a\rb", "the path holds a line feed or a carriage return"),
        ("/srv/a\r", "the path holds a line feed or a carriage return"),
        ("relative/x", "the path is not absolute"),
        ("-/x", "the path is not absolute"),
        ("~/x", "the path is not absolute"),
        (".", "the path is not absolute"),
        ("/srv/a/..", "the path holds a '..' component"),
        ("/srv/../a", "the path holds a '..' component"),
        ("/..", "the path holds a '..' component"),
        ("/../a", "the path holds a '..' component"),
        ("/srv/a/../..", "the path holds a '..' component"),
        ("a/../b", "the path is not absolute"),
        ("/srv/../a\nb", "the path holds a line feed or a carriage return"),
    ],
)
def test_systemd_working_directory_refuses_a_folder(folder: str, rule: str) -> None:
    """red_at_pretip (d2146a5 has no such function). systemd 259.9 ends the value at a line feed
    and at a carriage return and accepts the unit with the cut directory, so both are refused
    here; so is a path that is not absolute. It refuses a value with a '..' component ("path is
    not normalized") unless the component follows the root directly; here every '..' component
    is refused, and those five rows fail against the modules of ca48926 too, which returned a
    value for them. The last two rows pin the order of the rules; they and the first eight rows
    pass against ca48926. The whole message is compared: the rule, and the path as repr, on one
    line."""
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.supervision import systemd_working_directory

    with pytest.raises(BridgeError) as caught:
        systemd_working_directory(Path(folder))
    message = str(caught.value)
    assert message == f"A systemd unit cannot name this working directory: {rule}: {folder!r}"
    assert "\n" not in message and "\r" not in message


@pytest.mark.parametrize(
    "folder",
    [
        "/t146-no-such-folder/plain",
        *(f"/t146-no-such-folder{name}" for name, _ in WORKING_DIRECTORY_VALUES[1:-1]),
    ],
)
def test_systemd_definition_writes_the_working_directory_once(folder: str) -> None:
    """red_at_pretip (d2146a5). Through the public constructor: the unit holds exactly one
    WorkingDirectory= line, and its value is the function's value for the resolved folder. The
    nine folders with dots pass against the modules of ca48926."""
    from codex_herdr_bridge.supervision import NativeSupervisor, systemd_working_directory

    supervisor = NativeSupervisor(Path(folder) / "config.json")
    assert str(supervisor.config_path.parent) == folder
    values = _working_directory_values(supervisor.systemd_definition())
    assert values == [systemd_working_directory(Path(folder))]
    assert values == [folder.replace("%", "%%") + "/."]


@pytest.mark.parametrize(
    ("given", "resolved"),
    [
        ("relative/x/config.json", "work/relative/x"),
        ("-/x/config.json", "work/-/x"),
        ("~/x/config.json", "home/x"),
        ("relative/../x/config.json", "work/x"),
    ],
)
def test_systemd_working_directory_of_a_relative_configuration_path_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str, resolved: str
) -> None:
    """red_at_pretip (d2146a5). The constructor resolves the configuration path against the working
    directory and HOME, so '-' and '~' never reach the unit as the first character, and a '..'
    in the middle of the given path is gone from the folder before the unit is written: that
    row does not raise, and it passes against the modules of ca48926."""
    from codex_herdr_bridge.supervision import NativeSupervisor, systemd_working_directory

    (tmp_path / "work").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    values = _working_directory_values(NativeSupervisor(Path(given)).systemd_definition())
    assert values == [systemd_working_directory(tmp_path.resolve() / resolved)]
    assert values[0].startswith("/")
    assert values[0].endswith("/" + resolved.split("/", 1)[1] + "/.")


def test_systemd_definition_refuses_a_folder_with_a_line_feed() -> None:
    """pins_existing. d2146a5 refused this folder too, with another text."""
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.supervision import NativeSupervisor

    supervisor = NativeSupervisor(Path("/t146-no-such-folder/a\nb/config.json"))
    with pytest.raises(BridgeError):
        supervisor.systemd_definition()


def test_systemd_definition_refuses_a_folder_with_a_carriage_return() -> None:
    """red_at_pretip. systemd 259.9 accepted the unit of d2146a5 with the directory cut at the
    carriage return."""
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.supervision import NativeSupervisor

    supervisor = NativeSupervisor(Path("/t146-no-such-folder/a\rb/config.json"))
    with pytest.raises(BridgeError, match="line feed or a carriage return"):
        supervisor.systemd_definition()


# --- Reboot survival: the service definition lives where the OS reloads it at login ---
#
# Every test below runs against a fake HOME under tmp_path and a recorded fake `run`;
# nothing here may touch the real launchctl, systemctl, ~/Library/LaunchAgents or
# ~/.config/herdr-bridge. A test that would is a defect in the test.


class RecordedLaunchctl:
    """Fake `run` for launchctl: records argv and models the gui domain's registrations."""

    def __init__(self, registered: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.registered = set(registered)
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        from codex_herdr_bridge.process import CommandError

        self.calls.append(list(args))
        assert args[0] == "launchctl"
        verb = args[1]
        if verb == "print":
            if args[2] in self.registered:
                return Output("state = running", "")
            raise CommandError(113, "Could not find service")
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


class RecordedSystemctl:
    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        self.calls.append(list(args))
        assert args[:2] == ["systemctl", "--user"]
        return Output("", "")

    def __init__(self) -> None:
        self.calls: list[list[str]] = []


def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


def darwin_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launchctl: RecordedLaunchctl
) -> "NativeSupervisor":
    from codex_herdr_bridge.supervision import NativeSupervisor

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "darwin")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", launchctl)
    config = tmp_path / "cfg" / "config.json"
    Config("explicit-session").save(config)
    return NativeSupervisor(config)


def launch_agent_path(home: Path, label: str) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


async def test_launchd_start_installs_the_plist_under_launch_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    target = f"gui/{os.getuid()}/{supervisor.label}"

    await supervisor.start()

    path = launch_agent_path(home, supervisor.label)
    assert path.is_file(), "the plist must live where launchd reloads it at login"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert (home / "Library").stat().st_mode & 0o777 == 0o700
    assert not (supervisor.root / f"{supervisor.label}.plist").exists()
    document = plistlib.loads(path.read_bytes())
    assert document["Label"] == supervisor.label
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)] in launchctl.calls
    assert launchctl.verbs() == ["bootstrap"]
    assert target in launchctl.registered
    marker = json.loads(supervisor.marker.read_text())
    assert marker["manager"] == "launchd"
    assert marker["label"] == supervisor.label
    assert marker["definition"] == str(path)


async def test_launchd_start_keeps_an_existing_launch_agents_directory_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    agents.chmod(0o755)
    supervisor = darwin_supervisor(tmp_path, monkeypatch, RecordedLaunchctl())

    await supervisor.start()

    assert launch_agent_path(home, supervisor.label).is_file()
    assert agents.stat().st_mode & 0o777 == 0o755, "an owner's existing directory is left alone"


async def test_launchd_start_migrates_a_runtime_directory_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live 0.10.0 layout: marker without a definition path, plist in runtime/, job loaded."""
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    target = f"gui/{os.getuid()}/{supervisor.label}"
    supervisor.root.mkdir(mode=0o700)
    old_plist = supervisor.root / f"{supervisor.label}.plist"
    old_plist.write_bytes(supervisor.launchd_definition())
    supervisor.marker.write_text(json.dumps({"manager": "launchd", "label": supervisor.label}))
    launchctl.registered.add(target)

    await supervisor.start()

    new_plist = launch_agent_path(home, supervisor.label)
    assert not old_plist.exists(), "the runtime-directory plist is gone after migration"
    assert new_plist.is_file()
    assert launchctl.verbs() == ["bootout", "bootstrap"], launchctl.calls
    assert ["launchctl", "bootout", target] in launchctl.calls
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(new_plist)] in launchctl.calls
    assert target in launchctl.registered
    marker = json.loads(supervisor.marker.read_text())
    assert marker == {"manager": "launchd", "label": supervisor.label, "definition": str(new_plist)}


async def test_launchd_start_kickstarts_a_job_registered_at_the_launch_agents_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    target = f"gui/{os.getuid()}/{supervisor.label}"
    path = launch_agent_path(home, supervisor.label)
    path.parent.mkdir(parents=True)
    path.write_bytes(supervisor.launchd_definition())
    supervisor.root.mkdir(mode=0o700)
    supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(path)})
    )
    launchctl.registered.add(target)

    await supervisor.start()

    assert launchctl.verbs() == ["kickstart"], launchctl.calls
    assert ["launchctl", "kickstart", target] in launchctl.calls
    assert path.is_file()
    assert json.loads(supervisor.marker.read_text())["definition"] == str(path)


async def test_launchd_start_replaces_a_legacy_label_with_a_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    assert supervisor.legacy_label is not None
    legacy_target = f"gui/{os.getuid()}/{supervisor.legacy_label}"
    supervisor.root.mkdir(mode=0o700)
    legacy_plist = supervisor.root / f"{supervisor.legacy_label}.plist"
    legacy = plistlib.loads(supervisor.launchd_definition())
    legacy["Label"] = supervisor.legacy_label
    legacy_plist.write_bytes(plistlib.dumps(legacy))
    launchctl.registered.add(legacy_target)
    assert supervisor.legacy_installed() is True

    await supervisor.start()

    new_plist = launch_agent_path(home, supervisor.label)
    assert launchctl.verbs() == ["bootout", "bootstrap"], launchctl.calls
    assert ["launchctl", "bootout", legacy_target] in launchctl.calls
    assert not legacy_plist.exists()
    assert new_plist.is_file()
    assert launchctl.registered == {f"gui/{os.getuid()}/{supervisor.label}"}
    assert json.loads(supervisor.marker.read_text())["label"] == supervisor.label


def linux_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, systemctl: RecordedSystemctl
) -> "NativeSupervisor":
    from codex_herdr_bridge.supervision import NativeSupervisor

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "linux")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", systemctl)
    config = tmp_path / "cfg" / "config.json"
    Config("explicit-session").save(config)
    return NativeSupervisor(config)


async def test_systemd_start_enables_the_unit_by_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home(tmp_path, monkeypatch)
    systemctl = RecordedSystemctl()
    supervisor = linux_supervisor(tmp_path, monkeypatch, systemctl)

    await supervisor.start()

    unit = supervisor.root / f"{supervisor.label}.service"
    assert unit.is_file()
    assert systemctl.calls == [
        ["systemctl", "--user", "enable", str(unit)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "start", unit.name],
    ], "enable (which links) replaces the bare link so the unit starts at the next login"
    assert "link" not in [call[2] for call in systemctl.calls]
    marker = json.loads(supervisor.marker.read_text())
    assert marker == {"manager": "systemd", "label": supervisor.label, "definition": str(unit)}


async def test_systemd_stop_disables_and_removes_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home(tmp_path, monkeypatch)
    systemctl = RecordedSystemctl()
    supervisor = linux_supervisor(tmp_path, monkeypatch, systemctl)
    await supervisor.start()
    unit = supervisor.root / f"{supervisor.label}.service"
    systemctl.calls.clear()

    await supervisor.stop_if_installed()

    assert systemctl.calls == [
        ["systemctl", "--user", "stop", unit.name],
        ["systemctl", "--user", "disable", unit.name],
    ]
    assert not unit.exists()
    assert not supervisor.marker.exists()


async def test_launchd_stop_removes_the_plist_recorded_in_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    target = f"gui/{os.getuid()}/{supervisor.label}"
    path = launch_agent_path(home, supervisor.label)
    path.parent.mkdir(parents=True)
    path.write_bytes(supervisor.launchd_definition())
    supervisor.root.mkdir(mode=0o700)
    stale = supervisor.root / f"{supervisor.label}.plist"
    stale.write_bytes(supervisor.launchd_definition())
    supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(path)})
    )
    launchctl.registered.add(target)

    await supervisor.stop_if_installed()

    assert ["launchctl", "bootout", target] in launchctl.calls
    assert launchctl.registered == set(), "no loaded job is left behind"
    assert not path.exists(), "the LaunchAgents plist is removed"
    assert not stale.exists(), "nor is a stale runtime-directory copy"
    assert not supervisor.marker.exists()


async def test_launchd_stop_refuses_a_foreign_plist_at_the_recorded_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_home(tmp_path, monkeypatch)
    launchctl = RecordedLaunchctl()
    supervisor = darwin_supervisor(tmp_path, monkeypatch, launchctl)
    path = launch_agent_path(home, supervisor.label)
    path.parent.mkdir(parents=True)
    foreign = plistlib.loads(supervisor.launchd_definition())
    foreign["ProgramArguments"] = ["/usr/bin/python3", "-m", "something_else", "serve"]
    path.write_bytes(plistlib.dumps(foreign))
    supervisor.root.mkdir(mode=0o700)
    supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(path)})
    )
    launchctl.registered.add(f"gui/{os.getuid()}/{supervisor.label}")
    from codex_herdr_bridge.process import BridgeError

    with pytest.raises(BridgeError, match="cannot be verified"):
        await supervisor.stop_if_installed()

    assert launchctl.calls == [], "nothing is booted out on a refused definition"
    assert path.is_file()
    assert supervisor.marker.exists()


def test_non_object_supervisor_marker_is_refused_by_reinstall_check(tmp_path: Path) -> None:
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.supervision import NativeSupervisor

    supervisor = NativeSupervisor(tmp_path / "config.json")
    supervisor.root.mkdir(parents=True)
    supervisor.marker.write_text("[]")

    with pytest.raises(BridgeError, match="Supervisor marker must be an object"):
        supervisor.reinstall_needed()


def test_systemd_definition_installs_into_the_user_session_target(tmp_path: Path) -> None:
    from codex_herdr_bridge.supervision import NativeSupervisor

    definition = NativeSupervisor(tmp_path / "config.json").systemd_definition()
    # `systemctl --user enable` only links a unit with no [Install] section.
    assert definition.endswith("\n[Install]\nWantedBy=default.target\n")
