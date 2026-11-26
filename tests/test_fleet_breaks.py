"""fleet: the publisher's steps whose break shows from outside, each with a test."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import _open_descriptors, no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@contextlib.contextmanager
def _umask(mask: int) -> Iterator[None]:
    before = os.umask(mask)
    try:
        yield
    finally:
        os.umask(before)


def _fail_fchmod_of_files(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected I/O error")
        real(fd, mode)

    monkeypatch.setattr(publish.os, "fchmod", fchmod)


# ---------------------------------------------------------------- _open_folder


@pytest.mark.parametrize("name", ["fleet", "fleet.stage"])
def test_a_symbolic_link_given_as_a_folder_is_refused_with_its_own_words(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / name).symlink_to(tmp_path / "other")
    with pytest.raises(publish.PublishRefused, match="is a symbolic link or not a folder"):
        publish.Publisher(tmp_path / "fleet").open()


@pytest.mark.skipif(os.getuid() == 0, reason="an administrator can open every folder")
def test_a_folder_that_cannot_be_opened_is_refused_and_keeps_its_mode(tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    folder.chmod(0o000)
    try:
        with pytest.raises(publish.PublishRefused, match="cannot be opened"):
            publish.Publisher(folder).open()
        assert _mode(folder) == 0o000
        assert not (tmp_path / "fleet.stage").exists()
    finally:
        folder.chmod(0o700)


# ---------------------------------------------------------------- modes under a strict umask


def test_a_start_and_a_publication_under_a_strict_umask_give_0700_and_0600(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fleet"
    with _umask(0o277):  # what mkdir and open make is owner-read only
        p = publish.Publisher(folder)
        p.open()
        try:
            p.publish(_doc(1))
        finally:
            p.close()
    assert (_mode(folder), _mode(tmp_path / "fleet.stage")) == (0o700, 0o700)
    assert _mode(folder / "fleet.json") == 0o600


def test_a_lock_left_by_a_failed_start_has_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_fchmod_of_files(monkeypatch)
    with _umask(0o022):
        with pytest.raises(OSError, match="injected"):
            publish.Publisher(tmp_path / "fleet").open()
    assert _mode(tmp_path / "fleet.stage" / "writer.lock") == 0o600


# ---------------------------------------------------------------- _take_lock


def test_the_lock_is_checked_before_flock_also_when_its_other_name_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    real = publish._inspect

    def inspect_then_link(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if path.name == "fleet.stage":
            os.link(outside, path / "writer.lock")

    monkeypatch.setattr(publish, "_inspect", inspect_then_link)
    other = os.open(outside, os.O_RDONLY)
    try:
        fcntl.flock(other, fcntl.LOCK_EX)  # another program locks the outside file
        with deadline("the start with a locked file under the lock's name"):
            with pytest.raises(publish.PublishRefused, match="second name"):
                publish.Publisher(tmp_path / "fleet").open()
    finally:
        os.close(other)
    assert outside.read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------- close


def test_close_releases_every_descriptor_of_the_publisher(tmp_path: Path) -> None:
    before = _open_descriptors()
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    assert len(_open_descriptors() - before) == 3
    p.close()
    assert _open_descriptors() - before == set()


# ---------------------------------------------------------------- publish


def test_a_publisher_that_is_not_open_writes_nothing_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    p = publish.Publisher(tmp_path / "fleet")
    with pytest.raises(publish.PublishError, match="not open"):
        p.publish(_doc(1))
    assert os.listdir(tmp_path) == []


def test_a_write_that_takes_only_part_of_the_bytes_is_continued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write

    def short_write(fd: int, data: Any) -> int:  # what the system may do: write a part
        return real(fd, bytes(data[:512]))

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    data = _doc(1, pad=4000)
    assert len(data) > 2048
    try:
        monkeypatch.setattr(publish.os, "write", short_write)
        p.publish(data)
    finally:
        p.close()
    assert (tmp_path / "fleet" / "fleet.json").read_bytes() == data


def test_a_failed_sync_of_the_folder_after_the_rename_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected I/O error")
        real(fd)

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        monkeypatch.setattr(publish.os, "fsync", fsync)
        p.publish(_doc(1))
    finally:
        p.close()
    written = json.loads((tmp_path / "fleet" / "fleet.json").read_bytes())
    assert written["written_at"] == 1001.0


def test_the_staged_file_is_synced_then_renamed_then_the_folder_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[int, int] | None]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        kind = "folder" if stat.S_ISDIR(info.st_mode) else "file"
        calls.append((f"fsync {kind}", (info.st_dev, info.st_ino)))
        real_fsync(fd)

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        calls.append(("replace", None))
        real_replace(src, dst, **kwargs)

    folder = tmp_path / "fleet"
    p = publish.Publisher(folder)
    p.open()
    try:
        monkeypatch.setattr(publish.os, "fsync", fsync)
        monkeypatch.setattr(publish.os, "replace", replace)
        p.publish(_doc(1))
    finally:
        p.close()

    def identity(path: Path) -> tuple[int, int]:
        info = path.stat()
        return (info.st_dev, info.st_ino)

    # The synced file is the staged file, which after the rename is ``fleet.json`` (the same
    # inode); the synced folder is the published folder, not the staging folder.
    assert calls == [
        ("fsync file", identity(folder / "fleet.json")),
        ("replace", None),
        ("fsync folder", identity(folder)),
    ]
    assert identity(folder) != identity(tmp_path / "fleet.stage")
    assert json.loads((folder / "fleet.json").read_bytes())["written_at"] == 1001.0


def test_nothing_the_writer_makes_has_a_wider_mode_even_before_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: dict[str, list[int]] = {}
    real_mkdir, real_open = os.mkdir, os.open

    def mkdir(path: Any, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        made.setdefault(Path(path).name, []).append(mode)
        real_mkdir(path, mode, *args, **kwargs)

    def open_(path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            made.setdefault(Path(path).name, []).append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(publish.os, "mkdir", mkdir)
    monkeypatch.setattr(publish.os, "open", open_)
    with _umask(0):
        p = publish.Publisher(tmp_path / "place" / "fleet")
        p.open()
        try:
            p.publish(_doc(1))
        finally:
            p.close()
    # A missing parent folder is made by pathlib with its own default mode: not one of the
    # writer's folders, so it is left out here.
    made.pop("place", None)
    assert made == {
        "fleet": [0o700],
        "fleet.stage": [0o700],
        "writer.lock": [0o600],
        "fleet.json.new": [0o600],
    }
