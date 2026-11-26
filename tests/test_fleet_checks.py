"""fleet: each condition of the folder and lock checks has a test of its own."""

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import no_descriptor_left, process_unchanged

from answerback_bridge.fleet import publish

OTHER_UID = os.getuid() + 4242


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


class Reported:
    """A stat result as the system call reports it, with another owner."""

    def __init__(self, real: os.stat_result) -> None:
        self._real = real
        self.st_uid = OTHER_UID

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _folders(tmp_path: Path) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
        (tmp_path / name).chmod(0o755)


def _modes_kept(tmp_path: Path) -> None:
    for name in ("fleet", "fleet.stage"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o755


def test_a_published_folder_of_another_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path)
    ino = (tmp_path / "fleet").stat().st_ino
    real = os.fstat

    def fstat(fd: int) -> Any:
        info = real(fd)
        return Reported(info) if info.st_ino == ino else info

    monkeypatch.setattr(publish.os, "fstat", fstat)
    with pytest.raises(publish.PublishRefused, match="folder owned by the current user"):
        publish.Publisher(tmp_path / "fleet").open()
    _modes_kept(tmp_path)
    assert os.listdir(tmp_path / "fleet.stage") == []


def test_an_entry_of_another_owner_under_an_allowed_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path)
    (tmp_path / "fleet" / "fleet.json").write_text("keep", encoding="utf-8")
    real = os.stat

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        info = real(path, *args, **kwargs)
        return Reported(info) if path == "fleet.json" else info

    monkeypatch.setattr(publish.os, "stat", fake_stat)
    with pytest.raises(publish.PublishRefused, match="regular file of the current user"):
        publish.Publisher(tmp_path / "fleet").open()
    _modes_kept(tmp_path)
    assert (tmp_path / "fleet" / "fleet.json").read_text(encoding="utf-8") == "keep"


def test_a_lock_of_another_owner_on_its_descriptor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path)
    real = os.fstat

    def fstat(fd: int) -> Any:
        info = real(fd)
        return Reported(info) if stat.S_ISREG(info.st_mode) else info  # only the lock

    monkeypatch.setattr(publish.os, "fstat", fstat)
    with pytest.raises(publish.PublishRefused, match="regular file of the current user"):
        publish.Publisher(tmp_path / "fleet").open()
    _modes_kept(tmp_path)


def test_a_lock_replaced_by_a_fifo_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path)
    lock = tmp_path / "fleet.stage" / "writer.lock"
    lock.write_bytes(b"")
    real = publish._inspect

    def inspect_then_fifo(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if path.name == "fleet.stage":
            lock.unlink()
            os.mkfifo(lock)

    monkeypatch.setattr(publish, "_inspect", inspect_then_fifo)
    with deadline("the start with a FIFO under the lock's name"):
        with pytest.raises(publish.PublishRefused, match="must be a regular file"):
            publish.Publisher(tmp_path / "fleet").open()
    _modes_kept(tmp_path)
    assert stat.S_ISFIFO(lock.lstat().st_mode)


# ---------------------------------------------------------------- a FIFO given as a folder

OTHER = {"fleet": ("fleet.stage", "writer.lock"), "fleet.stage": ("fleet", "fleet.json")}


@pytest.mark.parametrize("other_there", [False, True])
@pytest.mark.parametrize("name", ["fleet", "fleet.stage"])
def test_a_fifo_given_as_a_folder_is_refused_at_once(
    tmp_path: Path, name: str, other_there: bool
) -> None:
    fifo = tmp_path / name
    os.mkfifo(fifo)
    fifo.chmod(0o640)  # mkfifo's mode passes through the umask; chmod does not
    other_name, writer_file = OTHER[name]
    other = tmp_path / other_name
    if other_there:
        other.mkdir()
        (other / writer_file).write_bytes(b"keep")
        (other / writer_file).chmod(0o600)
        other.chmod(0o755)
    with deadline(f"the start with a FIFO as {name}"):
        with pytest.raises(publish.PublishRefused, match="is a symbolic link or not a folder"):
            publish.Publisher(tmp_path / "fleet").open()
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert stat.S_IMODE(fifo.lstat().st_mode) == 0o640
    if other_there:
        assert stat.S_IMODE(other.stat().st_mode) == 0o755
        assert os.listdir(other) == [writer_file]
        assert (other / writer_file).read_bytes() == b"keep"
        assert stat.S_IMODE((other / writer_file).stat().st_mode) == 0o600
    else:
        assert not other.exists()
