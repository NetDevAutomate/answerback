"""fleet: the settings file of ``fleet configure`` and ``fleet serve --settings`` (T-185)."""

import json
import os
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_fakes import Fleet, Service, linux, writer
from test_fleet_lifetime import no_descriptor_left, process_unchanged

from answerback_bridge import cli
from answerback_bridge.fleet import cli as fleet_cli
from answerback_bridge.fleet import loop, settings
from answerback_bridge.fleet.settings import FileSettings, SettingsError

# ------------------------------------------------------------------ the module on its own


def _good() -> FileSettings:
    return FileSettings(("build-a", "build-b"), {"build-a": 6}, Path("/srv/fleet"), "team.lane=1")


def test_a_written_file_is_read_back_as_the_same_settings(tmp_path: Path) -> None:
    path = tmp_path / "made" / "fleet-writer.json"
    settings.write(path, _good())
    assert settings.read(path) == _good()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": "answerback.fleet-writer/1",
        "machines": ["build-a", "build-b"],
        "ceilings": {"build-a": 6},
        "dir": "/srv/fleet",
        "lane_label": "team.lane=1",
    }


def test_the_file_has_mode_0600_and_a_made_folder_0700(tmp_path: Path) -> None:
    path = tmp_path / "made" / "fleet-writer.json"
    settings.write(path, FileSettings())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert os.listdir(path.parent) == ["fleet-writer.json"], "no staged file is left behind"


@pytest.mark.parametrize("umask", [0o077, 0o277], ids=["no group or other bits", "no owner write"])
def test_a_made_folder_is_0700_whatever_the_umask(tmp_path: Path, umask: int) -> None:
    """R1: ``mkdir`` filters its mode through the umask; the folder is set to 0700 after it."""
    path = tmp_path / "made" / "fleet-writer.json"
    before = os.umask(umask)
    try:
        settings.write(path, FileSettings(("build-a",)))
    finally:
        os.umask(before)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert settings.read(path).machines == ("build-a",)


def test_a_folder_that_was_there_keeps_its_mode(tmp_path: Path) -> None:
    """R1: the mode is set on the folder the writer made, never on one that existed."""
    folder = tmp_path / "theirs"
    folder.mkdir(mode=0o750)
    os.chmod(folder, 0o750)  # not filtered by the umask of the test process
    settings.write(folder / "fleet-writer.json", FileSettings())
    assert stat.S_IMODE(folder.stat().st_mode) == 0o750


def test_a_second_write_replaces_the_file_and_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "fleet-writer.json"
    settings.write(path, _good())
    os.chmod(path, 0o644)
    settings.write(path, FileSettings(("build-c",)))
    assert settings.read(path).machines == ("build-c",)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_write_that_cannot_rename_leaves_no_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_rename(source: Any, target: Any) -> None:
        raise OSError(5, "injected error at the rename")

    monkeypatch.setattr(settings.os, "replace", no_rename)
    with pytest.raises(OSError, match="injected"):
        settings.write(tmp_path / "fleet-writer.json", FileSettings())
    assert os.listdir(tmp_path) == []


def test_an_empty_settings_file_means_every_machine_and_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"schema": "answerback.fleet-writer/1"}', encoding="utf-8")
    assert settings.read(path) == FileSettings()


def test_repeated_machines_are_kept_once_in_their_order() -> None:
    chosen = settings.check(["b", "a", "b"], {}, None, "ab.lane=1")
    assert chosen.machines == ("b", "a")


def test_the_default_path_is_beside_the_configuration_file_not_in_the_published_folder(
    tmp_path: Path,
) -> None:
    path = settings.default_path(tmp_path / "herdr-bridge" / "config.json")
    assert path == tmp_path / "herdr-bridge" / "fleet-writer.json"
    assert path.parent.name != "fleet"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("[]", "not an object"),
        ('{"schema": "answerback.fleet-writer/2"}', "schema is not answerback.fleet-writer/1"),
        ('{"schema": "answerback.fleet-writer/1", "extra": 1}', "unknown field extra"),
        ('{"schema": "answerback.fleet-writer/1", "machines": "a"}', "machines: a list"),
        ('{"schema": "answerback.fleet-writer/1", "machines": ["local"]}', "machines: a list"),
        ('{"schema": "answerback.fleet-writer/1", "machines": ["bad name"]}', "machines: a list"),
        (
            json.dumps(
                {"schema": "answerback.fleet-writer/1", "machines": [f"m{n}" for n in range(16)]}
            ),
            "at most 15 workers",
        ),
        ('{"schema": "answerback.fleet-writer/1", "ceilings": []}', "ceilings: an object"),
        ('{"schema": "answerback.fleet-writer/1", "ceilings": {"a": 0}}', "ceilings: an object"),
        ('{"schema": "answerback.fleet-writer/1", "ceilings": {"a": 100}}', "ceilings: an object"),
        ('{"schema": "answerback.fleet-writer/1", "ceilings": {"a": true}}', "ceilings: an object"),
        ('{"schema": "answerback.fleet-writer/1", "ceilings": {"a": "6"}}', "ceilings: an object"),
        (
            '{"schema": "answerback.fleet-writer/1", "ceilings": {"local": 6}}',
            "ceilings: an object",
        ),
        ('{"schema": "answerback.fleet-writer/1", "dir": "fleet"}', "dir: an absolute path"),
        ('{"schema": "answerback.fleet-writer/1", "dir": 7}', "dir: an absolute path"),
        ('{"schema": "answerback.fleet-writer/1", "lane_label": "lane"}', "lane_label: K=V"),
        ('{"schema": "answerback.fleet-writer/1", "lane_label": null}', "lane_label: K=V"),
    ],
)
def test_a_file_that_breaks_a_rule_is_refused_with_the_rule(
    tmp_path: Path, text: str, rule: str
) -> None:
    path = tmp_path / "s.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match=f"{path} cannot be used: .*{rule}"):
        settings.read(path)


def test_a_file_that_is_missing_too_large_or_not_json_is_refused_as_unreadable(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(SettingsError, match=f"{missing} cannot be read: No such file"):
        settings.read(missing)
    large = tmp_path / "large.json"
    large.write_bytes(b"[" + b" " * settings.READ_LIMIT + b"]")
    with pytest.raises(SettingsError, match="cannot be read: larger than"):
        settings.read(large)
    broken = tmp_path / "broken.json"
    broken.write_bytes(b'{"schema": \xff')
    with pytest.raises(SettingsError, match="cannot be read: not a JSON document in UTF-8"):
        settings.read(broken)


def test_a_file_of_exactly_64_kib_is_read_and_one_byte_more_is_refused(tmp_path: Path) -> None:
    head = b'{"schema": "answerback.fleet-writer/1", "machines": ["build-a"]'
    exact = tmp_path / "exact.json"
    exact.write_bytes(head + b" " * (settings.READ_LIMIT - len(head) - 1) + b"}")
    assert exact.stat().st_size == settings.READ_LIMIT == 64 * 1024
    assert settings.read(exact).machines == ("build-a",)
    over = tmp_path / "over.json"
    over.write_bytes(head + b" " * (settings.READ_LIMIT - len(head)) + b"}")
    assert over.stat().st_size == settings.READ_LIMIT + 1
    with pytest.raises(SettingsError, match=f"{over} cannot be read: larger than 65536 bytes"):
        settings.read(over)


def test_a_settings_path_that_is_a_folder_or_a_device_is_refused_as_no_regular_file(
    tmp_path: Path,
) -> None:
    for path in (tmp_path, Path("/dev/null")):
        with pytest.raises(SettingsError, match=f"{path} cannot be read: not a regular file"):
            settings.read(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFO support")
def test_a_settings_path_that_is_a_fifo_is_refused_without_waiting_for_its_writer(
    tmp_path: Path,
) -> None:
    """A FIFO holds an open for reading until a writer comes; the writer must not wait there."""
    fifo = tmp_path / "s.json"
    os.mkfifo(fifo, 0o600)
    outcome: list[BaseException | FileSettings] = []

    def attempt() -> None:
        try:
            outcome.append(settings.read(fifo))
        except BaseException as error:  # noqa: BLE001 - carried to the test thread as is
            outcome.append(error)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    if thread.is_alive():
        # Let the blocked read end, so no thread is left behind, then fail.
        holder = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.close(holder)
        thread.join(timeout=2.0)
        pytest.fail("the read waited for a writer of the FIFO")
    (result,) = outcome
    assert isinstance(result, SettingsError)
    assert str(result) == f"{fifo} cannot be read: not a regular file"


def test_a_settings_path_that_is_a_link_to_a_regular_file_is_followed(tmp_path: Path) -> None:
    """A symbolic link at the leaf is the user's choice; what it leads to is checked."""
    real = tmp_path / "real.json"
    settings.write(real, _good())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert settings.read(link) == _good()
    to_folder = tmp_path / "to-folder.json"
    to_folder.symlink_to(tmp_path)
    with pytest.raises(SettingsError, match="not a regular file"):
        settings.read(to_folder)


def test_a_path_at_or_below_the_published_folder_is_inside_it(tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    assert settings.inside(folder, folder)
    assert settings.inside(folder / "fleet-writer.json", folder)
    assert settings.inside(folder / "deep" / "s.json", folder)
    assert settings.inside(tmp_path / "other" / ".." / "fleet" / "s.json", folder)
    assert not settings.inside(tmp_path / "fleet-writer.json", folder)
    assert not settings.inside(tmp_path / "fleet2" / "s.json", folder), "a name that starts alike"


def test_a_link_inside_the_published_folder_counts_as_inside_wherever_it_points(
    tmp_path: Path,
) -> None:
    """Re-check X1: the entry's location decides as well as its target. A link at
    ``fleet/settings.json`` to a file outside is still an entry the publisher would refuse, so
    configure must not replace it with a file and serve must refuse it with the overlap rule."""
    folder = tmp_path / "fleet"
    folder.mkdir()
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}")
    link = folder / "settings.json"
    link.symlink_to(outside)
    assert settings.inside(link, folder)
    assert not settings.inside(outside, folder)
    away = tmp_path / "away.json"
    away.symlink_to(folder / "fleet.json")
    assert settings.inside(away, folder), "an entry outside that leads inside is inside too"


# ------------------------------------------------------------------ through the command line


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Any:
    fleet = Fleet({"local": linux(), "worker-a": linux(), "worker-b": linux()})
    service = Service()
    monkeypatch.setattr(fleet_cli, "make_runner", lambda binary, session: (fleet, binary))
    monkeypatch.setattr(fleet_cli, "make_call", lambda path: service)
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "absent" / "config.json")

    def invoke(arguments: list[str]) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["answerback", "fleet", *arguments])
        code: Any = 0
        try:
            cli.main()
        except SystemExit as error:
            code = error.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    invoke.fleet = fleet  # type: ignore[attr-defined]
    return invoke


def test_configure_writes_the_file_with_mode_0600_and_names_it(run: Any, tmp_path: Path) -> None:
    file = tmp_path / "conf" / "fleet-writer.json"
    folder = tmp_path / "published"
    code, out, err = run(
        [
            "configure",
            "--settings",
            str(file),
            "--machine",
            "worker-a",
            "--machine",
            "worker-b",
            "--ceiling",
            "worker-a=4",
            "--dir",
            str(folder),
            "--lane-label",
            "team.lane=1",
        ]
    )
    assert (code, err) == (0, "")
    assert out == f"fleet settings written to {file}\n"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert settings.read(file) == FileSettings(
        ("worker-a", "worker-b"), {"worker-a": 4}, folder, "team.lane=1"
    )


def test_configure_without_a_path_writes_beside_the_configuration_file(
    run: Any, tmp_path: Path
) -> None:
    code, out, _ = run(["configure", "--machine", "worker-a"])
    assert code == 0
    file = tmp_path / "absent" / "fleet-writer.json"
    assert out == f"fleet settings written to {file}\n"
    assert settings.read(file) == FileSettings(("worker-a",))
    assert not (tmp_path / "absent" / "fleet").exists(), "the published folder is not made"


def test_configure_makes_a_relative_folder_absolute(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    file = tmp_path / "s.json"
    code, _, _ = run(["configure", "--settings", str(file), "--dir", "out/fleet"])
    assert code == 0
    assert settings.read(file).dir == tmp_path / "out" / "fleet"


@pytest.mark.parametrize(
    "arguments",
    [
        ["configure", "--machine", "local"],
        ["configure", "--ceiling", "worker-a=0"],
        ["configure", "--lane-label", "lane"],
    ],
)
def test_configure_refuses_a_wrong_option_with_exit_code_2(run: Any, arguments: list[str]) -> None:
    code, out, err = run([*arguments, "--settings", "/nowhere/s.json"])
    assert code == 2 and out == ""
    assert "usage: answerback fleet configure" in err
    assert not Path("/nowhere/s.json").exists()


def test_configure_with_16_workers_is_refused_and_writes_nothing(run: Any, tmp_path: Path) -> None:
    file = tmp_path / "s.json"
    names = [word for n in range(16) for word in ("--machine", f"m{n}")]
    code, _, err = run(["configure", "--settings", str(file), *names])
    assert code == 2 and "at most 15 workers" in err
    assert not file.exists()


def test_configure_that_cannot_write_exits_1_with_one_line(run: Any, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    code, out, err = run(["configure", "--settings", str(blocker / "s.json")])
    assert code == 1 and out == ""
    assert err.startswith(f"fleet settings not written to {blocker / 's.json'}: ")
    assert len(err.strip().splitlines()) == 1


def test_configure_refuses_a_settings_path_inside_the_published_folder(
    run: Any, tmp_path: Path
) -> None:
    """R3: the publisher would refuse the folder; configure says so and writes nothing."""
    folder = tmp_path / "published"
    file = folder / "fleet-writer.json"
    code, out, err = run(["configure", "--settings", str(file), "--dir", str(folder)])
    assert (code, out) == (2, "")
    assert (
        err
        == f"fleet settings file {file} is inside the published folder {folder}; choose another\n"
    )
    assert not folder.exists()
    # The same with the default settings path and a --dir that is its folder or an ancestor.
    code, out, err = run(["configure", "--dir", str(tmp_path / "absent")])
    assert code == 2 and "inside the published folder" in err
    code, out, err = run(["configure", "--dir", str(tmp_path)])
    assert code == 2 and "inside the published folder" in err
    assert not (tmp_path / "absent").exists()
    # And the path equal to the folder itself.
    code, out, err = run(["configure", "--settings", str(folder), "--dir", str(folder)])
    assert code == 2 and f"fleet settings file {folder} is inside the published folder" in err


def test_configure_accepts_a_settings_path_beside_the_published_folder(
    run: Any, tmp_path: Path
) -> None:
    code, out, _ = run(
        ["configure", "--settings", str(tmp_path / "s.json"), "--dir", str(tmp_path / "fleet")]
    )
    assert code == 0 and out == f"fleet settings written to {tmp_path / 's.json'}\n"
    code, out, _ = run(["configure", "--settings", str(tmp_path / "fleet-writer.json")])
    assert code == 0, "the default published folder is absent/fleet, not the config folder"


def test_serve_refuses_a_settings_file_inside_the_published_folder(
    run: Any, tmp_path: Path
) -> None:
    """R3: for serve too, before the publisher opens the folder or anything is measured."""
    folder = tmp_path / "published"
    file = folder / "fleet-writer.json"
    settings.write(file, FileSettings(("worker-a",), {}, folder))
    code, out, err = run(["serve", "--once", "--settings", str(file)])
    assert (code, out) == (2, "")
    assert (
        err
        == f"fleet settings file {file} is inside the published folder {folder}; choose another\n"
    )
    assert os.listdir(folder) == ["fleet-writer.json"], "nothing was published"
    assert run.fleet.probed() == [], "nothing was measured"
    # The default folder of the configuration, with the file inside it.
    default = tmp_path / "absent" / "fleet"
    inside = default / "s.json"
    settings.write(inside, FileSettings())
    code, _, err = run(["serve", "--once", "--settings", str(inside)])
    assert code == 2 and f"inside the published folder {default}" in err
    assert os.listdir(default) == ["s.json"]


def test_serve_refuses_a_settings_path_that_is_no_regular_file(run: Any, tmp_path: Path) -> None:
    code, out, err = run(["serve", "--once", "--settings", str(tmp_path)])
    assert (code, out) == (2, "")
    assert f"{tmp_path} cannot be read: not a regular file" in err
    assert not (tmp_path / "absent").exists()


def test_serve_follows_a_settings_path_that_is_a_link_to_a_regular_file(
    run: Any, tmp_path: Path
) -> None:
    real = tmp_path / "real.json"
    settings.write(real, FileSettings(("worker-b",)))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    code, _, err = run(["serve", "--once", "--settings", str(link)])
    assert code == 0, err
    assert run.fleet.probed() == ["worker-b"]


def test_serve_once_reads_the_settings_file(run: Any, tmp_path: Path) -> None:
    file = tmp_path / "s.json"
    folder = tmp_path / "published" / "fleet"
    settings.write(file, FileSettings(("worker-b",), {"worker-b": 7}, folder, "team.lane=1"))
    code, _, err = run(["serve", "--once", "--settings", str(file)])
    assert code == 0, err
    assert os.listdir(folder) == ["fleet.json"]
    doc = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    assert [m["name"] for m in doc["machines"]] == [None, "worker-b"]
    assert doc["machines"][1]["reading"]["agents"] == {"active": 0, "ceiling": 7, "lanes": 0}
    assert run.fleet.probed() == ["worker-b"], "only the worker of the file is measured"
    labelled = [argv for argv in run.fleet.calls if argv[1:4] == ["ls", "--json", "--label"]]
    assert labelled == [["hc", "ls", "--json", "--label", "team.lane=1"]]


def test_serve_with_a_settings_file_without_a_folder_uses_the_default_folder(
    run: Any, tmp_path: Path
) -> None:
    file = tmp_path / "s.json"
    settings.write(file, FileSettings())
    code, _, err = run(["serve", "--once", "--settings", str(file)])
    assert code == 0, err
    assert (tmp_path / "absent" / "fleet" / "fleet.json").is_file()


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (["--machine", "worker-a"], "--machine"),
        (["--ceiling", "worker-a=2"], "--ceiling"),
        (["--dir", "/tmp/x"], "--dir"),
        (["--lane-label", "a=b"], "--lane-label"),
        (["--dir", "/tmp/x", "--machine", "worker-a"], "--dir, --machine"),
    ],
)
def test_serve_refuses_settings_together_with_an_option_of_the_file(
    run: Any, tmp_path: Path, extra: list[str], named: str
) -> None:
    file = tmp_path / "s.json"
    settings.write(file, FileSettings())
    code, out, err = run(["serve", "--once", "--settings", str(file), *extra])
    assert code == 2 and out == ""
    assert f"--settings cannot be combined with {named}" in err
    assert not (tmp_path / "absent").exists(), "nothing is published"


def test_serve_with_a_settings_file_that_cannot_be_used_exits_2_and_publishes_nothing(
    run: Any, tmp_path: Path
) -> None:
    file = tmp_path / "s.json"
    file.write_text('{"schema": "answerback.fleet-writer/1", "machines": ["local"]}', "utf-8")
    code, out, err = run(["serve", "--once", "--settings", str(file)])
    assert code == 2 and out == ""
    assert f"{file} cannot be used: machines: a list of names" in err
    assert not (tmp_path / "absent").exists()
    missing = tmp_path / "missing.json"
    code, _, err = run(["serve", "--once", "--settings", str(missing)])
    assert code == 2 and f"{missing} cannot be read" in err


def test_the_options_of_serve_work_as_before_the_settings_file(run: Any, tmp_path: Path) -> None:
    folder = tmp_path / "out"
    code, _, err = run(
        [
            "serve",
            "--once",
            "--dir",
            str(folder),
            "--machine",
            "worker-a",
            "--ceiling",
            "worker-a=4",
        ]
    )
    assert code == 0, err
    doc = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    assert [m["name"] for m in doc["machines"]] == [None, "worker-a"]
    assert doc["machines"][1]["reading"]["agents"]["ceiling"] == 4
    labelled = [argv for argv in run.fleet.calls if argv[1:4] == ["ls", "--json", "--label"]]
    assert labelled == [["hc", "ls", "--json", "--label", "ab.lane=1"]], "the default label"


# ------------------------------------------------------------------ health.service.version


def _snapshot_health(version: Any) -> dict[str, Any]:
    return {
        "running": True,
        "accepting": True,
        "instance_id": "svc",
        "started": 1.0,
        "version": version,
    }


async def test_the_document_carries_the_bridges_version_and_the_writers_own_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``health.service.version`` is the snapshot's ``health.version``; ``writer.version`` the
    writer's. R2: the two answers of the service carry different figures, so the test tells the
    snapshot's from the one of ``status``."""
    monkeypatch.setattr(loop, "__version__", "0.0.0+writer-under-test")
    service = Service()
    service.up = True
    service.status["version"] = "1.1.1+status-under-test"
    service.snapshot = "answer"
    service.snapshot_answer = {
        "schema": "answerback.fleet_snapshot/1",
        "health": _snapshot_health("9.9.9+snapshot-under-test"),
        "tasks": service.tasks,
    }
    w, _, sink = writer(Fleet({"local": linux()}), service)
    await w.once()
    doc = sink.docs[-1]
    assert doc["receipts"]["source"] == "fleet_snapshot", "the snapshot answered and was used"
    assert doc["health"]["service"]["version"] == "9.9.9+snapshot-under-test"
    assert doc["writer"]["version"] == "0.0.0+writer-under-test"


async def test_a_status_without_a_version_takes_it_from_the_snapshot() -> None:
    """R2: an older status shape without ``version``; the snapshot's ``health.version`` is used."""
    service = Service()
    service.up = True
    del service.status["version"]
    service.snapshot = "answer"
    service.snapshot_answer = {
        "health": _snapshot_health("9.9.9+snapshot-under-test"),
        "tasks": service.tasks,
    }
    w, _, sink = writer(Fleet({"local": linux()}), service)
    await w.once()
    doc = sink.docs[-1]
    assert doc["receipts"]["source"] == "fleet_snapshot"
    assert doc["health"]["service"]["version"] == "9.9.9+snapshot-under-test"


async def test_without_a_usable_snapshot_the_version_is_the_one_of_status() -> None:
    """The service refuses ``fleet_snapshot`` (an older bridge), or its answer says no version."""
    service = Service()
    service.up = True
    service.status["version"] = "1.1.1+status-under-test"
    w, _, sink = writer(Fleet({"local": linux()}), service)
    await w.once()
    doc = sink.docs[-1]
    assert doc["receipts"]["source"] == "status+task_list"
    assert doc["health"]["service"]["version"] == "1.1.1+status-under-test"
    for health in (None, {}, _snapshot_health(None), _snapshot_health(7), "health"):
        service = Service()
        service.up = True
        service.status["version"] = "1.1.1+status-under-test"
        service.snapshot = "answer"
        service.snapshot_answer = {"health": health, "tasks": service.tasks}
        w, _, sink = writer(Fleet({"local": linux()}), service)
        await w.once()
        doc = sink.docs[-1]
        assert doc["receipts"]["source"] == "fleet_snapshot"
        assert doc["health"]["service"]["version"] == "1.1.1+status-under-test", health


async def test_a_service_that_does_not_answer_publishes_no_version_for_it() -> None:
    w, _, sink = writer(Fleet({"local": linux()}), Service())
    await w.once()
    doc = sink.docs[-1]
    assert doc["health"]["service"] == {
        "state": "unreachable",
        "accepting": None,
        "started": None,
        "version": None,
        "instance_id": None,
    }
    assert doc["writer"]["version"] == loop.__version__
