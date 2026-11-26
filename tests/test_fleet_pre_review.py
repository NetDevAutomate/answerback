"""fleet: guards of the publisher that further single edits showed untested.

A name that is allowed only in the other folder, a lock file with no name, the descriptor of a
folder this start made, every write before the sync.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_lifetime import _open_descriptors, no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


@pytest.mark.parametrize(
    ("there", "name"),
    [("fleet", "fleet.json.new"), ("fleet", "writer.lock"), ("fleet.stage", "fleet.json")],
)
def test_an_existing_folder_with_a_name_allowed_only_in_the_other_folder_is_refused(
    tmp_path: Path, there: str, name: str
) -> None:
    folder = tmp_path / there
    folder.mkdir(mode=0o700)
    (folder / name).write_text("a regular file of the current user", encoding="utf-8")
    other = tmp_path / ("fleet.stage" if there == "fleet" else "fleet")
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(tmp_path / "fleet").open()
    assert str(refused.value) == f"{folder} holds files the fleet writer does not own: {name}"
    assert (folder / name).read_text(encoding="utf-8") == "a regular file of the current user"
    assert not other.exists()


def test_a_lock_file_whose_name_is_gone_before_its_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    real = publish.Publisher._open_lock

    def opened_then_unnamed(self: publish.Publisher, stage_fd: int) -> int:
        fd = real(self, stage_fd)
        os.unlink("writer.lock", dir_fd=stage_fd)  # another process removes the name
        return fd

    before = _open_descriptors()
    first = publish.Publisher(folder)
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.Publisher, "_open_lock", opened_then_unnamed)
            # the words of this refusal are not fixed here
            with pytest.raises(publish.PublishRefused):
                first.open()
    finally:
        first.close()  # closes nothing after a refused start; a start that went on is closed
    assert _open_descriptors() == before
    p = publish.Publisher(folder)
    p.open()
    p.close()
    assert stat.S_ISREG(os.stat(tmp_path / "fleet.stage" / "writer.lock").st_mode)


def test_a_made_folder_that_is_not_empty_at_its_open_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = tmp_path / "fleet.stage"
    real = os.mkdir

    def made_then_filled(path: Any, *args: Any, **kwargs: Any) -> None:
        real(path, *args, **kwargs)
        if Path(path) == made:
            (made / "fleet.json.new").write_text("another process put this here", encoding="utf-8")

    before = _open_descriptors()
    with monkeypatch.context() as m:
        m.setattr(publish.os, "mkdir", made_then_filled)
        with pytest.raises(publish.PublishRefused, match="does not own"):
            publish.Publisher(tmp_path / "fleet").open()
    assert _open_descriptors() == before


def test_a_made_folder_whose_mode_cannot_be_set_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.fchmod
    raised: list[OSError] = []

    def fchmod(fd: int, mode: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            error = OSError(errno.EIO, "injected I/O error at the mode of a made folder")
            raised.append(error)
            raise error
        real(fd, mode)

    before = _open_descriptors()
    with monkeypatch.context() as m:
        m.setattr(publish.os, "fchmod", fchmod)
        with pytest.raises(OSError) as failed:
            publish.Publisher(tmp_path / "fleet").open()  # makes both folders
    assert failed.value is raised[0]
    assert _open_descriptors() == before


def test_every_write_comes_before_the_sync_of_the_whole_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _doc(1, pad=4000)  # several kilobytes: a sync before the writes sees a short file
    assert len(data) > 4000
    calls: list[str] = []
    sizes: list[int] = []
    real_write, real_fsync, real_replace = os.write, os.fsync, os.replace

    def write(fd: int, chunk: Any) -> int:
        written = real_write(fd, chunk)  # the bytes are passed on unchanged
        calls.append("write")
        return written

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            calls.append("fsync file")
            sizes.append(info.st_size)
        else:
            calls.append("fsync folder")
        real_fsync(fd)

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        calls.append("replace")
        real_replace(src, dst, **kwargs)

    folder = tmp_path / "fleet"
    p = publish.Publisher(folder)
    p.open()
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", write)
            m.setattr(publish.os, "fsync", fsync)
            m.setattr(publish.os, "replace", replace)
            p.publish(data)
    finally:
        p.close()
    assert sizes == [len(data)]
    writes = calls.count("write")
    assert writes >= 1
    assert calls == ["write"] * writes + ["fsync file", "replace", "fsync folder"]
    assert (folder / "fleet.json").read_bytes() == data
