"""Shared helpers that keep the suite hermetic (see docs/TESTING-HARNESSES.md)."""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest


def pytest_configure() -> None:
    """Set the umask of the suite: 022, whatever the shell that starts pytest has.

    What a test creates must not depend on the umask of its caller. Under umask 002 a
    folder or file made with the default mode is group-writable, and the configuration
    path check refuses such a path, as it must. Pytest runs this hook before it imports
    any test module and before any fixture runs.

    Set in the hook and not at the top level of this file: a statement at the top level
    runs for every program that loads the file. A program that only loads it (the install
    smoke script, through tests/test_runtime.py) must keep the umask of its shell.
    """
    os.umask(0o022)


@pytest.fixture(autouse=True)
def mise_lookup_tripwire(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Refuse a launcher test that could discover the owner's real mise executable."""
    from answerback_bridge.bus import config as bus_config

    real_which = bus_config.shutil.which
    basetemp = tmp_path_factory.getbasetemp().absolute()

    def guarded_which(command: str, *args: object, **kwargs: object) -> str | None:
        result = real_which(command, *args, **kwargs)
        if command == "mise" and (
            result is None or not Path(result).absolute().is_relative_to(basetemp)
        ):
            pytest.fail("mise lookup tripwire: put a fake mise executable on PATH in this test")
        return result

    monkeypatch.setattr(bus_config.shutil, "which", guarded_which)


def basetemp_untraversable_component(path: Path) -> str | None:
    """Name the first component of ``path`` that cannot be opened O_NOFOLLOW from "/".

    The evidence reader in tools/worktree_guard_evidence.py opens each directory of a
    path relative to its parent with O_RDONLY | O_DIRECTORY | O_NOFOLLOW. Some sandboxes
    refuse that for their own temporary directory (EPERM), so a basetemp under such a
    path cannot host the evidence-reader tests. Returns ``None`` when every component
    opens; any error other than PermissionError propagates.
    """
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except PermissionError:
                return component
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)
    return None


def launchd_live_opt_in(environ: Mapping[str, str]) -> bool:
    """True only when ANSWERBACK_LIVE_LAUNCHD is exactly "1" in the given mapping.

    The one test that bootstraps a real launchd agent is opt-in; every other value,
    including "yes" and "true", keeps it skipped.
    """
    return environ.get("ANSWERBACK_LIVE_LAUNCHD") == "1"


# --- Umask restore (T-127) -------------------------------------------------------------
#
# The umask belongs to the process, so a test that runs product code which sets it (the
# `serve` branch of cli.main() sets 0o077) would leave that value to every later test of the
# session. A later test that creates a file, a folder or a FIFO with a mode and asserts the
# mode then passes alone and fails in the whole suite.


def read_umask() -> int:
    """Return the process umask and leave it as it was.

    The system has no call that only reads the umask: os.umask() sets a value and returns
    the one before. Both calls stand here, next to each other, and nowhere else in the
    suite. Between them the process has umask 0, so call this from the main thread of a
    test and not while another thread creates files.
    """
    current = os.umask(0)
    os.umask(current)
    return current


@pytest.fixture(autouse=True)
def umask_restored() -> Iterator[None]:
    """Put the umask of before the test back after it; never fail the test for changing it.

    Product code may rightly set a umask, so this restores and does not assert.
    """
    before = read_umask()
    try:
        yield
    finally:
        os.umask(before)


# --- Owner-path tripwire -------------------------------------------------------------
#
# NativeSupervisor resolves the places it writes and removes service definitions:
# ~/Library/LaunchAgents on macOS, the config's runtime directory, and (through the
# marker) whatever an earlier release recorded. A test that reaches those resolvers
# without faking HOME would write into the owner's real LaunchAgents directory, which
# launchd loads at the next login, and a test that writes then removes leaves nothing
# for a listing to find. The real home is captured here, at import, before any test can
# change HOME; every resolver result is checked against it before a write or removal.

REAL_HOME = Path(os.environ.get("HOME") or Path.home())
REAL_XDG_CONFIG_HOME: str | None = os.environ.get("XDG_CONFIG_HOME")
# herdr-app (T-181): `answerback install` and `herdr-app install` write the side panel there.
_PROTECTED_CONFIG_NAMES = ("answerback-bridge", "herdr-bridge", "codex-herdr-bridge", "herdr-app")
REAL_HERDR_APP_CONFIG: str | None = os.environ.get("HERDR_APP_CONFIG") or None
_GUARDED_RESOLVERS = ("_installed_definition_path", "_definition_path", "_runtime_definition_path")


def owner_path_violation(
    path: Path, real_home: Path, real_xdg_config_home: str | None
) -> str | None:
    """Name the owner directory `path` would touch, or None when it is safe to use.

    Only the owner's real LaunchAgents directory (the file's own directory, not a
    nested one), the real systemd user directories and the real bridge config
    directories count; anything else under the home (a basetemp, a scratch dir) is
    allowed, and a faked home never matches the captured real one.
    """
    path = Path(os.path.abspath(path))
    launch_agents = real_home / "Library" / "LaunchAgents"
    if path == launch_agents or path.parent == launch_agents:
        return f"{path} is in the owner's real LaunchAgents directory {launch_agents}"
    config_root = Path(real_xdg_config_home) if real_xdg_config_home else real_home / ".config"
    protected = [real_home / ".config" / "systemd", config_root / "systemd"]
    protected += [config_root / name for name in _PROTECTED_CONFIG_NAMES]
    protected += [real_home / ".config" / "herdr-app"]
    if REAL_HERDR_APP_CONFIG:
        protected.append(Path(os.path.abspath(Path(REAL_HERDR_APP_CONFIG).expanduser())))
    for directory in protected:
        if path == directory or directory in path.parents:
            return f"{path} is inside the owner's real {directory}"
    return None


def tripwire_active(environ: Mapping[str, str]) -> bool:
    """The tripwire is off only for the opt-in run of the real launchctl test."""
    return not launchd_live_opt_in(environ)


def _guarded(resolver):  # type: ignore[no-untyped-def]
    @functools.wraps(resolver)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = resolver(*args, **kwargs)
        reason = owner_path_violation(path, REAL_HOME, REAL_XDG_CONFIG_HOME)
        if reason is not None:
            pytest.fail(f"owner-path tripwire: {reason}; fake HOME in this test")
        return path

    return wrapper


# T-181 fix R6: the Herdr App installer's own mutation boundary. A test that passes the real
# folder to install or uninstall directly never goes through config_directory.
_GUARDED_HERDR_APP_CALLS = ("inspect", "install", "uninstall", "install_step", "uninstall_step")


def _guarded_herdr_app_call(function):  # type: ignore[no-untyped-def]
    @functools.wraps(function)
    def wrapper(directory, *args, **kwargs):  # type: ignore[no-untyped-def]
        reason = owner_path_violation(Path(directory), REAL_HOME, REAL_XDG_CONFIG_HOME)
        if reason is not None:
            pytest.fail(f"owner-path tripwire: {reason}; pass a folder of the test's own")
        return function(directory, *args, **kwargs)

    return wrapper


@pytest.fixture(autouse=True)
def owner_paths_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    if not tripwire_active(os.environ):
        return
    from answerback_bridge.providers.herdr import herdr_app
    from answerback_bridge.supervision import NativeSupervisor

    for name in _GUARDED_RESOLVERS:
        monkeypatch.setattr(NativeSupervisor, name, _guarded(getattr(NativeSupervisor, name)))
    # T-181: the Herdr App folder that install, uninstall and herdr-app resolve.
    monkeypatch.setattr(herdr_app, "config_directory", _guarded(herdr_app.config_directory))
    for name in _GUARDED_HERDR_APP_CALLS:
        monkeypatch.setattr(herdr_app, name, _guarded_herdr_app_call(getattr(herdr_app, name)))


@pytest.fixture(autouse=True)
def herdr_app_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Every test resolves the Herdr App folder to one that is not there (T-181).

    `answerback install` and `uninstall` then report the App's folder as missing and change
    nothing; a test of the installer passes its own folder or sets HERDR_APP_CONFIG itself.
    """
    monkeypatch.setenv("HERDR_APP_CONFIG", str(tmp_path_factory.getbasetemp() / "no-herdr-app"))
