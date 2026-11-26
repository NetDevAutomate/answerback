"""fleet: every removal of the staged name goes through the held staging folder."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_lifetime import no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def _outside(tmp_path: Path) -> Path:
    """A folder outside both of the writer's folders, with a file under the staged name."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "fleet.json.new").write_text("keep", encoding="utf-8")
    return outside


def _swap(tmp_path: Path, outside: Path) -> Path:
    """Replace the staging path by a link to ``outside``; return where the held one went."""
    held = tmp_path / "held.stage"
    (tmp_path / "fleet.stage").rename(held)
    (tmp_path / "fleet.stage").symlink_to(outside)
    return held


def test_the_leftover_staged_file_is_removed_in_the_held_folder_not_through_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
    (tmp_path / "fleet.stage" / "fleet.json.new").write_text(
        "left by an earlier writer", encoding="utf-8"
    )
    outside = _outside(tmp_path)
    real = publish.Publisher._take_lock
    moved: list[Path] = []

    def take_lock_then_swap(self: publish.Publisher, fd: int) -> None:
        real(self, fd)
        moved.append(_swap(tmp_path, outside))

    monkeypatch.setattr(publish.Publisher, "_take_lock", take_lock_then_swap)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()  # the start succeeds
    p.close()
    assert (outside / "fleet.json.new").read_text(encoding="utf-8") == "keep"
    assert os.listdir(moved[0]) == ["writer.lock"]


def test_the_staged_file_of_a_failed_publication_is_removed_in_the_held_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside(tmp_path)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    moved: list[Path] = []

    def swap_then_fail(src: Any, dst: Any, **kwargs: Any) -> None:
        moved.append(_swap(tmp_path, outside))
        raise OSError(errno.EIO, "injected I/O error at the rename")

    try:
        monkeypatch.setattr(publish.os, "replace", swap_then_fail)
        with pytest.raises(publish.PublishError, match="injected"):
            p.publish(_doc(1))
    finally:
        p.close()
    assert (outside / "fleet.json.new").read_text(encoding="utf-8") == "keep"
    assert os.listdir(moved[0]) == ["writer.lock"]
