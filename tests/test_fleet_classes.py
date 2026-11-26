"""fleet: an exception that is no ``OSError``, at every system call of a publication.

Only an ``OSError`` of a publication becomes a ``PublishError``; the removal of the staged file
after a failed write and the last sync of the published folder drop only an ``OSError``. Any other
exception comes out of ``publish()`` as it is, the publisher still holds its lock and descriptors,
and the staged file's descriptor is closed.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_lifetime import (
    _open_descriptors,
    lock_state,
    no_descriptor_left,
    process_unchanged,
)
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


class _Unusual(BaseException):
    """An exception of none of the standard classes: it derives from ``BaseException`` alone."""


CLASSES = {
    "interrupt": KeyboardInterrupt,
    "exit": SystemExit,
    "runtime": RuntimeError,
    "own": _Unusual,
}


def _regular(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


# Each place: the calls it replaces, and whether the second document is published after it.
PLACES = [
    "open of the staged file",
    "mode of the staged file",
    "first write",
    "sync of the staged file",
    "close of the staged file",
    "rename",
    "removal after a failed write",
    "sync of the published folder",
]
SECOND_PUBLISHED = {"rename", "sync of the published folder"}


def _wrappers(
    place: str, error: BaseException, write_error: OSError
) -> dict[str, Callable[..., Any]]:
    """The replacements of ``os`` functions that raise ``error`` at ``place``."""
    real = {
        name: getattr(os, name)
        for name in ("open", "fchmod", "write", "fsync", "close", "replace", "unlink")
    }
    if place == "open of the staged file":

        def open_(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            if path == publish.STAGED:
                raise error  # without making the call
            return real["open"](path, flags, mode, dir_fd=dir_fd)

        return {"open": open_}
    if place == "mode of the staged file":

        def fchmod(fd: int, mode: int) -> None:
            real["fchmod"](fd, mode)
            if _regular(fd):
                raise error

        return {"fchmod": fchmod}
    if place == "first write":

        def write(fd: int, data: Any) -> int:
            real["write"](fd, data)
            raise error

        return {"write": write}
    if place == "sync of the staged file":

        def fsync(fd: int) -> None:
            real["fsync"](fd)
            if _regular(fd):
                raise error

        return {"fsync": fsync}
    if place == "close of the staged file":

        def close(fd: int) -> None:
            regular = _regular(fd)
            real["close"](fd)
            if regular:
                raise error

        return {"close": close}
    if place == "rename":

        def replace(*args: Any, **kwargs: Any) -> None:
            real["replace"](*args, **kwargs)
            raise error

        return {"replace": replace}
    if place == "removal after a failed write":

        def failing_write(fd: int, data: Any) -> int:
            raise write_error  # writes nothing

        def unlink(path: Any, *, dir_fd: int | None = None) -> None:
            real["unlink"](path, dir_fd=dir_fd)
            raise error

        return {"write": failing_write, "unlink": unlink}

    def fsync_folder(fd: int) -> None:
        real["fsync"](fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise error

    return {"fsync": fsync_folder}


def _another_publisher_publishes(folder: Path, data: bytes) -> None:
    other = publish.Publisher(folder)
    other.open()
    try:
        other.publish(data)
    finally:
        other.close()
    assert (folder / "fleet.json").read_bytes() == data


@pytest.mark.parametrize("kind", list(CLASSES))
@pytest.mark.parametrize("place", PLACES)
def test_an_exception_that_is_no_oserror_at_any_call_of_a_publication_comes_out_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str, kind: str
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    first, second, third = _doc(1), _doc(2), _doc(3)
    error = CLASSES[kind](f"injected {kind} at the {place}")
    write_error = OSError(errno.EIO, "injected error at the write")
    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(first)
        fields = (p._lock_fd, p._stage_fd, p._folder_fd)
        held = _open_descriptors()
        with monkeypatch.context() as m:
            for name, wrapper in _wrappers(place, error, write_error).items():
                m.setattr(publish.os, name, wrapper)
            with pytest.raises(BaseException) as failed:
                p.publish(second)
        assert failed.value is error
        assert not isinstance(failed.value, publish.PublishError)
        if place == "removal after a failed write":
            assert failed.value.__context__ is write_error
        published = second if place in SECOND_PUBLISHED else first
        assert (folder / "fleet.json").read_bytes() == published
        assert (p._lock_fd, p._stage_fd, p._folder_fd) == fields
        assert lock_state(stage) == "exclusive"
        assert _open_descriptors() == held  # the staged file's descriptor was closed
    finally:
        p.close()
    assert lock_state(stage) == "free"
    _another_publisher_publishes(folder, third)


def test_a_removal_that_fails_after_a_failed_write_reports_the_error_of_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"

    def failing_write(fd: int, data: Any) -> int:
        raise OSError(errno.EIO, "injected error at the write")

    def failing_unlink(path: Any, *, dir_fd: int | None = None) -> None:
        raise OSError(errno.EIO, "injected error at the removal")  # removes nothing

    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_doc(1))
        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", failing_write)
            m.setattr(publish.os, "unlink", failing_unlink)
            with pytest.raises(publish.PublishError) as failed:
                p.publish(_doc(2))
        assert str(failed.value) == "publication failed: injected error at the write"
        assert (folder / "fleet.json").read_bytes() == _doc(1)
    finally:
        p.close()
    _another_publisher_publishes(folder, _doc(3))
