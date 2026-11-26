"""fleet: the publisher steps that the sweep of single edits found without a test."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_fleet_lifetime import no_descriptor_left, process_unchanged

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_file_system_of_a_folder_is_the_device_of_its_descriptor(tmp_path: Path) -> None:
    # Two file systems for the two sibling folders need a mount point, which no test may
    # make; so the helper that compares them is checked on its own descriptor.
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert publish._device(fd) == os.fstat(fd).st_dev
    finally:
        os.close(fd)


def test_close_of_a_publisher_that_was_never_opened_does_nothing(tmp_path: Path) -> None:
    p = publish.Publisher(tmp_path / "fleet")
    p.close()
    assert os.listdir(tmp_path) == []


def test_the_content_of_a_folder_is_checked_in_the_opened_folder_not_through_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    (folder / "notes.txt").write_text("not the writer's", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    real = publish._open_folder

    def open_then_swap(path: Path) -> int | None:
        fd = real(path)
        if fd is not None and path == folder:
            # Another process replaces the opened folder's path by a link to an empty one.
            path.rename(tmp_path / "held")
            path.symlink_to(empty)
        return fd

    monkeypatch.setattr(publish, "_open_folder", open_then_swap)
    with pytest.raises(publish.PublishRefused, match="notes.txt"):
        publish.Publisher(folder).open()
    assert os.listdir(empty) == []


def test_a_made_folder_gets_its_mode_through_its_descriptor_not_through_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    real = publish._inspect
    held: list[Path] = []

    def inspect_then_swap(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if not allowed and path.name == "fleet.stage":  # the folder this start just made
            path.rename(tmp_path / "held.stage")
            path.symlink_to(outside)
            held.append(tmp_path / "held.stage")

    monkeypatch.setattr(publish, "_inspect", inspect_then_swap)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    p.close()
    assert _mode(outside) == 0o755
    assert os.listdir(outside) == []
    assert _mode(held[0]) == 0o700
