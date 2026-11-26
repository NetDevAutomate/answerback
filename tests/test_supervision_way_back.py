"""The way back in the service switch from the legacy label to the current one (T-144).

Every test runs against a fake HOME under tmp_path and a recorded fake `run`; nothing
here may touch the real launchctl, systemctl, ~/Library/LaunchAgents or a real config
directory. Each test says in its docstring whether it is `red_at_pretip` (it fails at
the commit this row started from) or `pins_existing` (it passes there on purpose).
"""

import asyncio
import json
import os
import plistlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError, CommandError, Output
from answerback_bridge.supervision import NativeSupervisor

Fails = Callable[[list[str]], bool]


def never(args: list[str]) -> bool:
    return False


def present(path: str | Path) -> bool:
    return Path(path).is_file()


class Launchctl:
    """Fake `run` for launchctl that models registrations and can fail chosen calls."""

    def __init__(self) -> None:
        self.registered: set[str] = set()
        self.calls: list[list[str]] = []
        self.fails: Fails = never
        # A bootstrap that fails after launchd has taken the job, when True.
        self.registers_before_failing = False
        self.both_registered = False
        # The one object a cancelled call raises: before it acts, or after it did.
        self.cancellation = asyncio.CancelledError()
        self.cancel_before: Fails = never
        self.cancel_after: Fails = never

    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        self.calls.append(list(args))
        assert args[0] == "launchctl"
        if self.cancel_before(args):
            raise self.cancellation
        output = self.answer(args)
        if self.cancel_after(args):
            raise self.cancellation
        return output

    def answer(self, args: list[str]) -> Output:
        verb = args[1]
        if verb == "print":
            if args[2] in self.registered:
                return Output("state = running", "")
            raise CommandError(113, "Could not find service")
        if verb == "bootout":
            if self.fails(args):
                raise CommandError(1, f"refused bootout {args[2]}")
            self.registered.discard(args[2])
            return Output("", "")
        if verb == "bootstrap":
            path = Path(args[3])
            failing = self.fails(args)
            if not present(path):
                raise CommandError(5, f"no such file {path}")
            if not failing or self.registers_before_failing:
                self.registered.add(f"{args[2]}/{path.stem}")
                self.both_registered |= len(self.registered) > 1
            if failing:
                raise CommandError(5, f"refused bootstrap {path.name}")
            return Output("", "")
        assert verb == "kickstart"
        return Output("", "")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if call[1] != "print"]


class Systemctl:
    """Fake `run` for systemctl --user with the answers of a real user manager.

    The exit codes and texts are the ones systemd 259.9 gave on 2026-11-21. A unit is
    known to systemd only while its file is linked or enabled (`enabled`); a name in
    `refused` is a unit whose file systemd reads and refuses (`LoadState=bad-setting`).
    """

    def __init__(self) -> None:
        self.started: set[str] = set()
        self.enabled: set[str] = set()
        self.refused: set[str] = set()
        self.calls: list[list[str]] = []
        # A call that fails with exit 1: before it acts, or after it did.
        self.fails: Fails = never
        self.fails_after: Fails = never
        # The one object a cancelled call raises: before it acts, or after it did.
        self.cancellation = asyncio.CancelledError()
        self.cancel_before: Fails = never
        self.cancel_after: Fails = never
        # An answer of `show` that holds neither of the two lines, when True.
        self.unreadable_show = False
        # The (LoadState, ActiveState) that `show` answers for one unit, whatever the
        # fake's own state says: the answers the model below cannot give.
        self.shows: dict[str, tuple[str, str]] = {}
        self.both_started = False

    def load_state(self, unit: str) -> str:
        if unit in self.enabled:
            return "bad-setting" if unit in self.refused else "loaded"
        return "loaded" if unit in self.started else "not-found"

    async def __call__(self, args: list[str], **kwargs: Any) -> Output:
        self.calls.append(list(args))
        assert args[:2] == ["systemctl", "--user"]
        refusal = CommandError(1, f"refused {args[2]} {Path(args[-1]).name}")
        if self.cancel_before(args):
            raise self.cancellation
        if self.fails(args):
            raise refusal
        output = self.answer(args)
        if self.cancel_after(args):
            raise self.cancellation
        if self.fails_after(args):
            raise refusal
        return output

    def answer(self, args: list[str]) -> Output:
        verb = args[2]
        if verb == "daemon-reload":
            assert len(args) == 3
            return Output("", "")
        if verb == "enable":
            # Exit 0 also for a unit that systemd then refuses.
            if not present(args[3]):
                raise CommandError(1, f"no such unit file {args[3]}")
            self.enabled.add(Path(args[3]).name)
            return Output("", "")
        unit = args[-1]
        state = self.load_state(unit)
        if verb == "disable":
            if unit not in self.enabled:
                raise CommandError(1, f"Failed to disable unit: Unit {unit} does not exist")
            self.enabled.discard(unit)
        elif verb == "start":
            if state == "bad-setting":
                raise CommandError(
                    1, f"Failed to start {unit}: Unit {unit} has a bad unit file setting."
                )
            if state == "not-found":
                raise CommandError(5, f"Failed to start {unit}: Unit {unit} not found.")
            self.started.add(unit)
            self.both_started |= len(self.started) > 1
        elif verb == "stop":
            if state != "loaded" and unit not in self.started:
                raise CommandError(5, f"Failed to stop {unit}: Unit {unit} not loaded.")
            self.started.discard(unit)
        else:
            assert args[2:-1] == ["show", "-p", "LoadState", "-p", "ActiveState"]
            if self.unreadable_show:
                return Output("", "")
            active = "active" if unit in self.started else "inactive"
            state, active = self.shows.get(unit, (state, active))
            return Output(f"LoadState={state}\nActiveState={active}\n", "")
        return Output("", "")

    def tail(self) -> list[list[str]]:
        return [call[2:] for call in self.calls]


def show(unit: str) -> list[str]:
    return ["show", "-p", "LoadState", "-p", "ActiveState", unit]


def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


def supervisor_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, run: Any
) -> NativeSupervisor:
    monkeypatch.setattr("answerback_bridge.supervision.sys.platform", platform)
    monkeypatch.setattr("answerback_bridge.supervision.run", run)
    config = tmp_path / "cfg" / "config.json"
    Config("explicit-session").save(config)
    supervisor = NativeSupervisor(config)
    supervisor.root.mkdir(mode=0o700)
    return supervisor


class Darwin:
    """An owned legacy launchd service: its plist in the runtime directory, registered."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = fake_home(tmp_path, monkeypatch)
        self.launchctl = Launchctl()
        self.supervisor = supervisor_for(tmp_path, monkeypatch, "darwin", self.launchctl)
        legacy_label = self.supervisor.legacy_label
        assert legacy_label is not None
        self.legacy_label = legacy_label
        self.domain = f"gui/{os.getuid()}"
        self.legacy_target = f"{self.domain}/{legacy_label}"
        self.new_target = f"{self.domain}/{self.supervisor.label}"
        self.legacy_plist = self.supervisor.root / f"{legacy_label}.plist"
        document = plistlib.loads(self.supervisor.launchd_definition())
        document["Label"] = legacy_label
        self.legacy_bytes = plistlib.dumps(document)
        self.legacy_plist.write_bytes(self.legacy_bytes)
        self.new_plist = self.home / "Library" / "LaunchAgents" / f"{self.supervisor.label}.plist"
        self.launchctl.registered.add(self.legacy_target)

    def fail_new_bootstrap(self) -> None:
        self.launchctl.fails = lambda args: (
            args[1] == "bootstrap" and args[3] == str(self.new_plist)
        )

    def marker(self) -> dict[str, Any]:
        return json.loads(self.supervisor.marker.read_text())

    def assert_switched(self) -> None:
        assert self.launchctl.registered == {self.new_target}
        assert not self.legacy_plist.exists()
        assert self.new_plist.is_file()
        assert self.marker() == {
            "manager": "launchd",
            "label": self.supervisor.label,
            "definition": str(self.new_plist),
        }
        assert not self.launchctl.both_registered

    def assert_legacy_back(self) -> None:
        assert self.launchctl.registered == {self.legacy_target}
        assert self.legacy_plist.read_bytes() == self.legacy_bytes
        assert not self.new_plist.exists()
        assert self.marker()["label"] == self.legacy_label
        assert not self.launchctl.both_registered


class Linux:
    """An owned legacy systemd unit: its file in the runtime directory, enabled and started."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home(tmp_path, monkeypatch)
        self.systemctl = Systemctl()
        self.supervisor = supervisor_for(tmp_path, monkeypatch, "linux", self.systemctl)
        legacy_label = self.supervisor.legacy_label
        assert legacy_label is not None
        self.legacy_label = legacy_label
        self.legacy_unit = self.supervisor.root / f"{legacy_label}.service"
        self.legacy_bytes = (
            self.supervisor.systemd_definition()
            .replace("AnswerBack collaboration bridge", "Legacy bridge")
            .encode()
        )
        self.legacy_unit.write_bytes(self.legacy_bytes)
        self.new_unit = self.supervisor.root / f"{self.supervisor.label}.service"
        self.systemctl.enabled.add(self.legacy_unit.name)
        self.systemctl.started.add(self.legacy_unit.name)

    def fail_new(self, verb: str) -> None:
        self.systemctl.fails = lambda args: (
            args[2] == verb and Path(args[-1]).name == self.new_unit.name
        )

    def marker(self) -> dict[str, Any]:
        return json.loads(self.supervisor.marker.read_text())

    def assert_switched(self) -> None:
        assert self.systemctl.started == {self.new_unit.name}
        assert self.systemctl.enabled == {self.new_unit.name}
        assert not self.legacy_unit.exists()
        assert self.new_unit.is_file()
        assert self.marker() == {
            "manager": "systemd",
            "label": self.supervisor.label,
            "definition": str(self.new_unit),
        }
        assert not self.systemctl.both_started

    def assert_legacy_back(self) -> None:
        assert self.systemctl.started == {self.legacy_unit.name}
        assert self.systemctl.enabled == {self.legacy_unit.name}
        assert self.legacy_unit.read_bytes() == self.legacy_bytes
        assert not self.new_unit.exists()
        assert self.marker()["label"] == self.legacy_label
        assert not self.systemctl.both_started


# --- R1: the switch that succeeds is unchanged ---


async def test_r1_launchd_switch_that_succeeds_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: same launchctl calls in the same order, same files, same marker."""
    case = Darwin(tmp_path, monkeypatch)
    stale_copy = case.supervisor.root / f"{case.legacy_label}.plist"

    await case.supervisor.start()

    assert case.launchctl.calls == [
        ["launchctl", "print", case.legacy_target],
        ["launchctl", "bootout", case.legacy_target],
        ["launchctl", "print", case.legacy_target],
        ["launchctl", "print", case.new_target],
        ["launchctl", "bootstrap", case.domain, str(case.new_plist)],
    ]
    assert not stale_copy.exists()
    case.assert_switched()


async def test_r1_launchd_switch_removes_a_recorded_definition_and_the_runtime_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: a legacy marker with a definition path elsewhere; both files go."""
    case = Darwin(tmp_path, monkeypatch)
    recorded = tmp_path / "elsewhere" / f"{case.legacy_label}.plist"
    recorded.parent.mkdir()
    recorded.write_bytes(case.legacy_bytes)
    case.supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": case.legacy_label, "definition": str(recorded)})
    )

    await case.supervisor.start()

    assert not recorded.exists()
    case.assert_switched()


async def test_r1_systemd_switch_that_succeeds_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: same systemctl calls in the same order, same files, same marker."""
    case = Linux(tmp_path, monkeypatch)

    await case.supervisor.start()

    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
        ["enable", str(case.new_unit)],
        ["daemon-reload"],
        ["start", case.new_unit.name],
    ]
    assert "show" not in [call[0] for call in case.systemctl.tail()]
    case.assert_switched()


# --- R2, R4, R5: macOS, the new service does not start ---


async def test_r2_launchd_failed_bootstrap_registers_the_legacy_service_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: the legacy plist and marker were deleted before the new start."""
    case = Darwin(tmp_path, monkeypatch)
    case.fail_new_bootstrap()

    with pytest.raises(BridgeError) as raised:
        await case.supervisor.start()

    case.assert_legacy_back()
    assert case.launchctl.verbs() == ["bootout", "bootstrap", "bootstrap"]
    assert case.launchctl.calls[-1] == [
        "launchctl",
        "bootstrap",
        case.domain,
        str(case.legacy_plist),
    ]
    # R4: the failure of the new service, and that the previous one runs again.
    text = str(raised.value)
    assert "refused bootstrap" in text
    assert "previous service" in text and "runs again" in text
    assert case.legacy_label in text


async def test_r5_launchd_new_job_is_booted_out_before_the_legacy_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: a bootstrap that fails after launchd took the new job."""
    case = Darwin(tmp_path, monkeypatch)
    case.fail_new_bootstrap()
    case.launchctl.registers_before_failing = True

    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()

    case.assert_legacy_back()
    calls = case.launchctl.calls
    new_out = calls.index(["launchctl", "bootout", case.new_target])
    legacy_in = calls.index(["launchctl", "bootstrap", case.domain, str(case.legacy_plist)])
    assert calls.index(["launchctl", "bootout", case.legacy_target]) < calls.index(
        ["launchctl", "bootstrap", case.domain, str(case.new_plist)]
    )
    assert new_out < legacy_in, "the new job leaves launchd before the legacy one returns"


async def test_r4_launchd_way_back_that_fails_keeps_definition_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: both failures in the text; a later start can try again."""
    case = Darwin(tmp_path, monkeypatch)
    case.launchctl.fails = lambda args: args[1] == "bootstrap"

    with pytest.raises(BridgeError) as raised:
        await case.supervisor.start()

    text = str(raised.value)
    assert f"refused bootstrap {case.new_plist.name}" in text
    assert f"refused bootstrap {case.legacy_plist.name}" in text
    assert "previous service" in text and "is not running" in text
    assert text.endswith(". Its definition and the supervision record are kept; run start again")
    assert "runs again" not in text
    assert case.launchctl.registered == set()
    assert not case.launchctl.both_registered, "never both, also when the way back fails"
    assert case.legacy_plist.read_bytes() == case.legacy_bytes
    assert not case.new_plist.exists()
    assert case.marker()["label"] == case.legacy_label

    case.launchctl.fails = never
    await case.supervisor.start()
    case.assert_switched()


async def test_r5_launchd_legacy_is_not_started_when_the_new_job_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: a new job that stays registered blocks the legacy bootstrap."""
    case = Darwin(tmp_path, monkeypatch)
    case.launchctl.registers_before_failing = True
    case.launchctl.fails = lambda args: (
        args[1] == "bootstrap" or args[1:] == ["bootout", case.new_target]
    )

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused bootout" in str(raised.value)
    legacy_in = ["launchctl", "bootstrap", case.domain, str(case.legacy_plist)]
    assert legacy_in not in case.launchctl.calls
    assert not case.launchctl.both_registered
    assert case.legacy_plist.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


async def test_r4_launchd_historical_marker_without_a_definition_stays_for_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: no legacy plist remains, so there is no way back; the marker stays."""
    case = Darwin(tmp_path, monkeypatch)
    case.supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": case.legacy_label})
    )
    case.legacy_plist.unlink()
    case.fail_new_bootstrap()

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused bootstrap" in str(raised.value)
    assert "no definition of the previous service remains" in str(raised.value)
    # R15: nothing is said to be kept that is not there.
    assert "definition and the supervision record are kept" not in str(raised.value)
    assert str(raised.value).endswith(". The supervision record is kept; run start again")
    assert case.launchctl.registered == set()
    assert not case.new_plist.exists()
    assert case.marker()["label"] == case.legacy_label


# --- R3, R4, R5: Linux, the new service does not start ---


async def test_r3_systemd_failed_enable_starts_the_legacy_unit_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: systemd never saw the new unit; taking it out passes on `show` (R10)."""
    case = Linux(tmp_path, monkeypatch)
    case.fail_new("enable")

    with pytest.raises(BridgeError) as raised:
        await case.supervisor.start()

    case.assert_legacy_back()
    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
        ["enable", str(case.new_unit)],
        ["stop", case.new_unit.name],
        show(case.new_unit.name),
        ["disable", case.new_unit.name],
        show(case.new_unit.name),
        ["enable", str(case.legacy_unit)],
        ["daemon-reload"],
        ["start", case.legacy_unit.name],
    ]
    text = str(raised.value)
    assert "refused enable" in text
    assert "previous service" in text and "runs again" in text


async def test_r3_systemd_failed_start_stops_and_disables_the_new_unit_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: R3 and R5 through the order of the recorded calls."""
    case = Linux(tmp_path, monkeypatch)
    case.fail_new("start")

    with pytest.raises(BridgeError) as raised:
        await case.supervisor.start()

    case.assert_legacy_back()
    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
        ["enable", str(case.new_unit)],
        ["daemon-reload"],
        ["start", case.new_unit.name],
        ["stop", case.new_unit.name],
        ["disable", case.new_unit.name],
        ["enable", str(case.legacy_unit)],
        ["daemon-reload"],
        ["start", case.legacy_unit.name],
    ]
    text = str(raised.value)
    assert "refused start" in text
    assert "previous service" in text and "runs again" in text


async def test_r4_systemd_way_back_that_fails_keeps_definition_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: both failures in the text; a later start can try again."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.fails = lambda args: args[2] == "start"

    with pytest.raises(BridgeError) as raised:
        await case.supervisor.start()

    text = str(raised.value)
    assert f"refused start {case.new_unit.name}" in text
    assert f"refused start {case.legacy_unit.name}" in text
    assert "previous service" in text and "is not running" in text
    assert text.endswith(". Its definition and the supervision record are kept; run start again")
    assert "runs again" not in text
    assert case.systemctl.started == set()
    assert not case.systemctl.both_started, "never both, also when the way back fails"
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert not case.new_unit.exists()
    assert case.marker()["label"] == case.legacy_label

    case.systemctl.fails = never
    await case.supervisor.start()
    case.assert_switched()


async def test_r5_systemd_legacy_is_not_started_when_the_new_unit_cannot_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: a new unit that may still run blocks the legacy start."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.fails = lambda args: (
        args[2:]
        in (
            ["start", case.new_unit.name],
            ["stop", case.new_unit.name],
        )
    )

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused stop" in str(raised.value)
    assert ["start", case.legacy_unit.name] not in case.systemctl.tail()
    assert not case.systemctl.both_started
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


# --- R6: a second try works ---


async def test_r6_launchd_second_start_completes_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: after R2 a start with a working launchctl ends as R1."""
    case = Darwin(tmp_path, monkeypatch)
    case.fail_new_bootstrap()
    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()
    case.assert_legacy_back()
    assert case.supervisor.legacy_installed() is True

    case.launchctl.fails = never
    case.launchctl.calls.clear()
    await case.supervisor.start()

    assert case.launchctl.verbs() == ["bootout", "bootstrap"]
    case.assert_switched()


@pytest.mark.parametrize("verb", ["enable", "start"])
async def test_r6_systemd_second_start_completes_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """red_at_pretip: after R3 a start with a working systemctl ends as R1."""
    case = Linux(tmp_path, monkeypatch)
    case.fail_new(verb)
    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()
    case.assert_legacy_back()
    assert case.supervisor.legacy_installed() is True

    case.systemctl.fails = never
    await case.supervisor.start()

    case.assert_switched()


# --- R7: nothing else changes ---


@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_r7_failed_first_start_raises_the_original_error_and_leaves_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """pins_existing: without a legacy service the error passes through untouched."""
    fake_home(tmp_path, monkeypatch)
    failure = CommandError(1, "service manager refused")

    async def run(args: list[str], **kwargs: Any) -> Output:
        if args[1] == "print":
            raise CommandError(113, "Could not find service")
        raise failure

    supervisor = supervisor_for(tmp_path, monkeypatch, platform, run)

    with pytest.raises(CommandError) as raised:
        await supervisor.start()

    assert raised.value is failure
    assert "previous service" not in str(raised.value)
    assert not supervisor.marker.exists()


async def test_r7_stop_of_a_legacy_launchd_service_removes_marker_and_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: `stop` and `uninstall` on a legacy service behave as before."""
    case = Darwin(tmp_path, monkeypatch)
    case.supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": case.legacy_label})
    )

    await case.supervisor.stop_if_installed()

    assert case.launchctl.calls == [
        ["launchctl", "print", case.legacy_target],
        ["launchctl", "bootout", case.legacy_target],
        ["launchctl", "print", case.legacy_target],
    ]
    assert case.launchctl.registered == set()
    assert not case.legacy_plist.exists()
    assert not case.supervisor.marker.exists()


async def test_r7_stop_of_a_legacy_systemd_unit_removes_marker_and_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: `stop` and `uninstall` on a legacy unit behave as before."""
    case = Linux(tmp_path, monkeypatch)
    case.supervisor.marker.write_text(
        json.dumps({"manager": "systemd", "label": case.legacy_label})
    )

    await case.supervisor.stop_if_installed()

    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
    ]
    assert not case.legacy_unit.exists()
    assert not case.supervisor.marker.exists()


async def test_r7_supervisor_without_a_legacy_label_has_no_way_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: the shape of BrokerSupervisor (legacy label None) on a failed restart."""
    fake_home(tmp_path, monkeypatch)
    systemctl = Systemctl()
    supervisor = supervisor_for(tmp_path, monkeypatch, "linux", systemctl)
    supervisor.legacy_label = None
    await supervisor.start()
    unit = supervisor.root / f"{supervisor.label}.service"
    marker = supervisor.marker.read_text()
    systemctl.calls.clear()
    systemctl.fails = lambda args: args[2] == "start"

    with pytest.raises(CommandError, match="refused start") as raised:
        await supervisor.start()

    assert "previous service" not in str(raised.value)
    assert systemctl.tail() == [["enable", str(unit)], ["daemon-reload"], ["start", unit.name]]
    assert unit.is_file()
    assert supervisor.marker.read_text() == marker


# --- Fix round 1: a real systemd user manager (D1, D2, D3; R8, R9, R10, R13, R14) ---


async def test_d1_systemd_refused_new_unit_brings_the_legacy_unit_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: `stop` of a refused unit exits 5 and ended the way back (probe r1)."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.refused.add(case.new_unit.name)
    expected = [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
        ["enable", str(case.new_unit)],
        ["daemon-reload"],
        ["start", case.new_unit.name],
        ["stop", case.new_unit.name],
        show(case.new_unit.name),
        ["disable", case.new_unit.name],
        ["enable", str(case.legacy_unit)],
        ["daemon-reload"],
        ["start", case.legacy_unit.name],
    ]

    with pytest.raises(BridgeError, match="runs again") as raised:
        await case.supervisor.start()

    assert "has a bad unit file setting" in str(raised.value)
    assert "not loaded" not in str(raised.value)
    assert case.systemctl.tail() == expected
    case.assert_legacy_back()

    # As long as systemd refuses the new unit, every start ends the same way.
    case.systemctl.calls.clear()
    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()

    assert case.systemctl.tail() == expected
    case.assert_legacy_back()


def cancelled_before_enable(case: Linux) -> None:
    case.systemctl.cancel_before = lambda args: args[2:] == ["enable", str(case.new_unit)]


def cancelled_after_start(case: Linux) -> None:
    case.systemctl.cancel_after = lambda args: args[2:] == ["start", case.new_unit.name]


def way_back_fails_at_enable(case: Linux) -> None:
    case.systemctl.fails = lambda args: args[2] == "enable"


def new_unit_runs_and_cannot_be_stopped(case: Linux) -> None:
    case.systemctl.fails_after = lambda args: args[2:] == ["start", case.new_unit.name]
    case.systemctl.fails = lambda args: args[2:] == ["stop", case.new_unit.name]


@pytest.mark.parametrize(
    ("interrupt", "error", "left"),
    [
        pytest.param(cancelled_before_enable, asyncio.CancelledError, "", id="a"),
        pytest.param(cancelled_after_start, asyncio.CancelledError, "new", id="b"),
        pytest.param(way_back_fails_at_enable, BridgeError, "", id="c"),
        pytest.param(new_unit_runs_and_cannot_be_stopped, BridgeError, "new", id="d"),
    ],
)
async def test_r13_systemd_later_start_completes_the_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: Callable[[Linux], None],
    error: type[BaseException],
    left: str,
) -> None:
    """red_at_pretip: the later start ended at `stop <old>`, exit 5 (probe s4, s5, s6)."""
    case = Linux(tmp_path, monkeypatch)
    left_units = {case.new_unit.name} if left else set()
    interrupt(case)
    with pytest.raises(error):
        await case.supervisor.start()
    # What the interruption left: systemd has forgotten the old unit, the record names it.
    assert case.systemctl.started == left_units
    assert case.systemctl.enabled == left_units
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label

    case.systemctl.fails = case.systemctl.fails_after = never
    case.systemctl.cancel_before = case.systemctl.cancel_after = never
    case.systemctl.calls.clear()
    await case.supervisor.start()

    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        show(case.legacy_unit.name),
        ["disable", case.legacy_unit.name],
        show(case.legacy_unit.name),
        ["enable", str(case.new_unit)],
        ["daemon-reload"],
        ["start", case.new_unit.name],
    ]
    case.assert_switched()


@pytest.mark.parametrize("moment", ["cancel_before", "cancel_after"], ids=["e", "f"])
async def test_r13_launchd_later_start_completes_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    """pins_existing: a start cancelled before or right after `bootstrap` of the new job."""
    case = Darwin(tmp_path, monkeypatch)
    new_in = ["launchctl", "bootstrap", case.domain, str(case.new_plist)]
    setattr(case.launchctl, moment, lambda args: args == new_in)
    with pytest.raises(asyncio.CancelledError):
        await case.supervisor.start()
    left = {case.new_target} if moment == "cancel_after" else set()
    assert case.launchctl.registered == left
    assert case.legacy_plist.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label

    setattr(case.launchctl, moment, never)
    case.launchctl.calls.clear()
    await case.supervisor.start()

    # A job that the cancelled start left registered is kicked, not bootstrapped twice.
    assert case.launchctl.verbs() == ["kickstart" if left else "bootstrap"]
    case.assert_switched()


async def test_r8_new_unit_that_still_runs_blocks_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (through the `show` call): `stop` fails, `show` says loaded and active."""
    case = Linux(tmp_path, monkeypatch)
    new_unit_runs_and_cannot_be_stopped(case)

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused stop" in str(raised.value)
    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        ["disable", case.legacy_unit.name],
        ["enable", str(case.new_unit)],
        ["daemon-reload"],
        ["start", case.new_unit.name],
        ["stop", case.new_unit.name],
        show(case.new_unit.name),
    ]
    assert case.systemctl.started == {case.new_unit.name}
    assert not case.systemctl.both_started
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


async def test_r8_new_unit_that_cannot_be_disabled_blocks_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (through the `show` call): `disable` fails, `show` says loaded."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.fails = lambda args: (
        args[2:] in (["start", case.new_unit.name], ["disable", case.new_unit.name])
    )

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused disable" in str(raised.value)
    assert case.systemctl.tail()[5:] == [
        ["stop", case.new_unit.name],
        ["disable", case.new_unit.name],
        show(case.new_unit.name),
    ]
    assert case.systemctl.started == set()
    assert case.systemctl.enabled == {case.new_unit.name}
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


@pytest.mark.parametrize("verb", ["stop", "disable"])
async def test_r8_legacy_unit_that_cannot_be_taken_out_stops_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """red_at_pretip (through the `show` call): the error of `stop` or `disable` is raised."""
    case = Linux(tmp_path, monkeypatch)
    record = json.dumps({"manager": "systemd", "label": case.legacy_label})
    case.supervisor.marker.write_text(record)
    case.systemctl.fails = lambda args: args[2:] == [verb, case.legacy_unit.name]

    with pytest.raises(CommandError, match=f"refused {verb}") as raised:
        await case.supervisor.start()

    assert raised.value.returncode == 1
    assert "previous service" not in str(raised.value)
    taken = [["stop", case.legacy_unit.name], ["disable", case.legacy_unit.name]]
    assert case.systemctl.tail() == [
        *taken[: 1 if verb == "stop" else 2],
        show(case.legacy_unit.name),
    ]
    # Nothing is deleted, and nothing of the new service was written.
    assert case.systemctl.enabled == {case.legacy_unit.name}
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.supervisor.marker.read_text() == record
    assert not case.new_unit.exists()


@pytest.mark.parametrize("answer", ["fails", "unreadable"])
async def test_r8_failed_stop_is_raised_when_show_gives_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """red_at_pretip (through the `show` call): no tolerance without an answer of `show`."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.enabled.clear()
    case.systemctl.started.clear()
    if answer == "fails":
        case.systemctl.fails = lambda args: args[2] == "show"
    else:
        case.systemctl.unreadable_show = True

    with pytest.raises(CommandError, match="not loaded") as raised:
        await case.supervisor.start()

    assert raised.value.returncode == 5
    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        show(case.legacy_unit.name),
    ]
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


async def test_r10_new_unit_left_running_is_taken_out_before_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: an earlier attempt left the new unit running; this one fails at enable."""
    case = Linux(tmp_path, monkeypatch)
    cancelled_after_start(case)
    with pytest.raises(asyncio.CancelledError):
        await case.supervisor.start()
    assert case.systemctl.started == {case.new_unit.name}
    case.systemctl.cancel_after = never
    case.systemctl.calls.clear()
    case.fail_new("enable")

    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()

    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        show(case.legacy_unit.name),
        ["disable", case.legacy_unit.name],
        show(case.legacy_unit.name),
        ["enable", str(case.new_unit)],
        ["stop", case.new_unit.name],
        ["disable", case.new_unit.name],
        ["enable", str(case.legacy_unit)],
        ["daemon-reload"],
        ["start", case.legacy_unit.name],
    ]
    case.assert_legacy_back()


async def test_r10_enable_that_fails_after_linking_leaves_no_enabled_new_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: an `enable` that made its links and then failed (review F1)."""
    case = Linux(tmp_path, monkeypatch)
    case.systemctl.fails_after = lambda args: args[2:] == ["enable", str(case.new_unit)]

    with pytest.raises(BridgeError, match="runs again"):
        await case.supervisor.start()

    tail = case.systemctl.tail()
    assert tail.index(["disable", case.new_unit.name]) < tail.index(
        ["start", case.legacy_unit.name]
    )
    assert "show" not in [call[0] for call in tail]
    case.assert_legacy_back()


def unloaded_record(case: Linux, which: str) -> Path:
    """A record whose unit systemd has not loaded; returns the unit file it names."""
    case.systemctl.enabled.clear()
    case.systemctl.started.clear()
    if which == "legacy":
        case.supervisor.marker.write_text(
            json.dumps({"manager": "systemd", "label": case.legacy_label})
        )
        return case.legacy_unit
    case.legacy_unit.unlink()
    case.new_unit.write_text(case.supervisor.systemd_definition())
    case.supervisor.marker.write_text(
        json.dumps(
            {
                "manager": "systemd",
                "label": case.supervisor.label,
                "definition": str(case.new_unit),
            }
        )
    )
    return case.new_unit


@pytest.mark.parametrize("which", ["legacy", "current"])
async def test_r14_stop_removes_a_record_whose_unit_systemd_has_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """red_at_pretip: `stop` and `uninstall` failed with "not loaded" and kept everything."""
    case = Linux(tmp_path, monkeypatch)
    unit = unloaded_record(case, which)

    await case.supervisor.stop_if_installed()

    assert case.systemctl.tail() == [
        ["stop", unit.name],
        show(unit.name),
        ["disable", unit.name],
        show(unit.name),
    ]
    assert not unit.exists()
    assert not case.supervisor.marker.exists()


@pytest.mark.parametrize("refused", [False, True])
async def test_r14_stop_that_fails_on_a_running_unit_is_raised_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: bool
) -> None:
    """red_at_pretip (through the `show` call): loaded and active, or refused and active."""
    case = Linux(tmp_path, monkeypatch)
    record = json.dumps({"manager": "systemd", "label": case.legacy_label})
    case.supervisor.marker.write_text(record)
    if refused:
        # A unit that started before its file was changed into one systemd refuses.
        case.systemctl.refused.add(case.legacy_unit.name)
    case.systemctl.fails = lambda args: args[2] == "stop"

    with pytest.raises(CommandError, match="refused stop"):
        await case.supervisor.stop_if_installed()

    assert case.systemctl.tail() == [
        ["stop", case.legacy_unit.name],
        show(case.legacy_unit.name),
    ]
    assert case.systemctl.started == {case.legacy_unit.name}
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.supervisor.marker.read_text() == record


# --- Fix round 1: the record on the way back (D4; R11) ---


def manager_of(case: Darwin | Linux) -> str:
    return "launchd" if isinstance(case, Darwin) else "systemd"


def legacy_definition(case: Darwin | Linux) -> Path:
    return case.legacy_plist if isinstance(case, Darwin) else case.legacy_unit


def new_definition(case: Darwin | Linux) -> Path:
    return case.new_plist if isinstance(case, Darwin) else case.new_unit


def running(case: Darwin | Linux) -> set[str]:
    """The registered launchd jobs or the started systemd units, by label."""
    if isinstance(case, Darwin):
        return {target.rsplit("/", 1)[1] for target in case.launchctl.registered}
    return {unit.removesuffix(".service") for unit in case.systemctl.started}


def ever_together(case: Darwin | Linux) -> bool:
    if isinstance(case, Darwin):
        return case.launchctl.both_registered
    return case.systemctl.both_started


def recorded(case: Darwin | Linux) -> bytes:
    """Give the legacy service a record with a definition path and bytes of its own."""
    record = json.dumps(
        {
            "manager": manager_of(case),
            "label": case.legacy_label,
            "definition": str(legacy_definition(case)),
        },
        indent=1,
    ).encode()
    case.supervisor.marker.write_bytes(record)
    return record


def break_record_writes(
    monkeypatch: pytest.MonkeyPatch, case: Darwin | Linux, how: str
) -> PermissionError:
    """Wrap `supervision.atomic_write` for the writes of `supervisor.json`.

    The write of the new record, once: `replaced` is the real write and then the error
    (what `atomic_write` does when its temporary file cannot be removed), `missing`
    takes the record away and raises. `write_back_fails` is `replaced`, and every
    later write of the record raises without writing.
    """
    from answerback_bridge import supervision

    real = supervision.atomic_write
    failure = PermissionError("the temporary file of the record could not be removed")
    seen: list[str] = []

    def write(path: Path, text: str) -> None:
        if path != case.supervisor.marker:
            return real(path, text)
        seen.append(json.loads(text)["label"])
        if len(seen) == 1:
            assert seen == [case.supervisor.label]
            if how == "missing":
                path.unlink()
            else:
                real(path, text)
            raise failure
        if how == "write_back_fails":
            raise PermissionError("the record could not be written back")
        return real(path, text)

    monkeypatch.setattr("answerback_bridge.supervision.atomic_write", write)
    return failure


@pytest.mark.parametrize("how", ["replaced", "missing"])
@pytest.mark.parametrize("platform", [Darwin, Linux])
async def test_r11_way_back_restores_the_bytes_of_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: type[Darwin] | type[Linux],
    how: str,
) -> None:
    """red_at_pretip: the record named the new label next to a running old service (F2)."""
    case = platform(tmp_path, monkeypatch)
    record = recorded(case)
    break_record_writes(monkeypatch, case, how)

    with pytest.raises(BridgeError, match="runs again") as raised:
        await case.supervisor.start()

    assert "the temporary file of the record could not be removed" in str(raised.value)
    assert case.supervisor.marker.read_bytes() == record
    case.assert_legacy_back()
    assert running(case) == {case.legacy_label}

    # The wrapper fails one write only: this start has a record that can be written.
    await case.supervisor.start()

    case.assert_switched()
    assert running(case) == {case.supervisor.label}
    assert not ever_together(case)


@pytest.mark.parametrize("platform", [Darwin, Linux])
async def test_r11_record_that_cannot_be_written_back_blocks_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: type[Darwin] | type[Linux]
) -> None:
    """red_at_pretip: the old service was started under a record that names the new label."""
    case = platform(tmp_path, monkeypatch)
    recorded(case)
    break_record_writes(monkeypatch, case, "write_back_fails")

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    text = str(raised.value)
    assert "the temporary file of the record could not be removed" in text
    assert "the record could not be written back" in text
    assert "runs again" not in text
    # R15: the record is not the one the switch began with, and the text does not claim it.
    assert "supervision record are kept" not in text
    assert text.endswith(
        ". Its definition is kept, the supervision record could not be put back; run start again"
    )
    # The new service is out and its definition is gone; the old one was not started.
    assert running(case) == set()
    assert not ever_together(case)
    assert not new_definition(case).exists()
    assert legacy_definition(case).read_bytes() == case.legacy_bytes


# --- Fix round 1: cancellation (D5; R12) ---


@pytest.mark.parametrize("phase", ["switch", "way_back"])
async def test_r12_launchd_cancellation_leaves_start_as_the_same_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """pins_existing: no test failed when `except Exception` became `except BaseException`."""
    case = Darwin(tmp_path, monkeypatch)
    new_in = ["launchctl", "bootstrap", case.domain, str(case.new_plist)]
    legacy_in = ["launchctl", "bootstrap", case.domain, str(case.legacy_plist)]
    if phase == "switch":
        cancelled = new_in
    else:
        case.fail_new_bootstrap()
        cancelled = legacy_in
    case.launchctl.cancel_before = lambda args: args == cancelled

    with pytest.raises(asyncio.CancelledError) as raised:
        await case.supervisor.start()

    assert raised.value is case.launchctl.cancellation
    assert case.launchctl.calls[-1] == cancelled
    assert case.launchctl.calls.count(cancelled) == 1
    assert not case.launchctl.both_registered
    assert case.legacy_plist.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


@pytest.mark.parametrize("phase", ["switch", "way_back"])
async def test_r12_systemd_cancellation_leaves_start_as_the_same_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """pins_existing: no test failed when `except Exception` became `except BaseException`."""
    case = Linux(tmp_path, monkeypatch)
    if phase == "switch":
        cancelled = ["start", case.new_unit.name]
    else:
        case.fail_new("start")
        cancelled = ["enable", str(case.legacy_unit)]
    case.systemctl.cancel_before = lambda args: args[2:] == cancelled

    with pytest.raises(asyncio.CancelledError) as raised:
        await case.supervisor.start()

    assert raised.value is case.systemctl.cancellation
    assert case.systemctl.tail()[-1] == cancelled
    assert case.systemctl.tail().count(cancelled) == 1
    assert not case.systemctl.both_started
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


@pytest.mark.parametrize("phase", ["switch", "way_back"])
async def test_r12_systemd_cancelled_show_is_not_taken_for_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """red_at_pretip (there is no `show` there): a cancelled `show` is not a failed one."""
    case = Linux(tmp_path, monkeypatch)
    if phase == "switch":
        # systemd has forgotten the old unit: its `stop` fails and `show` is asked.
        case.systemctl.enabled.clear()
        case.systemctl.started.clear()
        cancelled = show(case.legacy_unit.name)
    else:
        case.fail_new("enable")
        cancelled = show(case.new_unit.name)
    case.systemctl.cancel_before = lambda args: args[2] == "show"

    with pytest.raises(asyncio.CancelledError) as raised:
        await case.supervisor.start()

    assert raised.value is case.systemctl.cancellation
    assert case.systemctl.tail()[-1] == cancelled
    assert case.systemctl.tail().count(cancelled) == 1
    assert not case.systemctl.both_started
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert case.marker()["label"] == case.legacy_label


# --- Fix round 1: the text of a way back that fails (D6; R15) ---


async def test_r15_systemd_historical_record_without_a_definition_says_what_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: the error said that a definition is kept when none remains (F7)."""
    case = Linux(tmp_path, monkeypatch)
    record = json.dumps({"manager": "systemd", "label": case.legacy_label})
    case.supervisor.marker.write_text(record)
    case.legacy_unit.unlink()
    case.fail_new("start")

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    text = str(raised.value)
    assert "refused start" in text
    assert "no definition of the previous service remains" in text
    assert "definition and the supervision record are kept" not in text
    assert text.endswith(". The supervision record is kept; run start again")
    assert case.systemctl.started == set()
    assert not case.systemctl.both_started
    assert not case.new_unit.exists()
    assert case.supervisor.marker.read_text() == record


async def test_r15_no_definition_and_no_record_says_that_nothing_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: neither the definition nor the record of the old service is as it was."""
    case = Linux(tmp_path, monkeypatch)
    case.supervisor.marker.write_text(
        json.dumps({"manager": "systemd", "label": case.legacy_label})
    )
    case.legacy_unit.unlink()
    break_record_writes(monkeypatch, case, "write_back_fails")

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    text = str(raised.value)
    assert "are kept" not in text and "Its definition" not in text
    assert "The supervision record is kept" not in text
    assert text.endswith(
        ". Neither its definition nor the supervision record is kept; run start again"
    )
    assert case.systemctl.started == set()


# --- Fix round 2: what `show` must say after a failure (review X1, X2, X3; E1, E2, E3) ---

NOT_INACTIVE = ["active", "activating", "deactivating", "reloading", "failed"]


def raise_at(
    monkeypatch: pytest.MonkeyPatch, case: Linux, raises: dict[tuple[str, str], Exception]
) -> None:
    """Put a `run` before the fake that records a call of (verb, unit) and raises its object."""

    async def run(args: list[str], **kwargs: Any) -> Output:
        error = raises.get((args[2], Path(args[-1]).name))
        if error is None:
            return await case.systemctl(args, **kwargs)
        case.systemctl.calls.append(list(args))
        raise error

    monkeypatch.setattr("answerback_bridge.supervision.run", run)


@pytest.mark.parametrize("active", NOT_INACTIVE)
async def test_e1_failed_disable_of_a_unit_that_is_not_inactive_blocks_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: str
) -> None:
    """red_at_pretip: `not-found` alone passed, and the old unit was started next to it."""
    case = Linux(tmp_path, monkeypatch)
    record = recorded(case)
    new = case.new_unit.name

    def fails(args: list[str]) -> bool:
        if args[2:] == ["disable", new] and active != "failed":
            # Something else started the unit again after its `stop` (review X1).
            case.systemctl.started.add(new)
        return args[2:] in (["start", new], ["disable", new])

    case.systemctl.fails = fails
    case.systemctl.shows[new] = ("not-found", active)

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused disable" in str(raised.value)
    assert case.systemctl.tail()[5:] == [["stop", new], ["disable", new], show(new)]
    assert ["start", case.legacy_unit.name] not in case.systemctl.tail()
    assert not case.systemctl.both_started
    assert case.supervisor.marker.read_bytes() == record
    assert case.legacy_unit.read_bytes() == case.legacy_bytes


async def test_e1_stop_deletes_nothing_when_the_unit_runs_after_a_failed_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: the record and the definition of a unit that runs were deleted."""
    case = Linux(tmp_path, monkeypatch)
    record = recorded(case)
    unit = case.legacy_unit.name
    original = CommandError(1, f"Failed to disable unit: Unit {unit} does not exist")
    raise_at(monkeypatch, case, {("disable", unit): original})
    case.systemctl.shows[unit] = ("not-found", "active")

    with pytest.raises(CommandError) as raised:
        await case.supervisor.stop_if_installed()

    assert raised.value is original
    assert case.systemctl.tail() == [["stop", unit], ["disable", unit], show(unit)]
    assert case.supervisor.marker.read_bytes() == record
    assert case.legacy_unit.read_bytes() == case.legacy_bytes


def show_cannot_be_made(
    monkeypatch: pytest.MonkeyPatch, case: Linux, verb: str, unit: str
) -> CommandError:
    """`verb` of the unit fails, and the program of the `show` that follows cannot be started."""
    original = CommandError(1, f"refused {verb} {unit}")
    unstartable = PermissionError(13, "cannot start systemctl for show")
    raise_at(monkeypatch, case, {(verb, unit): original, ("show", unit): unstartable})
    return original


@pytest.mark.parametrize("entry", ["take_out", "start", "stop_if_installed"])
@pytest.mark.parametrize("verb", ["stop", "disable"])
async def test_e2_show_that_cannot_be_made_leaves_the_failure_as_the_same_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, entry: str
) -> None:
    """red_at_pretip: the PermissionError of `show` was raised instead of the failure (X2)."""
    case = Linux(tmp_path, monkeypatch)
    record = recorded(case)
    unit = case.legacy_unit.name
    original = show_cannot_be_made(monkeypatch, case, verb, unit)

    with pytest.raises(CommandError) as caught:
        if entry == "take_out":
            await case.supervisor._take_out(unit)
        elif entry == "start":
            await case.supervisor.start()
        else:
            await case.supervisor.stop_if_installed()

    assert caught.value is original
    taken = [["stop", unit], ["disable", unit]]
    # The calls end at `show`: no `disable`, `enable` or `start` after it.
    assert case.systemctl.tail() == [*taken[: 1 if verb == "stop" else 2], show(unit)]
    assert case.supervisor.marker.read_bytes() == record
    assert case.legacy_unit.read_bytes() == case.legacy_bytes
    assert not case.new_unit.exists()


@pytest.mark.parametrize("verb", ["stop", "disable"])
async def test_e2_way_back_names_the_failure_and_not_the_show_that_could_not_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """red_at_pretip: the error held the PermissionError of `show`, not the failed call (X2)."""
    case = Linux(tmp_path, monkeypatch)
    record = recorded(case)
    new = case.new_unit.name
    case.fail_new("start")
    original = show_cannot_be_made(monkeypatch, case, verb, new)

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert f"starting it again failed too: {original}." in str(raised.value)
    assert "cannot start systemctl" not in str(raised.value)
    taken = [["stop", new], ["disable", new]]
    assert case.systemctl.tail()[5:] == [*taken[: 1 if verb == "stop" else 2], show(new)]
    assert not case.systemctl.both_started
    assert case.supervisor.marker.read_bytes() == record
    assert case.legacy_unit.read_bytes() == case.legacy_bytes


@pytest.mark.parametrize("active", NOT_INACTIVE)
@pytest.mark.parametrize("load", ["not-found", "bad-setting"])
async def test_e3_failed_stop_of_a_unit_that_is_not_inactive_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load: str, active: str
) -> None:
    """pins_existing: no test failed when only `active` after a failed `stop` was refused (X3)."""
    case = Linux(tmp_path, monkeypatch)
    unit = case.legacy_unit.name
    original = CommandError(1, f"refused stop {unit}")
    raise_at(monkeypatch, case, {("stop", unit): original})
    case.systemctl.shows[unit] = (load, active)

    with pytest.raises(CommandError) as caught:
        await case.supervisor._take_out(unit)

    assert caught.value is original
    assert case.systemctl.tail() == [["stop", unit], show(unit)]


async def test_e3_new_unit_that_is_still_deactivating_blocks_the_legacy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: `stop` fails, `show` says bad-setting and deactivating; no test had it."""
    case = Linux(tmp_path, monkeypatch)
    record = recorded(case)
    new = case.new_unit.name
    new_unit_runs_and_cannot_be_stopped(case)
    case.systemctl.shows[new] = ("bad-setting", "deactivating")

    with pytest.raises(BridgeError, match="is not running") as raised:
        await case.supervisor.start()

    assert "refused stop" in str(raised.value)
    assert case.systemctl.tail()[5:] == [["stop", new], show(new)]
    assert ["start", case.legacy_unit.name] not in case.systemctl.tail()
    assert case.systemctl.started == {new}
    assert not case.systemctl.both_started
    assert case.supervisor.marker.read_bytes() == record
    assert case.legacy_unit.read_bytes() == case.legacy_bytes


# --- T-149: a start that is going to refuse refuses before it changes anything ---

# A folder that no definition of the platform can name, and how the refusal reads.
# systemd: `systemd_working_directory` raises BridgeError for a line feed and for a
# carriage return. launchd: plistlib raises ValueError for a control character below
# U+0020 other than a tab, a line feed and a carriage return.
REFUSED_FOLDERS = [
    pytest.param("linux", "a\nb", BridgeError, "line feed or a carriage return", id="line-feed"),
    pytest.param(
        "linux", "a\rb", BridgeError, "line feed or a carriage return", id="carriage-return"
    ),
    pytest.param("darwin", "a\x01b", ValueError, "control characters", id="control-character"),
]


def tree(top: Path) -> dict[str, bytes | None]:
    """Every name under `top`, with the bytes of each regular file."""
    return {
        str(path.relative_to(top)): (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None
        )
        for path in sorted(top.rglob("*"))
    }


class Refused:
    """An earlier service, running, of a configuration in the folder `name` of tmp_path.

    Not built from Darwin or Linux: both make a definition of the current label, and that
    is what such a folder refuses. `earlier` says what is left of the earlier service:
    `record` is its record without a definition file; `found` is its definition file
    without a record. That file names the configuration through a link to the folder, as
    no line of a unit and no text of a plist can hold the folder's own name; the
    ownership check resolves the link.
    """

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        platform: str,
        name: str,
        earlier: str,
    ) -> None:
        self.home = fake_home(tmp_path, monkeypatch)
        launchd = platform == "darwin"
        self.manager = "launchd" if launchd else "systemd"
        self.run: Launchctl | Systemctl = Launchctl() if launchd else Systemctl()
        folder = tmp_path / name
        folder.mkdir()
        self.supervisor = supervisor_for(folder, monkeypatch, platform, self.run)
        legacy_label = self.supervisor.legacy_label
        assert legacy_label is not None
        suffix = ".plist" if launchd else ".service"
        self.legacy_definition = self.supervisor.root / (legacy_label + suffix)
        self.new_definition = (
            self.home / "Library" / "LaunchAgents" / f"{self.supervisor.label}.plist"
            if launchd
            else self.supervisor.root / f"{self.supervisor.label}.service"
        )
        if earlier == "record":
            self.supervisor.marker.write_text(
                json.dumps({"manager": self.manager, "label": legacy_label})
            )
        else:
            assert earlier == "found"
            link = tmp_path / "link"
            link.symlink_to(folder)
            command = [
                "/usr/bin/python3",
                "-m",
                "answerback_bridge",
                "--config",
                str(link / "cfg" / "config.json"),
                "serve",
            ]
            if launchd:
                self.legacy_definition.write_bytes(
                    plistlib.dumps({"Label": legacy_label, "ProgramArguments": command})
                )
            else:
                line = " ".join(json.dumps(argument) for argument in command)
                self.legacy_definition.write_text(f"[Service]\nExecStart={line}\n")
            assert self.supervisor.legacy_installed()
        if isinstance(self.run, Launchctl):
            self.run.registered.add(f"gui/{os.getuid()}/{legacy_label}")
        else:
            self.run.enabled.add(self.legacy_definition.name)
            self.run.started.add(self.legacy_definition.name)

    def running(self) -> set[str]:
        if isinstance(self.run, Launchctl):
            return set(self.run.registered)
        return self.run.started | self.run.enabled


@pytest.mark.parametrize("earlier", ["record", "found"])
@pytest.mark.parametrize(("platform", "name", "error", "text"), REFUSED_FOLDERS)
async def test_t149_start_refused_for_its_definition_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    name: str,
    error: type[Exception],
    text: str,
    earlier: str,
) -> None:
    """red_at_pretip: the earlier service was stopped and unregistered, and for `found` a
    record was written, before the definition of the new one was made and refused."""
    case = Refused(tmp_path, monkeypatch, platform, name, earlier)
    before, running = tree(tmp_path), case.running()
    record = case.supervisor.marker.read_bytes() if earlier == "record" else None

    with pytest.raises(error, match=text) as raised:
        await case.supervisor.start()

    assert type(raised.value) is error
    assert "previous service" not in str(raised.value)
    assert case.run.calls == []
    assert case.running() == running
    if record is None:
        assert not case.supervisor.marker.exists()
    else:
        assert case.supervisor.marker.read_bytes() == record
    assert not case.new_definition.exists()
    assert not (case.home / "Library").exists()
    assert tree(tmp_path) == before


@pytest.mark.parametrize("earlier", ["record", "found"])
async def test_t149_start_on_another_operating_system_refuses_before_any_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, earlier: str
) -> None:
    """red_at_pretip: for `found` a record was written before the refusal, and with a
    record the refusal read "does not match this operating system"."""
    case = Refused(tmp_path, monkeypatch, "sunos5", "plain", earlier)
    before = tree(tmp_path)

    with pytest.raises(BridgeError, match="requires macOS or Linux"):
        await case.supervisor.start()

    assert case.run.calls == []
    assert tree(tmp_path) == before


@pytest.mark.parametrize(("platform", "name", "error", "text"), REFUSED_FOLDERS)
def test_t149_reinstall_needed_is_true_when_the_definition_cannot_be_made(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    name: str,
    error: type[Exception],
    text: str,
) -> None:
    """red_at_pretip: the refusal of the definition came out of reinstall_needed()."""
    fake_home(tmp_path, monkeypatch)
    run = Launchctl() if platform == "darwin" else Systemctl()
    folder = tmp_path / name
    folder.mkdir()
    supervisor = supervisor_for(folder, monkeypatch, platform, run)
    manager = "launchd" if platform == "darwin" else "systemd"
    with pytest.raises(error, match=text):
        getattr(supervisor, f"{manager}_definition")()
    supervisor.marker.write_text(
        json.dumps(
            {
                "manager": manager,
                "label": supervisor.label,
                "definition": str(supervisor._installed_definition_path(manager)),
            }
        )
    )
    before = tree(tmp_path)

    assert supervisor.reinstall_needed() is True

    assert run.calls == []
    assert tree(tmp_path) == before


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_t149_reinstall_needed_passes_on_an_error_that_is_no_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """pins_existing: only the refusal of the definition is answered with True."""
    fake_home(tmp_path, monkeypatch)
    run = Launchctl() if platform == "darwin" else Systemctl()
    supervisor = supervisor_for(tmp_path, monkeypatch, platform, run)
    manager = "launchd" if platform == "darwin" else "systemd"
    supervisor.marker.write_text(
        json.dumps(
            {
                "manager": manager,
                "label": supervisor.label,
                "definition": str(supervisor._installed_definition_path(manager)),
            }
        )
    )
    failure = RuntimeError("no refusal")

    def broken() -> NoReturn:
        raise failure

    monkeypatch.setattr(supervisor, f"{manager}_definition", broken)

    with pytest.raises(RuntimeError) as raised:
        supervisor.reinstall_needed()

    assert raised.value is failure
    assert run.calls == []


@pytest.mark.parametrize("platform", [Darwin, Linux])
async def test_t149_start_makes_the_definition_once_and_before_the_first_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: type[Darwin] | type[Linux]
) -> None:
    """red_at_pretip: the definition was made after the earlier service was stopped and
    unregistered and after its record was written."""
    case = platform(tmp_path, monkeypatch)
    run = case.launchctl if isinstance(case, Darwin) else case.systemctl
    name = "launchd_definition" if isinstance(case, Darwin) else "systemd_definition"
    original = getattr(case.supervisor, name)
    # Each making: its text, the calls to the service manager before it, and whether a
    # record existed. Every text is marked with its number, so a second making that
    # went past this stand-in would not be taken for the first.
    made: list[tuple[bytes, int, bool]] = []

    def counted() -> bytes | str:
        value = original()
        number = len(made) + 1
        if isinstance(value, bytes):
            value += f"<!-- made {number} -->\n".encode()
            text = value
        else:
            value += f"# made {number}\n"
            text = value.encode()
        made.append((text, len(run.calls), case.supervisor.marker.exists()))
        return value

    monkeypatch.setattr(case.supervisor, name, counted)
    assert not case.supervisor.marker.exists()

    await case.supervisor.start()

    case.assert_switched()
    written = case.new_plist if isinstance(case, Darwin) else case.new_unit
    assert made == [(written.read_bytes(), 0, False)]
    assert b"made 1" in written.read_bytes()
