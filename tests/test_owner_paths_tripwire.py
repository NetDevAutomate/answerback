"""The suite must never resolve a service or config path inside the owner's real home.

A test that reaches NativeSupervisor's path resolvers without faking HOME would
write into the owner's real ~/Library/LaunchAgents, which launchd loads at the next
login, and a test that writes and then removes leaves nothing for a listing to find.
conftest installs an autouse tripwire; these tests pin its decision function and
prove the wrapper fires before any write.
"""

import json
import os
from pathlib import Path

import pytest
from conftest import REAL_HOME, REAL_XDG_CONFIG_HOME, owner_path_violation, tripwire_active

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.supervision import NativeSupervisor

HOME = Path("/Users/owner")


def test_real_launch_agents_file_is_a_violation() -> None:
    reason = owner_path_violation(HOME / "Library" / "LaunchAgents" / "x.plist", HOME, None)
    assert reason is not None
    assert "/Users/owner/Library/LaunchAgents/x.plist" in reason


def test_real_launch_agents_directory_itself_is_a_violation() -> None:
    assert owner_path_violation(HOME / "Library" / "LaunchAgents", HOME, None) is not None


def test_real_systemd_user_unit_is_a_violation() -> None:
    unit = HOME / ".config" / "systemd" / "user" / "x.service"
    assert owner_path_violation(unit, HOME, None) is not None


@pytest.mark.parametrize("name", ["answerback-bridge", "herdr-bridge", "codex-herdr-bridge"])
def test_real_bridge_config_directories_are_violations(name: str) -> None:
    path = HOME / ".config" / name / "runtime" / "local.answerback-bridge.abc.plist"
    assert owner_path_violation(path, HOME, None) is not None


def test_xdg_config_home_moves_the_protected_config_root() -> None:
    xdg = "/Volumes/cfg"
    moved = Path(xdg) / "herdr-bridge" / "runtime" / "supervisor.json"
    assert owner_path_violation(moved, HOME, xdg) is not None
    assert owner_path_violation(Path(xdg) / "systemd" / "user" / "x.service", HOME, xdg) is not None
    # The default root is not protected once XDG_CONFIG_HOME points elsewhere...
    default = HOME / ".config" / "herdr-bridge" / "config.json"
    assert owner_path_violation(default, HOME, xdg) is None
    # ...except systemd, which the owner's user manager still reads from ~/.config.
    unit = HOME / ".config" / "systemd" / "user" / "x.service"
    assert owner_path_violation(unit, HOME, xdg) is not None


def test_other_paths_under_the_real_home_are_not_violations() -> None:
    # A basetemp or scratch directory under the home must not false-trip.
    for path in (
        HOME / ".kiro" / "crew" / "scratch" / "pytest-0" / "runtime" / "x.plist",
        HOME / "Library" / "Application Support" / "x.plist",
        HOME / "Library" / "LaunchAgents" / "nested" / "x.plist",
        HOME / ".config" / "other-tool" / "config.json",
        Path("/private/tmp/basetemp/home/Library/LaunchAgents/x.plist"),
    ):
        assert owner_path_violation(path, HOME, None) is None, path


def test_a_faked_home_is_not_the_real_home() -> None:
    fake = Path("/private/tmp/basetemp/home")
    assert owner_path_violation(fake / "Library" / "LaunchAgents" / "x.plist", HOME, None) is None
    assert owner_path_violation(HOME / "Library" / "LaunchAgents" / "x.plist", fake, None) is None


def test_tripwire_is_active_unless_the_live_launchd_test_is_opted_in() -> None:
    assert tripwire_active({}) is True
    assert tripwire_active({"ANSWERBACK_LIVE_LAUNCHD": "yes"}) is True
    assert tripwire_active({"ANSWERBACK_LIVE_LAUNCHD": "1"}) is False


def test_real_home_was_captured_before_any_test_changed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REAL_HOME == Path(os.environ["HOME"])
    assert REAL_XDG_CONFIG_HOME == os.environ.get("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert REAL_HOME != tmp_path, "the captured home must not follow a faked HOME"


def test_resolving_the_real_launch_agents_path_fails_the_test_before_any_write(
    tmp_path: Path,
) -> None:
    # HOME deliberately NOT faked: the resolver would name the owner's real directory.
    supervisor = NativeSupervisor(tmp_path / "config.json")
    before = sorted(p.name for p in (REAL_HOME / "Library" / "LaunchAgents").glob("*"))
    with pytest.raises(pytest.fail.Exception, match="LaunchAgents"):
        supervisor._installed_definition_path("launchd")
    after = sorted(p.name for p in (REAL_HOME / "Library" / "LaunchAgents").glob("*"))
    assert after == before


def test_tripwire_compares_against_the_captured_home_not_the_faked_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = tmp_path / "cfg" / "config.json"
    Config("explicit-session").save(config)
    supervisor = NativeSupervisor(config)
    # Under the fake HOME the resolver is fine...
    assert supervisor._installed_definition_path("launchd").is_relative_to(home)
    # ...but a marker that points at the REAL LaunchAgents must still trip.
    supervisor.root.mkdir(mode=0o700)
    real = REAL_HOME / "Library" / "LaunchAgents" / f"{supervisor.label}.plist"
    supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(real)})
    )
    with pytest.raises(pytest.fail.Exception, match="LaunchAgents"):
        supervisor._definition_path(supervisor.label, "launchd")
    assert not real.exists()


def _herdr_app_targets(captured: Path) -> list[Path]:
    return [REAL_HOME / ".config" / "herdr-app", captured, captured / "mods" / "answerback-fleet"]


@pytest.mark.parametrize(
    "call", ["inspect", "install", "uninstall", "install_step", "uninstall_step"]
)
def test_a_direct_call_of_the_herdr_app_installer_on_the_real_folder_fails_before_it_reads(
    call: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-181 fix R6 (ABM-T181F): the installer's own entry points are guarded, not only
    config_directory, for the real default folder and the captured HERDR_APP_CONFIG.

    The installer's first step is replaced by one that records it was reached, so a missing
    guard fails this test without reading or writing the owner's folder.

    red at 350c792: the call reached the installer.
    """
    import conftest

    from answerback_bridge.providers.herdr import herdr_app

    captured = tmp_path / "captured-herdr-app"
    monkeypatch.setattr(conftest, "REAL_HERDR_APP_CONFIG", str(captured))
    reached: list[Path] = []

    def reached_installer(directory: Path, home: Path) -> None:
        reached.append(directory)
        raise AssertionError(f"the installer was reached for {directory}")

    monkeypatch.setattr(herdr_app, "_root_check", reached_installer)
    monkeypatch.setattr(herdr_app, "shipped", lambda: reached_installer(Path("shipped"), Path()))
    for target in _herdr_app_targets(captured):
        kwargs = {"apply": True} if call in ("install", "uninstall") else {}
        with pytest.raises(pytest.fail.Exception, match="owner-path tripwire"):
            getattr(herdr_app, call)(target, **kwargs)
    assert reached == []
    assert not captured.exists()
