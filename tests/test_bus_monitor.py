import asyncio
import errno
import hashlib
import json
import os
import selectors
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_bus_api import configured

from codex_herdr_bridge.bus.api import BusAPI
from codex_herdr_bridge.bus.config import setup_lock
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import BridgeError
from codex_herdr_bridge.transport import private_directory

# What `agent-bus monitor` writes on stderr, byte for byte.
BANNER = "Following local lifecycle events. Ctrl+C stops monitoring.\n"
UNAVAILABLE = "Local event ledger is unavailable; inspect bus configuration"
ERROR_OBJECT = json.dumps({"error": UNAVAILABLE}) + "\n"
NOTICE = (
    "Agent bus is not configured, so there are no lifecycle events; "
    "run agent-bus configure to enable it"
)


def populate(path: Path) -> BusAPI:
    api = BusAPI(path)
    row = api.submit("kiro:worker", str(path.parent), "PROMPT_PRIVATE", "monitor-one")
    store = api.store()
    store.claim(row["command_id"])
    store.bind(
        row["command_id"],
        {
            "id": "task-one",
            "status": "completed",
            "result": "RESULT_PRIVATE",
            "detail": "PRIVATE_DETAIL",
            "updated": 10,
            "turn_id": "turn-one",
        },
    )
    return api


async def test_snapshot_has_real_counts_and_never_exposes_bodies(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured(tmp_path)
    api = populate(path)
    ledger = api.store().path
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    frame = await Monitor(path).snapshot(after=0, limit=2)
    assert frame["available"]
    assert frame["stats"]["commands"] == 1
    assert frame["stats"]["tasks"]["completed"] == 1
    assert frame["stats"]["events"] == 3
    assert len(frame["events"]) == 2 and frame["has_more"]
    assert "PROMPT_PRIVATE" not in json.dumps(frame)
    assert "RESULT_PRIVATE" not in json.dumps(frame)
    assert "PRIVATE_DETAIL" not in json.dumps(frame)
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before
    next_page = await Monitor(path).snapshot(after=frame["cursor"])
    assert [event["type"] for event in next_page["events"]] == ["task.completed"]
    assert next_page["events"][0]["peer_id"] == "kiro:worker"


@pytest.mark.parametrize("bridge_config", [True, False], ids=["bridge-config", "no-bridge-config"])
async def test_missing_ledger_is_unavailable_and_never_created(
    tmp_path: Path, bridge_config: bool
) -> None:
    """T-62, red_at_base: a bus that was never configured says so; it is not a ledger failure.

    Input form: a bridge on which `agent-bus configure` never ran. Its config.json is written
    by Config.save(), as configure() itself writes it (bus/cli.py:175), or is absent; there is
    no `bus` directory. BusAPI.status() is the bridge's own word for this state.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    path = tmp_path / "config.json"
    if bridge_config:
        Config(provider_selection=[]).save(path)
    assert BusAPI(path).status() == {"configured": False}
    frame = await Monitor(path).snapshot()
    assert frame["configured"] is False
    assert frame["error"] is None
    # A reader that knows only `available` sees what it saw before: no ledger to read.
    assert frame["available"] is False
    assert frame["stats"] is None
    assert frame["events"] == []
    assert not path.parent.joinpath("bus").exists()


# Each real failure of a configured bus, and the exception class the frame names for it.
REAL_FAILURES = {
    "ledger-mode-000": "OperationalError",
    "ledger-not-a-database": "DatabaseError",
    "ledger-is-a-directory": "ValueError",
    "ledger-group-readable": "ValueError",
    "ledger-locked": "OperationalError",
    "ledger-deleted": "FileNotFoundError",
    "token-deleted": "FileNotFoundError",
    "bus-directory-mode-000": "PermissionError",
    "bus-is-a-file": "NotADirectoryError",
    "bus-config-corrupt": "JSONDecodeError",
    "bus-config-dangling-symlink": "ValueError",
}


@contextmanager
def broken_bus(tmp_path: Path, how: str) -> Iterator[Path]:
    """A bus as `agent-bus configure` leaves it, then damaged in one way.

    Input form: configure() (bus/cli.py:170-178) runs BusConfig.create and then BusAPI.store(),
    which creates the ledger; configured() and store() here make the same files.
    """
    path = configured(tmp_path)
    BusAPI(path).store()
    bus = path.parent / "bus"
    ledger = bus / "ledger.sqlite"
    with ExitStack() as undo:
        if how == "ledger-mode-000":
            ledger.chmod(0o000)
        elif how == "ledger-not-a-database":
            ledger.write_bytes(b"not a database\n" * 64)
        elif how == "ledger-is-a-directory":
            ledger.unlink()
            ledger.mkdir(mode=0o700)
        elif how == "ledger-group-readable":
            ledger.chmod(0o644)
        elif how == "ledger-locked":
            writer = sqlite3.connect(ledger, isolation_level=None)
            undo.callback(writer.close)
            writer.execute("BEGIN EXCLUSIVE")
        elif how == "ledger-deleted":
            ledger.unlink()
        elif how == "token-deleted":
            (bus / "token").unlink()
        elif how == "bus-directory-mode-000":
            bus.chmod(0o000)
            undo.callback(bus.chmod, 0o700)
        elif how == "bus-is-a-file":
            shutil.rmtree(bus)
            bus.write_text("not a directory\n")
        elif how == "bus-config-corrupt":
            (bus / "config.json").write_text("{not json\n")
        elif how == "bus-config-dangling-symlink":
            (bus / "config.json").unlink()
            (bus / "config.json").symlink_to(bus / "missing.json")
        elif how == "config-removed-token-and-ledger-left":
            (bus / "config.json").unlink()
        elif how == "only-setup-lock-left":
            # What an interrupted `agent-bus configure` leaves: BusConfig.create() takes this
            # lock first (bus/config.py:170), and setup_lock() never removes its file.
            shutil.rmtree(bus)
            with setup_lock(bus):
                pass
        elif how == "bus-dangling-symlink":
            shutil.rmtree(bus)
            bus.symlink_to(tmp_path / "unmounted")
        elif how == "bus-symlink-to-empty-directory":
            shutil.rmtree(bus)
            private_directory(tmp_path / "elsewhere")
            bus.symlink_to(tmp_path / "elsewhere")
        else:
            raise AssertionError(how)
        yield path


def configured_bus(tmp_path: Path, populated: bool) -> Path:
    """A bus as `agent-bus configure` leaves it; its ledger is empty or holds one finished command.

    Input form: configured() is Config.save() and BusConfig.create(); BusAPI.store() creates the
    ledger as configure() does (bus/cli.py:170-178); populate() stores through BusAPI.
    """
    path = configured(tmp_path)
    if populated:
        populate(path)
    else:
        BusAPI(path).store()
    return path


def lose(path: Path, loss: str) -> None:
    """Take the configuration, or the whole bus folder, away from under a running monitor."""
    bus = path.parent / "bus"
    if loss == "config-removed":
        (bus / "config.json").unlink()
    elif loss == "bus-removed":
        shutil.rmtree(bus)
    else:
        raise AssertionError(loss)


LOSSES = ("config-removed", "bus-removed")
FAILURE = (False, True, "FileNotFoundError")
CALM = (False, False, None)


def state(frame: dict[str, Any]) -> tuple[Any, Any, Any]:
    return frame["available"], frame["configured"], frame["error"]


def entries(folder: Path) -> list[str]:
    return sorted(entry.name for entry in folder.iterdir())


@pytest.fixture
def read_once(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set when a Monitor has finished its first read, whatever the outcome; the read is unchanged.

    A test that damages a bus under a running monitor waits for this, so the damage comes after
    a read and not before the first one.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    done = threading.Event()
    original = Monitor._read

    def read(self: Any, after: int | None, limit: int) -> dict[str, Any]:
        try:
            return original(self, after, limit)
        finally:
            done.set()

    monkeypatch.setattr(Monitor, "_read", read)
    return done


@pytest.mark.parametrize(("how", "error"), REAL_FAILURES.items(), ids=REAL_FAILURES)
async def test_real_ledger_failure_is_still_reported_as_unavailable(
    tmp_path: Path, how: str, error: str
) -> None:
    """T-62, pins_existing: a configured bus that cannot be read keeps its state and words.

    Input form: see broken_bus(): the files `agent-bus configure` leaves, with one real damage.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    with broken_bus(tmp_path, how) as path:
        frame = await Monitor(path).snapshot()
        assert (frame["available"], frame["error"], frame["stats"]) == (False, error, None)
        with pytest.raises(BridgeError) as raised:
            async for _event in Monitor(path).follow(count=1, interval=0.01):
                pass
        assert str(raised.value) == "Local event ledger is unavailable; inspect bus configuration"


@pytest.mark.parametrize("how", REAL_FAILURES)
async def test_real_ledger_failure_is_never_reported_as_not_configured(
    tmp_path: Path, how: str
) -> None:
    """T-62, red_at_base: the calm state is for a bus that was never configured, nothing else.

    Input form: see broken_bus(). A deleted ledger or token raises the same FileNotFoundError
    as a missing bus, and a `bus` directory that cannot be entered hides its config.json; both
    are failures of a configured bus and must not be read as "not configured".
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    with broken_bus(tmp_path, how) as path:
        frame = await Monitor(path).snapshot()
        assert frame["configured"] is True
        assert frame["available"] is False


async def test_readable_ledger_is_reported_as_configured(tmp_path: Path) -> None:
    """T-62, red_at_base: every frame carries `configured`, the healthy one too.

    Input form: configured() and populate() of this suite: BusConfig.create, then commands
    stored through BusAPI, as `agent-bus configure` and `agent-bus submit` do.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured(tmp_path)
    populate(path)
    frame = await Monitor(path).snapshot()
    assert frame["configured"] is True
    assert frame["available"] is True


async def test_follow_returns_new_events_and_honours_cursor(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured(tmp_path)
    api = BusAPI(path)
    api.store()
    monitor = Monitor(path)

    async def produce() -> None:
        await asyncio.sleep(0.05)
        api.submit("kiro:worker", str(tmp_path), "PRIVATE", "new")

    producer = asyncio.create_task(produce())
    try:
        events = [
            event
            async for event in monitor.follow(
                after=0,
                count=1,
                interval=0.02,
            )
        ]
        assert [event["type"] for event in events] == ["command.accepted"]
        assert (await monitor.snapshot(after=events[-1]["seq"]))["events"] == []
    finally:
        await producer


async def test_follow_treats_unconfigured_bus_as_calm_not_alarming(tmp_path: Path) -> None:
    """T-62, red_at_base: follow() on a never-configured bus raises BusNotConfigured, no alarm.

    Input form: as in test_missing_ledger_is_unavailable_and_never_created: a bridge config
    written by Config.save() and no `bus` directory.
    """
    from codex_herdr_bridge.bus import monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    # At d8e46d4 this is BridgeError("Local event ledger is unavailable; inspect bus ...").
    with pytest.raises(FileNotFoundError) as raised:
        async for _event in monitor.Monitor(path).follow(count=1, interval=0.01):
            pass
    assert isinstance(raised.value, monitor.BusNotConfigured)
    assert "not configured" in str(raised.value)
    assert "unavailable" not in str(raised.value)
    assert not path.parent.joinpath("bus").exists()


def test_read_raises_file_not_found_for_an_unconfigured_bus(tmp_path: Path) -> None:
    """T-62, pins_existing: the not-configured signal of _read() stays a FileNotFoundError.

    The event collector's bus source calls Monitor._read() itself and takes FileNotFoundError
    for "the bus is opt-in" (events/collector.py:531-533). Input form: a bridge config written
    by Config.save() and no `bus` directory.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    with pytest.raises(FileNotFoundError):
        Monitor(path)._read(None, 1)
    assert not path.parent.joinpath("bus").exists()


@pytest.mark.parametrize("populated", [True, False], ids=["populated", "empty"])
@pytest.mark.parametrize("loss", LOSSES)
async def test_follow_reports_a_bus_lost_while_following_as_a_failure(
    tmp_path: Path, read_once: threading.Event, loss: str, populated: bool
) -> None:
    """T-62 fix 1 (B1), red_at_pretip: a bus that this monitor has read cannot become "opt-in".

    Input form: see configured_bus() and lose(). At 288b5c4 follow() raises BusNotConfigured
    here and snapshot() on the same instance returns configured: False.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured_bus(tmp_path, populated)
    expected = [event["type"] for event in (await Monitor(path).snapshot(after=0))["events"]]
    assert len(expected) == (3 if populated else 0)
    # The read above was another monitor's: wait for the first read of the one that follows.
    read_once.clear()
    monitor = Monitor(path)
    stream = monitor.follow(after=0, interval=0.01)
    seen: list[str] = []

    async def drain() -> None:
        async for event in stream:
            seen.append(event["type"])

    following = asyncio.ensure_future(drain())
    try:
        assert await asyncio.to_thread(read_once.wait, 10)
        lose(path, loss)
        with pytest.raises(BridgeError) as raised:
            await asyncio.wait_for(following, 10)
    finally:
        following.cancel()
        await stream.aclose()
    assert str(raised.value) == UNAVAILABLE
    assert seen == expected
    assert state(await monitor.snapshot()) == FAILURE


@pytest.mark.parametrize("first", ["empty-ledger", "populated-ledger", "ledger-not-a-database"])
async def test_monitor_remembers_that_it_has_read_a_configured_bus(
    tmp_path: Path, first: str
) -> None:
    """T-62 fix 1 (B1), red_at_pretip: what a monitor has seen is kept in the monitor.

    The observation counts, not the events: an empty ledger, and a bus whose read failed after
    its config.json was found, are remembered too. Input form: configured_bus() or broken_bus(),
    then the whole bus folder is removed, so nothing on disk shows that a bus was set up. At
    288b5c4 the second snapshot() returns configured: False.
    """
    from codex_herdr_bridge.bus.monitor import BusNotConfigured, Monitor

    with ExitStack() as undo:
        if first == "ledger-not-a-database":
            path = undo.enter_context(broken_bus(tmp_path, first))
        else:
            path = configured_bus(tmp_path, first == "populated-ledger")
        monitor = Monitor(path)
        assert (await monitor.snapshot())["configured"] is True
        lose(path, "bus-removed")
        assert state(await monitor.snapshot()) == FAILURE
        with pytest.raises(BridgeError) as raised:
            async for _event in monitor.follow(count=1, interval=0.01):
                pass
        assert str(raised.value) == UNAVAILABLE
        # The collector's signal for "opt-in" is FileNotFoundError; this one is the plain class.
        with pytest.raises(FileNotFoundError) as plain:
            monitor._read(None, 1)
        assert not isinstance(plain.value, BusNotConfigured)
        # A monitor that has not read this bus sees an absent folder and nothing else.
        assert state(await Monitor(path).snapshot()) == CALM
        assert not path.parent.joinpath("bus").exists()


@pytest.mark.parametrize("found", ["dangling-symlink", "not-json", "directory"])
async def test_monitor_remembers_a_config_that_it_found_and_could_not_load(
    tmp_path: Path, found: str
) -> None:
    """T-62 fix 4, pins_existing: a monitor remembers a config.json that it found.

    Green at 9532823, the commit fix round 4 started from: the behaviour is older than the test.
    Red at d8e46d4 at one assertion only, the calm frame of a new monitor, which this row adds
    (a frame has no `configured` key there); the assertions about the one monitor hold there too.

    Also when it could not load it: the memory is set when the lstat of bus/config.json
    succeeds, before BusConfig.load(). So after such a read an ordinary empty bus folder is a
    failure for that monitor and calm for a new one; the difference is the monitor's memory,
    not the disk. Input form: a bridge config written by Config.save() and the folder of
    private_directory(), as in test_absent_or_empty_bus_folder_is_not_configured. The three
    config.json are not forms the bridge writes (BusConfig.create() writes a JSON file); they
    are damage: the link and the text are those of broken_bus() ("bus-config-dangling-symlink",
    "bus-config-corrupt"), the directory is the third row of the orchestrator's probe of fix
    round 4. The class of the first read's error is BusConfig.load()'s and is not pinned here.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bus = tmp_path / "bus"
    private_directory(bus)
    config = bus / "config.json"
    if found == "dangling-symlink":
        config.symlink_to(bus / "missing.json")
    elif found == "not-json":
        config.write_text("{not json\n")
    else:
        config.mkdir(mode=0o700)

    monitor = Monitor(path)
    first = await monitor.snapshot()
    assert first["available"] is False
    assert first.get("configured", True) is True
    assert first["error"]

    if found == "directory":
        config.rmdir()
    else:
        config.unlink()
    assert bus.is_dir() and not bus.is_symlink()
    assert entries(bus) == []

    second = await monitor.snapshot()
    assert second["available"] is False
    assert second.get("configured", True) is True
    assert second["error"] == "FileNotFoundError"
    with pytest.raises(BridgeError) as raised:
        async for _event in monitor.follow(count=1, interval=0.01):
            pass
    assert str(raised.value) == UNAVAILABLE
    # A monitor that has not read this folder sees an empty bus folder and nothing else.
    assert state(await Monitor(path).snapshot()) == CALM
    assert entries(bus) == []


@pytest.mark.parametrize(
    ("met", "error"),
    [
        pytest.param(
            "bus-folder-mode-0000",
            "PermissionError",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="folder permissions do not bind root"
            ),
        ),
        ("only-setup-lock", "FileNotFoundError"),
        ("bus-is-a-file", "NotADirectoryError"),
    ],
    ids=["bus-folder-mode-0000", "only-setup-lock", "bus-is-a-file"],
)
async def test_monitor_does_not_remember_a_failed_read_that_found_no_config(
    tmp_path: Path, met: str, error: str
) -> None:
    """T-62 fix 6 (R6), pins_existing: a failed read that found no config.json is not remembered.

    Green against the source of 90b259b, the commit fix round 6 started from; the tip's source
    and that source differ in one comment. Red against the source of d8e46d4 at one assertion,
    the calm frame of the same monitor's second read (a frame has no `configured` key there);
    the assertions about the first read hold there too.

    The memory is set when the lstat of bus/config.json succeeds and nowhere else. Here the
    first read fails without finding config.json: in none of the three cases does what stands
    at the bus folder's name hold a config.json, which the test asserts before that read. The
    class of the first read's error is pinned as well; alone it does not show that config.json
    was not found, since a bus that lost its ledger also fails with FileNotFoundError. So an
    ordinary empty bus folder afterwards is calm for the same monitor, as for a new one. Input
    form: a bridge config written by Config.save(), as in
    test_absent_or_empty_bus_folder_is_not_configured. The three bus folders are those of
    broken_bus(), here without a bus set up first: the folder of private_directory() with one
    chmod ("bus-directory-mode-000"), the folder and lock that setup_lock() leaves
    ("only-setup-lock-left"), and a file with the text of "bus-is-a-file". They are the rows
    mode-0000, entry-setup.lock and bus-file of the fourth cross-harness review's table.
    """
    from codex_herdr_bridge.bus import monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bus = tmp_path / "bus"
    if met == "bus-is-a-file":
        bus.write_text("not a directory\n")
        assert bus.is_file() and not bus.is_symlink()
    elif met == "only-setup-lock":
        with setup_lock(bus):
            pass
        assert bus.is_dir() and not bus.is_symlink()
        assert entries(bus) == ["setup.lock"]
    else:
        private_directory(bus)
        assert bus.is_dir() and not bus.is_symlink()
        assert entries(bus) == []

    one = monitor.Monitor(path)
    if met == "bus-folder-mode-0000":
        bus.chmod(0o000)
    try:
        first = await one.snapshot()
    finally:
        if met == "bus-folder-mode-0000":
            bus.chmod(0o700)
    assert first["available"] is False
    assert first.get("configured", True) is True
    assert first["error"] == error

    if met == "bus-is-a-file":
        bus.unlink()
        private_directory(bus)
    elif met == "only-setup-lock":
        (bus / "setup.lock").unlink()
    assert bus.is_dir() and not bus.is_symlink()
    assert bus.stat().st_mode & 0o777 == 0o700
    assert entries(bus) == []

    assert state(await one.snapshot()) == CALM
    with pytest.raises(monitor.BusNotConfigured):
        async for _event in one.follow(count=1, interval=0.01):
            pass
    # Here the monitor's memory makes no difference: a new monitor sees the same.
    assert state(await monitor.Monitor(path).snapshot()) == CALM
    assert entries(bus) == []


async def test_monitor_reused_after_a_failed_follow_that_found_no_config_is_not_configured(
    tmp_path: Path,
) -> None:
    """T-128, pins_existing: a follow() that fails without finding config.json is not remembered.

    Green before this row and with it: the source is unchanged. Red against a monitor that
    sets `_has_read_config` right before follow() raises BridgeError for a frame that is not
    available (note N3 of the fifth cross-harness review of T-62, whose changed monitor passed
    the whole of this file): there the last read of the same monitor is
    (False, True, "FileNotFoundError") and its second follow() raises BridgeError.

    The neighbour above pins the same memory for snapshot(); this test pins it for follow().
    Input form as there: a bridge config written by Config.save() and the folder and lock
    that setup_lock() leaves, without a bus set up first.
    """
    from codex_herdr_bridge.bus import monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bus = tmp_path / "bus"
    with setup_lock(bus):
        pass
    assert bus.is_dir() and not bus.is_symlink()
    assert entries(bus) == ["setup.lock"]

    one = monitor.Monitor(path)
    with pytest.raises(BridgeError) as raised:
        async for _event in one.follow(count=1, interval=0.01):
            pass
    assert type(raised.value) is BridgeError
    assert str(raised.value) == UNAVAILABLE

    (bus / "setup.lock").unlink()
    assert entries(bus) == []

    # The same monitor: it never found config.json, so an empty bus folder is calm for it.
    assert state(await one.snapshot()) == CALM
    with pytest.raises(monitor.BusNotConfigured):
        async for _event in one.follow(count=1, interval=0.01):
            pass
    assert entries(bus) == []


# A bus folder that is not an ordinary empty folder, seen by a monitor that never read the bus.
LEFT_OVER = (
    "config-removed-token-and-ledger-left",
    "only-setup-lock-left",
    "bus-dangling-symlink",
    "bus-symlink-to-empty-directory",
)


@pytest.mark.parametrize("how", LEFT_OVER)
async def test_bus_folder_that_holds_something_or_is_a_link_is_a_failure(
    tmp_path: Path, how: str
) -> None:
    """T-62 fix 1 (B2), red_at_pretip: an absent config.json alone does not mean "opt-in".

    Input form: see broken_bus(): the files `agent-bus configure` leaves, then the configuration
    is removed, or the folder is replaced by the lock file of an interrupted configure, or by a
    symbolic link. At 288b5c4 every one of these is configured: False and BusNotConfigured.
    """
    from codex_herdr_bridge.bus.monitor import Monitor

    with broken_bus(tmp_path, how) as path:
        bus = path.parent / "bus"
        before = entries(tmp_path)
        assert state(await Monitor(path).snapshot()) == FAILURE
        with pytest.raises(BridgeError) as raised:
            async for _event in Monitor(path).follow(count=1, interval=0.01):
                pass
        assert str(raised.value) == UNAVAILABLE
        # Looking changed nothing: no target was created behind a link, no file was added.
        assert entries(tmp_path) == before
        assert bus.is_symlink() == ("symlink" in how)
        if how == "bus-symlink-to-empty-directory":
            assert entries(tmp_path / "elsewhere") == []


@pytest.mark.parametrize("place", ["no-bus-folder", "empty-bus-folder"])
async def test_absent_or_empty_bus_folder_is_not_configured(tmp_path: Path, place: str) -> None:
    """T-62 fix 1, pins_existing: the two calm states of the first round stay calm.

    Input form: a bridge config written by Config.save(). The empty folder is made by
    private_directory(), the first step of every bus setup (bus/config.py:57), before any
    file is in it.
    """
    from codex_herdr_bridge.bus.monitor import BusNotConfigured, Monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bus = tmp_path / "bus"
    if place == "empty-bus-folder":
        private_directory(bus)
    monitor = Monitor(path)
    assert state(await monitor.snapshot()) == CALM
    with pytest.raises(BusNotConfigured):
        async for _event in monitor.follow(count=1, interval=0.01):
            pass
    # Reading twice changes neither the answer nor the folder.
    assert state(await monitor.snapshot()) == CALM
    assert bus.exists() == (place == "empty-bus-folder")
    assert not bus.exists() or entries(bus) == []


# An empty bus folder that a read cannot inspect, and the class of the error it gets. Without
# the right to search the folder, the lstat of config.json is refused. With it, config.json is
# absent, and the folder cannot be opened to be listed, so nothing shows that it is empty.
NOT_INSPECTABLE = {
    0o000: "PermissionError",
    0o400: "PermissionError",
    0o300: "FileNotFoundError",
    0o100: "FileNotFoundError",
}


@pytest.mark.skipif(os.geteuid() == 0, reason="folder permissions do not bind root")
@pytest.mark.parametrize(
    ("mode", "error"), NOT_INSPECTABLE.items(), ids=[f"mode-{mode:04o}" for mode in NOT_INSPECTABLE]
)
def test_empty_bus_folder_that_cannot_be_searched_or_listed_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int, error: str
) -> None:
    """T-62 fix 3 (R4), pins_existing: an empty bus folder is calm only when a read can see that.

    A monitor that has read nothing, and a bus folder with no entry that it may not search or
    may not list: a failure, as at d8e46d4, where a frame has no `configured` key. Input form:
    a bridge config written by Config.save() and the folder of private_directory(), as in
    test_absent_or_empty_bus_folder_is_not_configured, then one chmod. The bridge makes the
    folder with mode 0700 only; the other modes are the state of the third cross-harness
    review's R4 (mode 0000) and the three beside it.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli
    from codex_herdr_bridge.bus.monitor import Monitor

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    bus = tmp_path / "bus"
    private_directory(bus)

    async def follow(monitor: Any) -> None:
        async for _event in monitor.follow(count=1, interval=0.01):
            pass

    argv = ["--config", str(path), "monitor", "--count", "1", "--interval", "0.01"]
    shown = {}
    codes = {}
    bus.chmod(mode)
    try:
        monitor = Monitor(path)
        frame = asyncio.run(monitor.snapshot())
        with pytest.raises(BridgeError) as raised:
            asyncio.run(follow(monitor))
        for as_json in (False, True):
            with pytest.raises(SystemExit) as stopped:
                run_cli(parser(nested=True).parse_args([*argv, "--json"] if as_json else argv))
            codes[as_json] = stopped.value.code
            shown[as_json] = capsys.readouterr()
    finally:
        bus.chmod(0o700)

    assert frame["available"] is False
    assert frame["error"] == error
    # d8e46d4 has no such key; where a frame has it, it is True.
    assert frame.get("configured", True) is True
    assert str(raised.value) == UNAVAILABLE
    assert codes == {False: 1, True: 1}
    assert shown[False].out == "" and shown[True].out == ""
    assert shown[False].err == BANNER + ERROR_OBJECT
    assert shown[True].err == ERROR_OBJECT
    # Looking changed nothing: the folder is there, with no entry in it.
    assert entries(tmp_path) == ["bus", "config.json"]
    assert entries(bus) == []


def link_in_place(path: Path) -> Path:
    """Move the bus folder aside and leave a symbolic link to it where it was.

    Input form: not one the bridge makes; private_directory() refuses a link when a bus is set
    up (transport.py:107-112). It is the case `replacement-link-full` of the second
    cross-harness review (its audit_probe.py, replace()): rename, then symlink_to.
    """
    bus = path.parent / "bus"
    target = bus.with_name("bus-elsewhere")
    bus.rename(target)
    bus.symlink_to(target)
    return bus


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("when", ["link-before-the-first-read", "link-under-a-running-monitor"])
def test_bus_folder_that_links_to_a_populated_bus_is_read_as_a_working_bus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    as_json: bool,
) -> None:
    """T-62 fix 2 (R1), pins_existing: a link to a folder that holds this bus is a working bus.

    The monitor's paragraph makes a linked bus folder a failure only when the read does not
    find config.json. Every assertion here holds at d8e46d4 too, where a frame has no
    `configured` key and follow() has no hook. Input form: configured_bus() with one finished
    command and link_in_place(), before any read or right after the command's first read,
    and the namespace of `answerback bus monitor`.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured_bus(tmp_path, populated=True)
    bus = path.parent / "bus"
    before = entries(tmp_path)
    expected = ["command.accepted", "command.dispatching", "task.completed"]

    def working(monitor: Any) -> list[str]:
        frame = asyncio.run(monitor.snapshot(after=0))
        assert frame["available"] is True
        assert frame.get("error") is None
        # d8e46d4 has no such key; where a frame has it, it is True.
        assert frame.get("configured", True) is True
        return [event["type"] for event in frame["events"]]

    async def followed(monitor: Any) -> list[str]:
        async with asyncio.timeout(20):
            stream = monitor.follow(after=0, count=len(expected), interval=0.01)
            return [event["type"] async for event in stream]

    reader = Monitor(path)
    if when == "link-before-the-first-read":
        link_in_place(path)
    else:
        assert working(reader) == expected
        original = Monitor._read
        replaced: list[bool] = []

        def read(self: Any, after: int | None, limit: int) -> dict[str, Any]:
            frame = original(self, after, limit)
            if not replaced:
                # The command's first read is done. In its own thread, before its next read:
                # one more command is stored in the real folder, then the folder is the link.
                replaced.append(True)
                BusAPI(path).submit("kiro:worker", str(tmp_path), "PRIVATE", "new")
                link_in_place(path)
            return frame

        monkeypatch.setattr(Monitor, "_read", read)
        expected = [*expected, "command.accepted"]

    argv = ["--config", str(path), "monitor", "--after", "0", "--count", str(len(expected))]
    # --timeout only bounds a hung test: a monitor that ends by it prints too few events.
    argv += ["--interval", "0.01", "--timeout", "20"]
    run_cli(parser(nested=True).parse_args([*argv, "--json"] if as_json else argv))
    shown = capsys.readouterr()
    assert shown.err == ("" if as_json else BANNER)
    lines = shown.out.splitlines()
    if as_json:
        assert [json.loads(line)["type"] for line in lines] == expected
    else:
        assert [line.split()[3] for line in lines] == expected
        assert all("command=" in line for line in lines)

    assert bus.is_symlink()
    # A monitor that read the folder before it became a link, or first read it through the
    # link, and one that is new now: the frame and follow() of each.
    for monitor in (reader, Monitor(path)):
        assert working(monitor) == expected
        assert asyncio.run(followed(monitor)) == expected
    assert bus.is_symlink()
    # Reading made nothing: the folder that was moved aside is the only new entry.
    assert entries(tmp_path) == sorted([*before, "bus-elsewhere"])


@pytest.mark.parametrize("place", ["empty-folder", "folder-with-an-entry", "listing-fails"])
def test_absent_or_empty_closes_the_descriptor_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str
) -> None:
    """T-62 fix 2 (R2), pins_existing: the helper leaves no open descriptor on any of its paths.

    The test records which descriptor the helper's own os.open() returned for the folder and
    asks the operating system about that one afterwards; it does not count open descriptors.
    Input form: the empty folder is private_directory()'s, the entry is the setup.lock that
    setup_lock() leaves (bus/config.py:55-62). A listing that fails after the folder was
    opened cannot be made on disk, so os.scandir() raises for the recorded descriptor, as in
    the second cross-harness review's case `fds-list-error`.
    """
    from codex_herdr_bridge.bus import monitor

    folder = tmp_path / "bus"
    if place == "folder-with-an-entry":
        with setup_lock(folder):
            pass
    else:
        private_directory(folder)
    info = folder.lstat()
    opened: list[int] = []
    listed: list[int] = []
    real_open, real_scandir = os.open, os.scandir

    def recording_open(target: Any, flags: int, *rest: Any, **named: Any) -> int:
        descriptor = real_open(target, flags, *rest, **named)
        if os.fsdecode(target) == str(folder):
            opened.append(descriptor)
        return descriptor

    def scandir(target: Any = ".") -> Any:
        if target in opened:
            listed.append(target)
            if place == "listing-fails":
                raise PermissionError(errno.EACCES, "Permission denied")
        return real_scandir(target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", recording_open)
        patch.setattr(os, "scandir", scandir)
        answer = monitor.absent_or_empty(folder)

    assert answer is (place == "empty-folder")
    assert len(opened) == 1 and listed == opened
    # The check below is valid on one assumption: the test process has one thread at this point
    # and the folder is the test's own, so nothing else opens this folder between the helper's
    # return and the fstat. Another thread that opened the same folder in that time could get
    # the same number, and the check would then report a leak that is none.
    try:
        now = os.fstat(opened[0])
    except OSError as error:
        # Closed: the number names no open file.
        assert error.errno == errno.EBADF
    else:
        # The number is in use: either the helper left the folder open, or it was closed and
        # the number was given to another file since. Only the folder itself is a leak.
        leaked = (now.st_dev, now.st_ino) == (info.st_dev, info.st_ino)
        if leaked:
            os.close(opened[0])
        assert not leaked, "absent_or_empty() left the bus folder open"


@pytest.mark.parametrize("cursor", [-1, True, "one", 2**63])
async def test_invalid_cursors_are_refused(tmp_path: Path, cursor: object) -> None:
    from codex_herdr_bridge.bus.monitor import Monitor

    with pytest.raises(ValueError):
        await Monitor(configured(tmp_path)).snapshot(after=cursor)


def test_terminal_monitor_outputs_resumable_ndjson(tmp_path: Path) -> None:
    path = configured(tmp_path)
    populate(path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_herdr_bridge.bus.cli",
            "--config",
            str(path),
            "monitor",
            "--after",
            "0",
            "--count",
            "2",
            "--timeout",
            "2",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["seq"] for event in events] == [1, 2]
    assert all(event["command_id"] for event in events)
    assert "PROMPT_PRIVATE" not in result.stdout


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_terminal_monitor_on_unconfigured_bus_ends_calmly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    """T-62, red_at_base: `agent-bus monitor` without --web on a never-configured bus is calm.

    Input form: the namespace `answerback bus monitor` hands to run_cli (cli.py:974-978): the
    bus parser as cli.py:74-81 mounts it, parser(nested=True), which reads no default
    configuration folder. The bridge config is written by Config.save(); no `bus` directory.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    argv = ["--config", str(path), "monitor", "--count", "1", "--interval", "0.01"]
    # At d8e46d4 run_cli prints {"error": "Local event ledger is unavailable; ..."} and exits 1.
    run_cli(parser(nested=True).parse_args([*argv, "--json"] if as_json else argv))
    shown = capsys.readouterr()
    assert shown.out == ""
    assert "unavailable" not in shown.err and '"error"' not in shown.err
    if as_json:
        notice = json.loads(shown.err)
        assert notice["configured"] is False and set(notice) == {"configured", "message"}
        message = notice["message"]
    else:
        message = shown.err.splitlines()[-1]
    assert "not configured" in message and "agent-bus configure" in message
    assert not path.parent.joinpath("bus").exists()


def test_terminal_monitor_on_broken_ledger_still_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-62, pins_existing: a real ledger failure keeps its error text and exit code 1.

    Input form: see broken_bus() and the test above: the namespace of `answerback bus monitor`
    on a configured bus whose ledger is not a database.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    with broken_bus(tmp_path, "ledger-not-a-database") as path:
        argv = ["--config", str(path), "monitor", "--count", "1", "--interval", "0.01", "--json"]
        with pytest.raises(SystemExit) as stopped:
            run_cli(parser(nested=True).parse_args(argv))
    assert stopped.value.code == 1
    shown = capsys.readouterr()
    assert shown.out == ""
    assert json.loads(shown.err) == {
        "error": "Local event ledger is unavailable; inspect bus configuration"
    }


def run_monitor(argv: list[str]) -> tuple[threading.Thread, list[object]]:
    """`answerback bus monitor` on a worker thread; the list receives its exit code."""
    from codex_herdr_bridge.bus.cli import parser, run_cli

    args = parser(nested=True).parse_args(argv)
    outcome: list[object] = []

    def target() -> None:
        try:
            run_cli(args)
            outcome.append(0)
        except SystemExit as stopped:
            outcome.append(stopped.code)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("populated", [True, False], ids=["populated", "empty"])
@pytest.mark.parametrize("loss", LOSSES)
def test_terminal_monitor_fails_loudly_when_the_bus_is_lost_while_it_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    read_once: threading.Event,
    loss: str,
    populated: bool,
    as_json: bool,
) -> None:
    """T-62 fix 1 (B1), red_at_pretip: a running monitor that loses its bus exits 1, as before.

    Input form: the namespace of `answerback bus monitor` (see the tests above) on
    configured_bus(); lose() runs after the command's first read. At 288b5c4 the command
    prints the "not configured" notice and exits 0.
    """
    path = configured_bus(tmp_path, populated)
    argv = ["--config", str(path), "monitor", "--after", "0", "--interval", "0.01"]
    # --timeout only bounds a hung test: a monitor that ends by it exits 0 and fails below.
    argv += ["--timeout", "20", "--json"] if as_json else ["--timeout", "20"]
    thread, outcome = run_monitor(argv)
    assert read_once.wait(10)
    lose(path, loss)
    thread.join(30)
    assert not thread.is_alive()
    shown = capsys.readouterr()
    assert outcome == [1]
    assert shown.err == (ERROR_OBJECT if as_json else BANNER + ERROR_OBJECT)
    lines = shown.out.splitlines()
    assert len(lines) == (3 if populated else 0)
    if as_json:
        assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3][: len(lines)]


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("how", [*LEFT_OVER, "bus-is-a-file", "ledger-not-a-database"])
def test_terminal_monitor_fails_loudly_on_a_bus_folder_that_is_not_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str, as_json: bool
) -> None:
    """T-62 fix 1 (B2, M1): a failure prints the banner in text mode, then the old error object.

    red_at_pretip for the four LEFT_OVER states (288b5c4: notice and exit 0). pins_existing for
    bus-is-a-file and ledger-not-a-database: their output is byte for byte that of d8e46d4.
    Input form: see broken_bus(), and the namespace of `answerback bus monitor`.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    with broken_bus(tmp_path, how) as path:
        argv = ["--config", str(path), "monitor", "--count", "1", "--interval", "0.01"]
        with pytest.raises(SystemExit) as stopped:
            run_cli(parser(nested=True).parse_args([*argv, "--json"] if as_json else argv))
    assert stopped.value.code == 1
    shown = capsys.readouterr()
    assert shown.out == ""
    assert shown.err == (ERROR_OBJECT if as_json else BANNER + ERROR_OBJECT)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("place", ["no-bus-folder", "empty-bus-folder"])
def test_terminal_monitor_on_unconfigured_bus_prints_the_notice_and_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], place: str, as_json: bool
) -> None:
    """T-62 fix 1 (M1): without a configured bus stderr carries the notice alone, exit 0.

    red_at_pretip in text mode (288b5c4 prints the "Following ..." banner first);
    pins_existing with --json. Input form: as in
    test_absent_or_empty_bus_folder_is_not_configured, and the namespace of
    `answerback bus monitor`.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    if place == "empty-bus-folder":
        private_directory(tmp_path / "bus")
    argv = ["--config", str(path), "monitor", "--count", "1", "--interval", "0.01"]
    run_cli(parser(nested=True).parse_args([*argv, "--json"] if as_json else argv))
    shown = capsys.readouterr()
    assert shown.out == ""
    if as_json:
        assert shown.err == json.dumps({"configured": False, "message": NOTICE}) + "\n"
    else:
        assert shown.err == NOTICE + "\n"


def test_terminal_monitor_prints_the_banner_before_the_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-62 fix 1 (M1), pins_existing: a readable ledger gets the banner once, then its events.

    Input form: configured_bus() with one finished command, and the namespace of
    `answerback bus monitor` in text mode.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    path = configured_bus(tmp_path, populated=True)
    argv = ["--config", str(path), "monitor", "--after", "0", "--count", "3", "--interval", "0.01"]
    run_cli(parser(nested=True).parse_args(argv))
    shown = capsys.readouterr()
    assert shown.err == BANNER
    lines = shown.out.splitlines()
    assert len(lines) == 3 and all("command=" in line for line in lines)


def test_terminal_monitor_prints_the_banner_for_an_empty_ledger_at_once(tmp_path: Path) -> None:
    """T-62 fix 1 (M1), pins_existing: the banner does not wait for the first event.

    Input form: configured_bus() with an empty ledger and the command as a process, as in
    test_terminal_monitor_outputs_resumable_ndjson. The first command is stored only after
    the banner has been read from the process.
    """
    path = configured_bus(tmp_path, populated=False)
    command = [sys.executable, "-m", "codex_herdr_bridge.bus.cli", "--config", str(path)]
    command += ["monitor", "--after", "0", "--count", "1", "--interval", "0.01", "--timeout", "30"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            assert selector.select(20), "No banner while the ledger is empty"
        assert process.stderr.readline() == BANNER
        BusAPI(path).submit("kiro:worker", str(tmp_path), "PRIVATE", "first")
        out, err = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0
    assert err == ""
    assert len(out.splitlines()) == 1 and "command.accepted" in out


@pytest.mark.parametrize("bus", ["configured", "never-configured"])
def test_terminal_monitor_refuses_an_argument_before_any_read_and_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], read_once: threading.Event, bus: str
) -> None:
    """T-62 fix 1 (M1), red_at_pretip: a refused argument prints its error and nothing else.

    The banner comes from the first read, and follow() refuses an argument before it reads.
    At 288b5c4, as at d8e46d4, the banner precedes this error. Input form: configured_bus(),
    or a bridge config written by Config.save(), and the namespace of `answerback bus monitor`.
    """
    from codex_herdr_bridge.bus.cli import parser, run_cli

    if bus == "configured":
        path = configured_bus(tmp_path, populated=True)
    else:
        path = tmp_path / "config.json"
        Config(provider_selection=[]).save(path)
    with pytest.raises(SystemExit) as stopped:
        run_cli(
            parser(nested=True).parse_args(["--config", str(path), "monitor", "--interval", "0"])
        )
    assert stopped.value.code == 1
    shown = capsys.readouterr()
    assert shown.out == ""
    assert shown.err == json.dumps({"error": "Monitor interval must be 0.01–60 seconds"}) + "\n"
    assert not read_once.is_set()


async def test_status_timeout_is_unknown_and_confirmed_disconnect_is_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured(tmp_path)
    populate(path)
    call = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr("codex_herdr_bridge.bus.monitor.RemoteBridge.call", call)
    frame = await Monitor(path).snapshot()
    assert frame["connection"]["service_running"] is None
    assert frame["connection"]["broker_connected"] is None
    call.side_effect = None
    call.return_value = {"bus": {"connected": False}}
    frame = await Monitor(path).snapshot()
    assert frame["connection"]["service_running"] is True
    assert frame["connection"]["broker_connected"] is False
    call.assert_awaited_with("status", {})


def test_monitor_rejects_web_only_and_terminal_only_option_mixes(tmp_path: Path) -> None:
    for args in (
        ["--web", "--interval", "2"],
        ["--web", "--json"],
        ["--web", "--after", "0"],
        ["--no-open"],
        ["--port", "10000"],
        ["--after", str(2**63)],
        ["--interval", "0"],
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "codex_herdr_bridge.bus.cli",
                "--config",
                str(tmp_path / "config.json"),
                "monitor",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1, (args, result.stderr)


async def test_refusal_has_safe_reason_and_blocker_without_raw_detail(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.monitor import Monitor

    path = configured(tmp_path)
    api = BusAPI(path)
    row = api.submit("kiro:worker", str(tmp_path), "SECRET_PROMPT", "busy")
    api.store().claim(row["command_id"])
    api.store().finish(
        row["command_id"],
        "rejected",
        "SECRET_DETAIL",
        blocking_task_id="older-task",
    )
    frame = await Monitor(path).snapshot(after=0)
    assert frame["events"][-1]["payload"] == {
        "delivery": "rejected",
        "reason_code": "target_busy",
        "blocking_task_id": "older-task",
    }
    assert "SECRET" not in json.dumps(frame)
    reset = await Monitor(path).snapshot(after=2**63 - 1)
    assert reset["reset_required"] is True


@contextmanager
def dashboard_page(
    path: Path, rewrite: Callable[[dict[str, Any]], None] | None = None
) -> Iterator[tuple[Any, list[str]]]:
    """The shipped dashboard.js in a browser, fed by the bridge's own Dashboard server.

    Input form: the frame of /api/frame as bus/web.py serves it, as in tests/test_bus_web.py.
    `rewrite` changes each served frame in place before the page receives it.
    """
    from playwright.sync_api import sync_playwright

    from codex_herdr_bridge.bus.web import Dashboard

    with Dashboard(path) as dashboard, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            if rewrite is not None:

                def served(route: Any) -> None:
                    response = route.fetch()
                    frame = response.json()
                    rewrite(frame)
                    route.fulfill(response=response, json=frame)

                page.route("**/api/frame*", served)
            page.goto(dashboard.url)
            yield page, errors
        finally:
            browser.close()


def test_dashboard_shows_unconfigured_bus_calmly(tmp_path: Path) -> None:
    """T-62, red_at_base: the page shows a calm, distinct state for a never-configured bus.

    Input form: see dashboard_page(); the bridge config is written by Config.save() and there
    is no `bus` directory.
    """
    from playwright.sync_api import expect

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    with dashboard_page(path) as (page, errors):
        # At d8e46d4 this is "Ledger unavailable", drawn as an error.
        expect(page.locator("#connection")).to_have_text("Bus not configured")
        expect(page.locator("#connection")).to_have_class("connection waiting")
        expect(page.locator("#notice")).to_contain_text("not configured")
        expect(page.locator("#notice")).to_contain_text("agent-bus configure")
        expect(page.locator("#notice")).not_to_contain_text("unavailable")
        expect(page.get_by_test_id("commands-count")).to_have_text("—")
        page.get_by_role("button", name="Debug").click()
        expect(page.locator("#diagnostics")).to_contain_text('"bus_configured": false')
        expect(page.locator("#diagnostics")).to_contain_text('"ledger_available": false')
        assert not errors
    assert not path.parent.joinpath("bus").exists()


def test_dashboard_still_shows_real_ledger_failure_as_unavailable(tmp_path: Path) -> None:
    """T-62, pins_existing: the page keeps its alarm and its words for a real ledger failure.

    Input form: see dashboard_page() and broken_bus(): a configured bus whose ledger is not a
    database.
    """
    from playwright.sync_api import expect

    with broken_bus(tmp_path, "ledger-not-a-database") as path, dashboard_page(path) as shown:
        page, errors = shown
        expect(page.locator("#connection")).to_have_text("Ledger unavailable")
        expect(page.locator("#connection")).to_have_class("connection error")
        expect(page.locator("#notice")).to_have_text(
            "The local event ledger is unavailable. Previously observed events remain visible."
        )
        page.get_by_role("button", name="Debug").click()
        expect(page.locator("#diagnostics")).to_contain_text('"ledger_error": "DatabaseError"')
        assert not errors


UNAVAILABLE_NOTICE = (
    "The local event ledger is unavailable. Previously observed events remain visible."
)


@pytest.mark.parametrize("key", ["no-configured-key", "configured-null"])
def test_dashboard_takes_only_an_explicit_false_for_not_configured(
    tmp_path: Path, key: str
) -> None:
    """T-62 fix 1 (B3), pins_existing: a frame without `configured: false` is a failure.

    Input form: see dashboard_page(). The served frame is that of a server at d8e46d4 for a
    missing bus: available false, the error class, and no `configured` key; the second case
    carries the key as null. The bridge config is written by Config.save().
    """
    from playwright.sync_api import expect

    def older(frame: dict[str, Any]) -> None:
        frame["error"] = "FileNotFoundError"
        if key == "no-configured-key":
            del frame["configured"]
        else:
            frame["configured"] = None

    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    with dashboard_page(path, older) as (page, errors):
        expect(page.locator("#connection")).to_have_text("Ledger unavailable")
        expect(page.locator("#connection")).to_have_class("connection error")
        expect(page.locator("#notice")).to_have_text(UNAVAILABLE_NOTICE)
        page.get_by_role("button", name="Debug").click()
        expect(page.locator("#diagnostics")).to_contain_text('"ledger_error": "FileNotFoundError"')
        expect(page.locator("#diagnostics")).not_to_contain_text('"bus_configured": false')
        assert not errors


@pytest.mark.parametrize("loss", LOSSES)
def test_dashboard_reports_a_bus_lost_while_the_page_is_open_as_unavailable(
    tmp_path: Path, loss: str
) -> None:
    """T-62 fix 1 (B1), red_at_pretip: a page that has shown events never turns "not configured".

    Input form: see dashboard_page(), configured_bus() and lose(). The Dashboard server keeps
    one Monitor (bus/web.py:91), so what it has read is remembered between two polls of the
    page. At 288b5c4 the page shows "Bus not configured" after the loss.
    """
    from playwright.sync_api import expect

    path = configured_bus(tmp_path, populated=True)
    with dashboard_page(path) as (page, errors):
        rows = page.get_by_test_id("event-row")
        expect(page.get_by_test_id("commands-count")).to_have_text("1")
        expect(rows).to_have_count(3)
        lose(path, loss)
        expect(page.locator("#connection")).to_have_text("Ledger unavailable")
        expect(page.locator("#connection")).to_have_class("connection error")
        expect(page.locator("#notice")).to_have_text(UNAVAILABLE_NOTICE)
        expect(page.get_by_test_id("commands-count")).to_have_text("—")
        expect(rows).to_have_count(3)
        page.get_by_role("button", name="Debug").click()
        expect(page.locator("#diagnostics")).to_contain_text('"bus_configured": true')
        expect(page.locator("#diagnostics")).to_contain_text('"ledger_error": "FileNotFoundError"')
        assert not errors
