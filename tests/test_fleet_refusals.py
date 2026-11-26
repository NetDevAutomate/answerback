"""fleet: the exact class and words of each refusal and failure of the publisher."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_lifetime import no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import document, publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def test_a_folder_under_a_parent_that_is_a_link_loop_is_refused_as_a_link(
    tmp_path: Path,
) -> None:
    (tmp_path / "loop").symlink_to(tmp_path / "loop")  # loop -> loop: the open gives ELOOP
    folder = tmp_path / "loop" / "fleet"
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(folder).open()
    assert str(refused.value) == f"{folder} is a symbolic link or not a folder"


def test_a_document_with_more_than_five_problems_is_refused_with_five(tmp_path: Path) -> None:
    problems = document.validate(b"{}")
    assert len(problems) > 5
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        with pytest.raises(publish.PublishError) as refused:
            p.publish(b"{}")
    finally:
        p.close()
    assert str(refused.value) == "document refused: " + ", ".join(problems[:5])


def test_a_folder_that_is_gone_between_its_making_and_its_opening_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.mkdir
    stage = tmp_path / "fleet.stage"

    def made_then_gone(path: Any, *args: Any, **kwargs: Any) -> None:
        real(path, *args, **kwargs)
        if Path(path) == stage:
            os.rmdir(path)  # another process removes it at once

    monkeypatch.setattr(publish.os, "mkdir", made_then_gone)
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(tmp_path / "fleet").open()
    assert str(refused.value) == f"{stage} vanished while it was made"


def test_an_error_without_words_of_its_own_at_the_open_of_a_folder_keeps_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    real = os.open

    def refusing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == folder:
            raise OSError("a policy refused the open")  # no errno, no strerror
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(publish.os, "open", refusing_open)
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(folder).open()
    assert str(refused.value) == f"{folder} cannot be opened: a policy refused the open"


def test_an_error_without_words_of_its_own_at_the_rename_keeps_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        raise OSError("the disk said no")

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        monkeypatch.setattr(publish.os, "replace", refusing_rename)
        with pytest.raises(publish.PublishError) as failed:
            p.publish(_doc(1))
    finally:
        p.close()
    assert str(failed.value) == "publication failed: the disk said no"


@pytest.mark.skipif(os.getuid() == 0, reason="an administrator may write to every folder")
def test_a_lock_that_cannot_be_made_for_lack_of_permission_is_no_symbolic_link(
    tmp_path: Path,
) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
    (tmp_path / "fleet.stage").chmod(0o500)  # the owner may not write into it
    try:
        with pytest.raises(PermissionError) as refused:
            publish.Publisher(tmp_path / "fleet").open()
    finally:
        (tmp_path / "fleet.stage").chmod(0o700)
    assert type(refused.value) is PermissionError
    assert "symbolic link" not in str(refused.value)


def test_two_wrong_allowed_entries_are_refused_by_the_first_name_in_sorted_order(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "fleet.stage"
    stage.mkdir()
    (tmp_path / "target").write_text("keep", encoding="utf-8")
    for name in ("writer.lock", "fleet.json.new"):
        (stage / name).symlink_to(tmp_path / "target")
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(tmp_path / "fleet").open()
    assert str(refused.value) == (
        f"{stage / 'fleet.json.new'} must be a regular file of the current user, not a link"
    )
