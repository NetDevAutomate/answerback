"""fleet: what each method of a publisher does in each state its three descriptors can be in.

The states, by what the publisher holds: nothing; the lock and both folders; both folders and
no lock (an interrupt ended ``close()`` at the close of the lock); the published folder only (an
interrupt ended ``close()`` at the close of the staging folder). A publisher that holds anything
refuses a second ``open()`` and keeps what it holds; ``publish()`` needs the lock as well as the
folders; ``close()`` releases what is held.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_fleet_closes import _closes_then_raises, _failing_folder_mode, _identity_of, _two_folders
from test_fleet_lifetime import (
    _open_descriptors,
    _open_files,
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


# How each state is reached, and the state it gives.
WAYS = {
    "new": "nothing",
    "closed": "nothing",
    "failed start": "nothing",
    "failed start, interrupt in its cleanup": "nothing",
    "interrupt at the close of the published folder": "nothing",
    "open": "all three",
    "interrupt at the close of the lock": "both folders",
    "interrupt at the close of the staging folder": "published folder",
}
HELD = {
    "nothing": (False, False, False),
    "all three": (True, True, True),
    "both folders": (False, True, True),
    "published folder": (False, False, True),
}


def _fields(p: publish.Publisher) -> tuple[int | None, int | None, int | None]:
    return p._lock_fd, p._stage_fd, p._folder_fd


def _reach(
    way: str, folder: Path, stage: Path, monkeypatch: pytest.MonkeyPatch
) -> publish.Publisher:
    p = publish.Publisher(folder)
    if way == "new":
        return p
    if way.startswith("failed start"):
        failures = {}
        if "interrupt" in way:
            failures = {_identity_of(stage / "writer.lock"): KeyboardInterrupt()}
        with monkeypatch.context() as m:
            m.setattr(publish.os, "fchmod", _failing_folder_mode([]))
            m.setattr(publish.os, "close", _closes_then_raises(failures))
            with pytest.raises(KeyboardInterrupt if failures else OSError):
                p.open()
        return p
    p.open()
    if way == "open":
        return p
    if way == "closed":
        p.close()
        return p
    target = {
        "interrupt at the close of the lock": stage / "writer.lock",
        "interrupt at the close of the staging folder": stage,
        "interrupt at the close of the published folder": folder,
    }[way]
    wrapper = _closes_then_raises({_identity_of(target): KeyboardInterrupt()})
    with monkeypatch.context() as m:
        m.setattr(publish.os, "close", wrapper)
        with pytest.raises(KeyboardInterrupt):
            p.close()
    assert not wrapper.failures
    return p


def _kernel_agrees(p: publish.Publisher, stage: Path) -> None:
    """The lock as another open of the lock file sees it matches the publisher's field."""
    assert lock_state(stage) == ("free" if p._lock_fd is None else "exclusive")


def _another_publisher_works(folder: Path, n: int) -> bytes:
    other = publish.Publisher(folder)
    other.open()  # the lock is free
    try:
        other.publish(_doc(n))
    finally:
        other.close()
    return _doc(n)


@pytest.mark.parametrize("method", ["open", "publish", "close"])
@pytest.mark.parametrize("way", list(WAYS))
def test_each_method_in_each_state_of_a_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str, method: str
) -> None:
    folder, stage = _two_folders(tmp_path)
    baseline = _open_descriptors()
    p = _reach(way, folder, stage, monkeypatch)
    state = WAYS[way]
    try:
        assert tuple(fd is not None for fd in _fields(p)) == HELD[state]
        partial = state in ("both folders", "published folder")
        expected: bytes | None = None
        if partial:
            # The lock is free: another publisher starts, publishes and closes.
            expected = _another_publisher_works(folder, 7)
        _kernel_agrees(p, stage)
        fields, files = _fields(p), _open_files()
        if method == "open":
            if state == "nothing":
                p.open()
                p.publish(_doc(1))  # a closed or never opened publisher starts again
                assert (folder / "fleet.json").read_bytes() == _doc(1)
            else:
                with pytest.raises(publish.PublishRefused) as refused:
                    p.open()
                assert str(refused.value) == (
                    f"this publisher still holds {folder}: close it first"
                )
                assert _fields(p) == fields and _open_files() == files
                if state == "all three":
                    p.publish(_doc(2))  # still holds the lock of its first start
                    assert (folder / "fleet.json").read_bytes() == _doc(2)
        elif method == "publish":
            if state == "all three":
                p.publish(_doc(3))
                assert (folder / "fleet.json").read_bytes() == _doc(3)
            else:
                words = (
                    "publication failed: the writer lock is not held"
                    if state == "both folders"
                    else "publication failed: the folders are not open"
                )
                with pytest.raises(publish.PublishError) as refused:
                    p.publish(_doc(4))
                assert str(refused.value) == words
                assert _fields(p) == fields and _open_files() == files
        else:
            p.close()
            assert _fields(p) == (None, None, None)
            assert _open_descriptors() == baseline
        _kernel_agrees(p, stage)
        if partial:
            # The first publisher still writes nothing.
            with pytest.raises(publish.PublishError):
                p.publish(_doc(5))
            assert (folder / "fleet.json").read_bytes() == expected
            assert os.listdir(stage) == ["writer.lock"]
    finally:
        p.close()
    _kernel_agrees(p, stage)
    assert _fields(p) == (None, None, None)
    assert _open_descriptors() == baseline
    if state == "all three":
        _another_publisher_works(folder, 8)  # the lock of the first start was released
