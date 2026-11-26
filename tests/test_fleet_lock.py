"""fleet: the writer lock as the kernel holds it, not only as the publisher's field says.

Another open of the lock file asks the kernel (``lock_state``). An open publisher holds the lock
exclusively after every call that can end on it, so a second publisher of the folder is refused;
after ``close()`` the lock is free. A publication writes and renames under the lock.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
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


def _fields(p: publish.Publisher) -> tuple[int | None, int | None, int | None]:
    return p._lock_fd, p._stage_fd, p._folder_fd


def _end_a_call(way: str, p: publish.Publisher, monkeypatch: pytest.MonkeyPatch) -> None:
    """One call on an open publisher, ended in the given way."""
    real_write = publish.os.write
    if way == "publication":
        p.publish(_doc(1))
    elif way == "refused document":
        with pytest.raises(publish.PublishError, match="^document refused: "):
            p.publish(b"not a document")
    elif way == "failed write":

        def failing(fd: int, data: Any) -> int:
            raise OSError(errno.EIO, "injected error at the first write")

        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", failing)
            with pytest.raises(publish.PublishError, match="injected error at the first write"):
                p.publish(_doc(1))
    elif way == "interrupted write":

        def interrupted(fd: int, data: Any) -> int:
            real_write(fd, data)  # the bytes are written, then the interrupt
            raise KeyboardInterrupt

        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", interrupted)
            with pytest.raises(KeyboardInterrupt):
                p.publish(_doc(1))
    else:
        with pytest.raises(publish.PublishRefused, match="still holds"):
            p.open()


@pytest.mark.parametrize(
    "way",
    ["publication", "refused document", "failed write", "interrupted write", "second open"],
)
def test_an_open_publisher_holds_the_lock_after_every_way_a_call_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    p = publish.Publisher(folder)
    p.open()
    second = publish.Publisher(folder)
    try:
        fields = _fields(p)
        _end_a_call(way, p, monkeypatch)
        assert lock_state(stage) == "exclusive"
        assert _fields(p) == fields
        before = _open_descriptors()
        with pytest.raises(publish.LockHeld) as held:
            second.open()
        assert str(held.value) == f"another fleet writer already publishes to {folder}"
        assert _fields(second) == (None, None, None)
        assert _open_descriptors() == before  # nothing of the refused start is left open
        if way != "interrupted write":  # that one leaves a staged file; a later start removes it
            p.publish(_doc(2))
            assert (folder / "fleet.json").read_bytes() == _doc(2)
    finally:
        p.close()
    assert lock_state(stage) == "free"
    second.open()
    try:
        second.publish(_doc(3))
    finally:
        second.close()
    assert (folder / "fleet.json").read_bytes() == _doc(3)


# The calls of the publisher through its ``os`` name that are asked about.
_ASKED = ("open", "fchmod", "write", "fsync", "close", "replace", "unlink", "fstat", "listdir")


def test_every_call_of_the_publisher_while_it_holds_the_lock_is_made_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the moment ``open()`` takes the lock up to the close that gives it back.

    Each call the publisher makes through ``os`` in that time asks the kernel first how the
    lock looks from another open of the lock file. The calls of that question itself are
    not asked about.
    """
    stage = tmp_path / "fleet.stage"
    real = {name: getattr(os, name) for name in _ASKED}
    real_flock = fcntl.flock
    state = {"holding": False, "asking": False, "fail_write": False}
    seen: list[tuple[str, str]] = []
    lock_file: list[tuple[int, int]] = []

    def ask(name: str) -> None:
        state["asking"] = True
        try:
            seen.append((name, lock_state(stage)))
        finally:
            state["asking"] = False

    def wrap(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if not state["holding"] or state["asking"]:
                return real[name](*args, **kwargs)
            ask(name)
            if name == "write":
                if state["fail_write"]:
                    raise OSError(errno.EIO, "injected error at a write")
                fd, data = args
                return real["write"](fd, data[: max(1, len(data) // 3)])  # a third at most
            if name == "close":
                info = real["fstat"](args[0])
                if (info.st_dev, info.st_ino) in lock_file:
                    state["holding"] = False  # the lock's descriptor is given back
            return real[name](*args, **kwargs)

        return call

    def flock(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        if not state["asking"] and operation & fcntl.LOCK_EX:
            info = real["fstat"](fd)  # the real one: the set of names holds the publisher's calls
            lock_file[:] = [(info.st_dev, info.st_ino)]
            state["holding"] = True

    writes: list[int] = []
    p = publish.Publisher(tmp_path / "fleet")
    with monkeypatch.context() as m:
        for name in _ASKED:
            m.setattr(publish.os, name, wrap(name))
        m.setattr(publish.fcntl, "flock", flock)
        try:
            p.open()
            assert state["holding"]
            p.publish(_doc(1, pad=4000))
            writes.append(sum(1 for name, _ in seen if name == "write"))
            state["fail_write"] = True
            with pytest.raises(publish.PublishError, match="injected error at a write"):
                p.publish(_doc(2))
            state["fail_write"] = False
        finally:
            p.close()
    assert not state["holding"], "the close that gives the lock back was not seen"
    assert writes[0] >= 3
    assert {name for name, _ in seen} == {
        "fchmod",
        "unlink",
        "open",
        "write",
        "fsync",
        "close",
        "replace",
    }
    assert [name for name, _ in seen].count("replace") == 1
    assert seen[-1][0] == "close"
    assert {answer for _, answer in seen} == {"exclusive"}, seen


def test_the_publisher_takes_the_lock_once_at_its_start_and_never_locks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every call through ``fcntl`` from the start of ``open()`` to the return of ``close()``.

    Exactly one: ``flock`` with ``LOCK_EX | LOCK_NB`` in ``open()``. The close of the lock's
    descriptor gives the lock back; the publisher never unlocks, and never locks again, with
    ``flock``. The other ways to a file lock fail the test when the publisher calls them.
    """
    real_flock = fcntl.flock
    real_write = os.write
    phase = {"now": "before"}
    record: list[tuple[str, str, int]] = []

    def flock(fd: int, operation: int) -> None:
        record.append((phase["now"], "flock", operation))
        real_flock(fd, operation)

    def forbidden(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            record.append((phase["now"], name, -1))
            raise AssertionError(f"the publisher called fcntl.{name} in {phase['now']}")

        return call

    def failing_write(fd: int, data: Any) -> int:
        raise OSError(errno.EIO, "injected error at a write")

    p = publish.Publisher(tmp_path / "fleet")
    with monkeypatch.context() as m:
        m.setattr(publish.fcntl, "flock", flock)
        m.setattr(publish.fcntl, "lockf", forbidden("lockf"))
        m.setattr(publish.fcntl, "fcntl", forbidden("fcntl"))
        try:
            phase["now"] = "open"
            p.open()
            phase["now"] = "publication"
            p.publish(_doc(1))
            phase["now"] = "refused document"
            with pytest.raises(publish.PublishError, match="^document refused: "):
                p.publish(b"not a document")
            phase["now"] = "failed write"
            m.setattr(publish.os, "write", failing_write)
            with pytest.raises(publish.PublishError, match="injected error at a write"):
                p.publish(_doc(2))
            m.setattr(publish.os, "write", real_write)
            phase["now"] = "second publication"
            p.publish(_doc(3))
        finally:
            phase["now"] = "close"
            p.close()
            phase["now"] = "after"
    assert record == [("open", "flock", fcntl.LOCK_EX | fcntl.LOCK_NB)], (
        f"the calls through fcntl were: {record}"
    )
    assert (tmp_path / "fleet" / "fleet.json").read_bytes() == _doc(3)


@pytest.mark.parametrize("first", ["A", "B"], ids=["A-closed-first", "B-closed-first"])
def test_two_publishers_of_two_folders_hold_their_own_lock_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    folders = {"A": tmp_path / "a" / "fleet", "B": tmp_path / "b" / "fleet"}
    stages = {name: folder.with_name("fleet.stage") for name, folder in folders.items()}
    pubs = {name: publish.Publisher(folder) for name, folder in folders.items()}
    last: dict[str, bytes] = {}
    try:
        pubs["A"].open()
        pubs["B"].open()
        fields = {name: _fields(p) for name, p in pubs.items()}

        def each_as_before() -> None:
            for name, p in pubs.items():
                assert lock_state(stages[name]) == "exclusive", name
                assert _fields(p) == fields[name], name
                if name in last:
                    assert (folders[name] / "fleet.json").read_bytes() == last[name], name

        each_as_before()
        for n, name in enumerate(["A", "B", "A", "B"], start=1):
            data = _doc(n * 10 + (1 if name == "A" else 2))
            pubs[name].publish(data)
            last[name] = data
            each_as_before()
        with pytest.raises(publish.PublishError, match="^document refused: "):
            pubs["A"].publish(b"not a document")
        each_as_before()

        def failing(fd: int, data: Any) -> int:
            raise OSError(errno.EIO, "injected error at a write")

        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", failing)
            with pytest.raises(publish.PublishError, match="injected error at a write"):
                pubs["B"].publish(_doc(99))
        each_as_before()
        third = publish.Publisher(folders["A"])
        with pytest.raises(publish.LockHeld):
            third.open()
        assert _fields(third) == (None, None, None)
        each_as_before()
        other = "B" if first == "A" else "A"
        pubs[first].close()
        assert lock_state(stages[first]) == "free"
        assert lock_state(stages[other]) == "exclusive"
        assert _fields(pubs[other]) == fields[other]
        data = _doc(77)
        pubs[other].publish(data)
        assert (folders[other] / "fleet.json").read_bytes() == data
        assert (folders[first] / "fleet.json").read_bytes() == last[first]
        pubs[other].close()
        assert lock_state(stages["A"]) == lock_state(stages["B"]) == "free"
    finally:
        for p in pubs.values():
            p.close()
