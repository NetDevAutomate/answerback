"""fleet: guards of the publisher that single edits of five kinds showed untested.

The kinds: a flag, a mode or a limit; the order of two steps; a wrong descriptor or argument;
the policy for an error; state that is stored.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import _open_descriptors, no_descriptor_left, process_unchanged

from answerback_bridge.fleet import document, publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def test_a_start_that_finds_the_lock_held_changes_nothing_of_the_lock_file(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "fleet.stage"
    stage.mkdir(mode=0o700)
    lock = stage / "writer.lock"
    lock.touch()
    lock.chmod(0o640)  # by chmod: the mode of an open would go through the umask
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with deadline("a start beside a held lock"), pytest.raises(publish.LockHeld):
            publish.Publisher(tmp_path / "fleet").open()
        assert stat.S_IMODE(os.stat(lock).st_mode) == 0o640
    finally:
        os.close(holder)


def test_an_error_of_flock_other_than_would_block_is_passed_on_and_closes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "injected: no locks available")

    before = _open_descriptors()
    monkeypatch.setattr(publish.fcntl, "flock", no_locks)
    with pytest.raises(OSError) as failed:
        publish.Publisher(tmp_path / "fleet").open()
    assert type(failed.value) is OSError
    assert failed.value.errno == errno.ENOLCK
    assert failed.value.strerror == "injected: no locks available"
    assert _open_descriptors() == before


@pytest.mark.parametrize("name", ["fleet.json", "fleet.json.new"])
def test_a_folder_this_start_made_that_is_not_empty_at_its_open_is_refused_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # The writer's own names: a file of another name does not tell a check with the
    # writer's names from the check with none.
    made = tmp_path / ("fleet" if name == "fleet.json" else "fleet.stage")
    real = os.mkdir

    def made_then_filled(path: Any, *args: Any, **kwargs: Any) -> None:
        real(path, *args, **kwargs)
        if Path(path) == made:
            (made / name).write_text("another process put this here", encoding="utf-8")

    monkeypatch.setattr(publish.os, "mkdir", made_then_filled)
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(tmp_path / "fleet").open()
    assert str(refused.value) == f"{made} holds files the fleet writer does not own: {name}"
    assert (made / name).read_text(encoding="utf-8") == "another process put this here"


def test_a_leftover_that_cannot_be_removed_fails_the_start_with_the_systems_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    staged = tmp_path / "fleet.stage" / "fleet.json.new"
    real = fcntl.flock

    def locked_then_folder(fd: int, operation: int) -> None:
        real(fd, operation)
        os.mkdir(staged)  # after the folders are checked: a folder under the staged name

    before = _open_descriptors()
    with monkeypatch.context() as m:
        m.setattr(publish.fcntl, "flock", locked_then_folder)
        with pytest.raises(OSError):  # EISDIR on Linux, EPERM on macOS
            publish.Publisher(folder).open()
    assert staged.is_dir()
    assert _open_descriptors() == before
    os.rmdir(staged)
    p = publish.Publisher(folder)
    p.open()
    p.close()


def test_a_publisher_never_opened_refuses_a_bad_document_as_a_document(tmp_path: Path) -> None:
    problems = document.validate(b"{}")
    with pytest.raises(publish.PublishError) as refused:
        publish.Publisher(tmp_path / "fleet").publish(b"{}")
    assert str(refused.value) == "document refused: " + ", ".join(problems[:5])
