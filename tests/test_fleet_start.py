"""fleet: a refused start sets no mode of a folder that existed and keeps what was there."""

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import no_descriptor_left, process_unchanged

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _folders(tmp_path: Path) -> tuple[Path, Path]:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    for path in (folder, stage):
        path.mkdir()
        path.chmod(0o755)
    return folder, stage


def _fail_fchmod_of_files(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(5, "injected I/O error")
        real(fd, mode)

    monkeypatch.setattr(publish.os, "fchmod", fchmod)


def test_a_start_refused_because_the_lock_is_held_keeps_the_folders_modes(tmp_path: Path) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    holder = publish.Publisher(folder)
    holder.open()
    try:
        folder.chmod(0o755)
        stage.chmod(0o755)
        with deadline("the start beside a writer that holds the lock"):
            with pytest.raises(publish.LockHeld):
                publish.Publisher(folder).open()
        assert (_mode(folder), _mode(stage)) == (0o755, 0o755)
    finally:
        holder.close()


def test_a_start_refused_for_a_hard_link_under_the_lock_keeps_the_folders_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _folders(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    real = publish._inspect

    def inspect_then_link(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if path.name == "fleet.stage":
            os.link(outside, path / "writer.lock")

    monkeypatch.setattr(publish, "_inspect", inspect_then_link)
    with pytest.raises(publish.PublishRefused, match="second name"):
        publish.Publisher(folder).open()
    assert (_mode(folder), _mode(stage)) == (0o755, 0o755)


def test_a_start_refused_because_the_lock_mode_cannot_be_set_keeps_the_folders_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _folders(tmp_path)
    _fail_fchmod_of_files(monkeypatch)
    with pytest.raises(OSError, match="injected"):
        publish.Publisher(folder).open()
    assert (_mode(folder), _mode(stage)) == (0o755, 0o755)


def test_a_start_refused_at_the_lock_keeps_the_staged_file_an_earlier_writer_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = _folders(tmp_path)
    (stage / "fleet.json.new").write_bytes(b"left over")
    _fail_fchmod_of_files(monkeypatch)
    with pytest.raises(OSError, match="injected"):
        publish.Publisher(folder).open()
    assert (stage / "fleet.json.new").read_bytes() == b"left over"


@pytest.mark.parametrize("there_before", [True, False])
def test_a_start_that_succeeds_ends_with_both_folders_at_0700(
    tmp_path: Path, there_before: bool
) -> None:
    if there_before:
        _folders(tmp_path)
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    p = publish.Publisher(folder)
    p.open()
    p.close()
    assert (_mode(folder), _mode(stage)) == (0o700, 0o700)


# ------------------------------------------------ v6: a start that fails removes nothing


def test_a_failed_start_leaves_the_folders_and_lock_it_made_and_the_next_start_uses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    with monkeypatch.context() as m:
        _fail_fchmod_of_files(m)
        with pytest.raises(OSError, match="injected"):
            publish.Publisher(folder).open()
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]
    assert os.listdir(folder) == [] and os.listdir(stage) == ["writer.lock"]
    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_document())
    finally:
        p.close()
    assert (_mode(folder), _mode(stage)) == (0o700, 0o700)
    assert _mode(stage / "writer.lock") == 0o600
    assert _mode(folder / "fleet.json") == 0o600


def test_a_failed_mkdir_of_the_staging_folder_leaves_the_published_folder_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    real = os.mkdir

    def mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == "fleet.stage":
            raise OSError(28, "injected: no space left")
        real(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(publish.os, "mkdir", mkdir)
        with pytest.raises(OSError, match="injected"):
            publish.Publisher(folder).open()
    assert os.listdir(tmp_path) == ["fleet"] and os.listdir(folder) == []
    p = publish.Publisher(folder)
    p.open()
    p.close()
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]


def test_a_start_refused_for_two_file_systems_leaves_the_folders_it_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    devices = iter(range(100))
    monkeypatch.setattr(publish, "_device", lambda fd: next(devices))
    with pytest.raises(publish.PublishRefused, match="one file system"):
        publish.Publisher(tmp_path / "fleet").open()
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]


def _document() -> bytes:
    import json

    from test_fleet_document import _inputs

    from answerback_bridge.fleet import document

    return json.dumps(document.build(_inputs(), written_at=1000.0, now_mono=1)).encode()


def test_a_folder_under_the_staged_name_fails_every_publication_until_it_is_removed(
    tmp_path: Path,
) -> None:
    import json

    folder, stage = tmp_path / "fleet", tmp_path / "fleet.stage"
    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_document())
        good = (folder / "fleet.json").read_bytes()
        (stage / "fleet.json.new").mkdir()
        for _ in range(2):
            with pytest.raises(publish.PublishError):
                p.publish(_document().replace(b"1000.0", b"2000.0"))
            assert (folder / "fleet.json").read_bytes() == good
        assert (stage / "fleet.json.new").is_dir()  # the writer does not remove a folder
        (stage / "fleet.json.new").rmdir()
        p.publish(_document().replace(b"1000.0", b"3000.0"))
    finally:
        p.close()
    assert json.loads((folder / "fleet.json").read_bytes())["written_at"] == 3000.0
