"""fleet: a close that fails.

These tests fix the rules of the publisher when a close fails: a descriptor is given to
``os.close`` at most once and forgotten before; every held descriptor is tried; out of a failed
start comes the error of the start, or, when a close of its cleanup raised an interrupt, the
first such interrupt, after every held descriptor was tried; ``close()`` raises the first error
of its closes, and an interrupt of a close goes out of ``close()`` at once, the fields it did
not reach staying stored. They do not rely on what the system does with a descriptor whose
close reported an error: each wrapper below decides whether it closes for real before it raises.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import _open_descriptors, no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish

Key = tuple[int, int, int]


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def _identity(fd: int) -> Key | None:
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _identity_of(path: Path) -> Key:
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


class _FailingClose:
    """A wrapper around ``os.close``: for a descriptor of one of the given files, once per
    file, it raises the given error. ``closes_first`` says whether it closes the descriptor
    for real before it raises; when it does not, the descriptor is noted in ``refused``.
    Descriptors in ``spare`` are closed as usual. ``numbers`` notes the number each of the
    given files had when its close was asked for, and ``calls`` each file whose close was
    asked for, in order."""

    def __init__(
        self,
        failures: dict[Key, BaseException],
        *,
        closes_first: bool,
        spare: frozenset[int] = frozenset(),
    ) -> None:
        self.failures = dict(failures)
        self.closes_first = closes_first
        self.spare = spare
        self.refused: list[int] = []
        self.numbers: dict[Key, int] = {}
        self.calls: list[Key] = []
        self.watch = set(failures)
        self.real = os.close

    def watching(self, *paths: Path) -> _FailingClose:
        self.watch.update(_identity_of(path) for path in paths)
        return self

    def __call__(self, fd: int) -> None:
        key = _identity(fd)
        if fd not in self.spare and key in self.watch:
            self.numbers.setdefault(key, fd)  # type: ignore[index]
            self.calls.append(key)  # type: ignore[arg-type]
        if fd not in self.spare and key in self.failures:
            error = self.failures.pop(key)  # type: ignore[arg-type]
            if self.closes_first:
                self.real(fd)
            else:
                self.refused.append(fd)
            raise error
        self.real(fd)


def _closes_then_raises(failures: dict[Key, BaseException], **more: object) -> _FailingClose:
    return _FailingClose(failures, closes_first=True, **more)  # type: ignore[arg-type]


def _raises_without_closing(failures: dict[Key, BaseException]) -> _FailingClose:
    return _FailingClose(failures, closes_first=False)


def _fail_once_for(path: Path, refused: list[int]) -> Callable[[int], None]:
    """Raises an ``OSError`` once for the descriptor of ``path``, without closing it."""
    wrapper = _raises_without_closing(
        {
            _identity_of(path): OSError(
                errno.EIO, "injected error at the close of the published folder"
            )
        }
    )
    wrapper.refused = refused
    return wrapper


def _close_left(refused: list[int], path: Path) -> None:
    """Close a descriptor whose close was refused, if it still is that folder."""
    for fd in refused:
        if _identity(fd) == _identity_of(path):
            os.close(fd)


@contextlib.contextmanager
def _others_take(number: int | Iterable[int], tmp_path: Path) -> Iterator[dict[int, Key]]:
    """Others open files until they hold every number in ``number``; all are closed at the end."""
    wanted = {number} if isinstance(number, int) else set(number)
    mine: list[int] = []
    try:
        while not wanted <= set(mine) and len(mine) < 32:
            mine.append(os.open(tmp_path, os.O_RDONLY))
        assert wanted <= set(mine), f"no file of others got the old numbers {sorted(wanted)}"
        yield {fd: _identity(fd) for fd in mine}  # type: ignore[misc]
    finally:
        for fd in mine:
            if _identity(fd) == _identity_of(tmp_path):
                os.close(fd)


def _unchanged(others: dict[int, Key]) -> bool:
    return all(_identity(fd) == key for fd, key in others.items())


def _two_folders(tmp_path: Path) -> tuple[Path, Path]:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    folder.mkdir()
    stage.mkdir()
    (stage / "writer.lock").touch()
    return folder, stage


def _failing_folder_mode(found: list[OSError]) -> Callable[[int, int], None]:
    """``os.fchmod`` that fails for a folder: with both folders there before, after the lock."""
    real = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            error = OSError(errno.EIO, "injected I/O error at the mode of a folder")
            found.append(error)
            raise error
        real(fd, mode)

    return fchmod


def _lock_number(stage: Path, before: set[int]) -> int:
    """The descriptor of the lock file that a start opened (one of the new ones)."""
    key = _identity_of(stage / "writer.lock")
    return next(fd for fd in sorted(_open_descriptors() - before) if _identity(fd) == key)


# ---------------------------------------------------------------- the staged file


def test_a_failed_close_of_the_staged_file_fails_the_publication_and_keeps_the_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    real = os.close
    failing = {"on": False}

    def close(fd: int) -> None:
        regular = failing["on"] and stat.S_ISREG(os.fstat(fd).st_mode)
        real(fd)  # closed for real, then the error
        if regular:
            raise OSError(errno.EIO, "injected error at the close of the staged file")

    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_doc(1))
        before = (folder / "fleet.json").read_bytes()
        monkeypatch.setattr(publish.os, "close", close)
        failing["on"] = True
        with pytest.raises(publish.PublishError) as failed:
            p.publish(_doc(2))
        failing["on"] = False
        assert str(failed.value) == (
            "publication failed: injected error at the close of the staged file"
        )
        assert (folder / "fleet.json").read_bytes() == before
        assert os.listdir(tmp_path / "fleet.stage") == ["writer.lock"]
        p.publish(_doc(3))
    finally:
        p.close()
    assert json.loads((folder / "fleet.json").read_bytes())["written_at"] == 1003.0


# ---------------------------------------------------------------- the cleanup of a failed start


def _three(folder: Path, stage: Path) -> dict[str, Key]:
    return {
        "lock": _identity_of(stage / "writer.lock"),
        "stage": _identity_of(stage),
        "folder": _identity_of(folder),
    }


class _Unusual(BaseException):
    """An exception of none of the standard classes: it derives from ``BaseException`` alone."""


# Every kind of exception a close may raise: an ``OSError``, and four that are not one.
NOT_OSERROR = ["interrupt", "exit", "runtime", "own"]


def _failure(kind: str, where: str) -> BaseException:
    text = f"injected {kind} at the close of the {where}"
    if kind == "oserror":
        return OSError(errno.EIO, text)
    return {
        "interrupt": KeyboardInterrupt,
        "exit": SystemExit,
        "runtime": RuntimeError,
        "own": _Unusual,
    }[kind](text)


@pytest.mark.parametrize("closes_first", [True, False], ids=["closes", "refuses"])
@pytest.mark.parametrize("kind", ["oserror", *NOT_OSERROR])
@pytest.mark.parametrize("where", ["lock", "stage", "folder"])
def test_the_cleanup_of_a_failed_start_gives_every_descriptor_to_close_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, kind: str, closes_first: bool
) -> None:
    folder, stage = _two_folders(tmp_path)
    files = _three(folder, stage)
    before = _open_descriptors()
    started: list[OSError] = []
    error = _failure(kind, where)
    wrapper = _FailingClose({files[where]: error}, closes_first=closes_first).watching(
        folder, stage, stage / "writer.lock"
    )
    p = publish.Publisher(folder)
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "fchmod", _failing_folder_mode(started))
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(BaseException) as failed:
                p.open()
        assert len(started) == 1
        # Each of the three once, the last opened first.
        assert wrapper.calls == [files["lock"], files["stage"], files["folder"]]
        if kind == "oserror":
            assert failed.value is started[0]  # the error of the start
        else:
            # The exception of the close, after every descriptor was tried; it shows the
            # error of the start as the one it happened during.
            assert failed.value is error
            assert failed.value.__context__ is started[0]
            assert failed.value.__suppress_context__ is False
            assert failed.value.__cause__ is None
        assert (p._lock_fd, p._stage_fd, p._folder_fd) == (None, None, None)
        assert _open_descriptors() - before == set(wrapper.refused)
        assert [_identity(fd) for fd in wrapper.refused] == ([] if closes_first else [files[where]])
        released = [n for k, n in wrapper.numbers.items() if n not in wrapper.refused]
        with _others_take(released, tmp_path) as others:
            p.close()  # closes nothing: no number is stored
            assert _unchanged(others)
    finally:
        _close_left(
            wrapper.refused, {"lock": stage / "writer.lock", "stage": stage}.get(where, folder)
        )
    assert _open_descriptors() == before
    second = publish.Publisher(folder)
    second.open()  # the lock is free
    second.close()


@pytest.mark.parametrize(
    ("lock", "stage_error", "out"),
    [
        ("interrupt", "interrupt", "lock"),
        ("oserror", "interrupt", "stage"),
        ("interrupt", "oserror", "lock"),
        ("oserror", "oserror", "start"),
        ("exit", "interrupt", "lock"),
        ("runtime", "exit", "lock"),
        ("oserror", "runtime", "stage"),
        ("own", "runtime", "lock"),
    ],
    ids=[
        "two-interrupts",
        "oserror-then-interrupt",
        "interrupt-then-oserror",
        "two-oserrors",
        "exit-then-interrupt",
        "runtime-then-exit",
        "oserror-then-runtime",
        "own-then-runtime",
    ],
)
def test_two_failing_closes_in_the_cleanup_give_the_first_interrupt_or_the_error_of_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: str, stage_error: str, out: str
) -> None:
    folder, stage = _two_folders(tmp_path)
    files = _three(folder, stage)
    before = _open_descriptors()
    started: list[OSError] = []
    errors = {"lock": _failure(lock, "lock"), "stage": _failure(stage_error, "staging folder")}
    wrapper = _closes_then_raises(
        {files["lock"]: errors["lock"], files["stage"]: errors["stage"]}
    ).watching(folder)
    with monkeypatch.context() as m:
        m.setattr(publish.os, "fchmod", _failing_folder_mode(started))
        m.setattr(publish.os, "close", wrapper)
        with pytest.raises(BaseException) as failed:
            publish.Publisher(folder).open()
    assert wrapper.calls == [files["lock"], files["stage"], files["folder"]]
    assert failed.value is (started[0] if out == "start" else errors[out])
    assert _open_descriptors() == before  # the published folder was closed too


def test_a_failed_close_in_the_cleanup_of_a_failed_start_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _two_folders(tmp_path)
    before = _open_descriptors()
    started: list[OSError] = []
    wrapper = _closes_then_raises(
        {_identity_of(stage / "writer.lock"): OSError(errno.EIO, "injected close error")}
    )
    with monkeypatch.context() as m:
        m.setattr(publish.os, "fchmod", _failing_folder_mode(started))
        m.setattr(publish.os, "close", wrapper)
        with pytest.raises(OSError):
            publish.Publisher(folder).open()
    assert not wrapper.failures
    after = _open_descriptors()
    for fd in after - before:
        if _identity(fd) in (_identity_of(folder), _identity_of(stage)):
            os.close(fd)
    assert after == before


def test_a_failed_close_in_the_cleanup_does_not_replace_the_error_of_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _two_folders(tmp_path)
    before = _open_descriptors()
    started: list[OSError] = []
    wrapper = _closes_then_raises(
        {_identity_of(stage / "writer.lock"): OSError(errno.EIO, "injected close error")}
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "fchmod", _failing_folder_mode(started))
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(OSError) as failed:
                publish.Publisher(folder).open()
        assert not wrapper.failures
        assert len(started) == 1
        assert failed.value is started[0]
    finally:
        for fd in _open_descriptors() - before:
            if _identity(fd) in (_identity_of(folder), _identity_of(stage)):
                os.close(fd)


def test_a_failed_close_in_the_cleanup_of_a_start_refused_by_a_held_lock_keeps_lockheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _two_folders(tmp_path)
    holder = os.open(stage / "writer.lock", os.O_RDWR)
    before = _open_descriptors()
    wrapper = _closes_then_raises(
        {_identity_of(stage / "writer.lock"): OSError(errno.EIO, "injected close error")},
        spare=frozenset({holder}),
    )
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with deadline("a start beside a held lock"), pytest.raises(publish.LockHeld) as refused:
                publish.Publisher(folder).open()
        assert not wrapper.failures
        assert str(refused.value) == f"another fleet writer already publishes to {folder}"
    finally:
        for fd in _open_descriptors() - before:
            if _identity(fd) in (_identity_of(folder), _identity_of(stage)):
                os.close(fd)
        os.close(holder)


# ---------------------------------------------------------------- close()


def test_a_failed_close_of_the_lock_in_close_is_raised_and_not_closed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    lock = _lock_number(tmp_path / "fleet.stage", before)
    error = OSError(errno.EIO, "injected error at the close of the lock")
    wrapper = _closes_then_raises({_identity_of(tmp_path / "fleet.stage" / "writer.lock"): error})
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(OSError) as failed:
                p.close()
        assert failed.value is error
        with _others_take(lock, tmp_path) as others:
            p.close()
            assert _unchanged(others)
    finally:
        with contextlib.suppress(OSError):
            p.close()


def test_a_failed_close_of_the_lock_in_close_still_closes_both_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    wrapper = _closes_then_raises(
        {_identity_of(tmp_path / "fleet.stage" / "writer.lock"): OSError(errno.EIO, "injected")}
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(OSError, match="injected"):
                p.close()
        assert _open_descriptors() == before
    finally:
        with contextlib.suppress(OSError):
            p.close()


def test_two_failed_closes_in_close_raise_the_first_and_leave_nothing_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    first = OSError(errno.EIO, "injected error at the close of the lock")
    second = OSError(errno.EIO, "injected error at the close of the staging folder")
    wrapper = _closes_then_raises(
        {_identity_of(stage / "writer.lock"): first, _identity_of(stage): second}
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(OSError) as failed:
                p.close()
        assert not wrapper.failures, "both failing closes were asked for"
        assert failed.value is first
        assert _open_descriptors() == before
    finally:
        with contextlib.suppress(OSError):
            p.close()


@pytest.mark.parametrize("kind", NOT_OSERROR)
@pytest.mark.parametrize("where", ["lock", "stage", "folder"])
def test_an_exception_that_is_no_oserror_goes_out_of_close_at_once_and_keeps_what_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, kind: str
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    files = _three(folder, stage)
    order = ["lock", "stage", "folder"]
    reached = order[: order.index(where) + 1]
    fields = {"lock": "_lock_fd", "stage": "_stage_fd", "folder": "_folder_fd"}
    numbers = {name: getattr(p, fields[name]) for name in order}
    error = _failure(kind, where)
    wrapper = _closes_then_raises({files[where]: error}).watching(
        folder, stage, stage / "writer.lock"
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(BaseException) as failed:
                p.close()
        assert failed.value is error
        assert wrapper.calls == [files[name] for name in reached]  # none after the failing one
        left = [name for name in order if name not in reached]
        for name in order:
            assert getattr(p, fields[name]) == (numbers[name] if name in left else None)
        assert _open_descriptors() - before == {numbers[name] for name in left}
        p.close()  # releases what was not reached
        assert (p._lock_fd, p._stage_fd, p._folder_fd) == (None, None, None)
        assert _open_descriptors() == before
    finally:
        p.close()
    second = publish.Publisher(folder)
    second.open()
    second.close()


@pytest.mark.parametrize("closes_first", [True, False], ids=["closes", "refuses"])
def test_an_oserror_at_the_close_of_the_staging_folder_alone_comes_out_after_every_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closes_first: bool
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    files = _three(folder, stage)
    error = OSError(errno.EIO, "injected error at the close of the staging folder")
    wrapper = _FailingClose({files["stage"]: error}, closes_first=closes_first).watching(
        folder, stage / "writer.lock"
    )
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(OSError) as failed:
                p.close()
        assert failed.value is error
        assert wrapper.calls == [files["lock"], files["stage"], files["folder"]]
        assert (p._lock_fd, p._stage_fd, p._folder_fd) == (None, None, None)
        assert _open_descriptors() - before == set(wrapper.refused)
    finally:
        _close_left(wrapper.refused, stage)
    assert _open_descriptors() == before
    second = publish.Publisher(folder)
    second.open()
    second.close()


def test_an_interrupt_at_the_close_of_the_staging_folder_goes_out_and_frees_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    work = tmp_path / "work"
    work.mkdir()
    before = _open_descriptors()
    p = publish.Publisher(folder)
    p.open()
    monkeypatch.chdir(work)
    interrupt = KeyboardInterrupt()
    wrapper = _raises_without_closing({_identity_of(stage): interrupt})
    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", wrapper)
            with pytest.raises(KeyboardInterrupt) as failed:
                p.close()
        assert failed.value is interrupt
        second = publish.Publisher(folder)
        second.open()  # the lock is free: it was closed first
        second.close()
        with pytest.raises(publish.PublishError) as refused:
            p.publish(_doc(1))
        assert str(refused.value) == "publication failed: the folders are not open"
        assert os.listdir(work) == []
        assert "fleet.json" not in os.listdir(folder)
        # Until the further close(): the refused staging descriptor and the published folder,
        # which the interrupted close() had not reached.
        extra = _open_descriptors() - before
        assert len(extra) == 2 and set(wrapper.refused) <= extra
        (published,) = extra - set(wrapper.refused)
        assert _identity(published) == _identity_of(folder)
        p.close()  # closes the published folder, not reached before
        assert _open_descriptors() == before | set(wrapper.refused)
        assert [_identity(fd) for fd in wrapper.refused] == [_identity_of(stage)]
    finally:
        with contextlib.suppress(OSError):
            p.close()
        _close_left(wrapper.refused, stage)


def test_a_failed_close_of_the_published_folder_frees_the_lock_and_refuses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    p = publish.Publisher(folder)
    p.open()
    refused: list[int] = []
    try:
        monkeypatch.setattr(publish.os, "close", _fail_once_for(folder, refused))
        with pytest.raises(OSError, match="injected error at the close of the published"):
            p.close()
        second = publish.Publisher(folder)
        second.open()  # the lock is free
        second.close()
        with pytest.raises(publish.PublishError) as failed:
            p.publish(_doc(1))
        assert str(failed.value) == "publication failed: the folders are not open"
        assert os.listdir(work) == []
        assert "fleet.json" not in os.listdir(folder)
    finally:
        monkeypatch.undo()
        _close_left(refused, folder)
