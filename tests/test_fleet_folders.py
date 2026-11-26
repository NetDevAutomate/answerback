"""fleet: the folders are held by descriptor; links and swapped paths change nothing else."""

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import no_descriptor_left, process_unchanged

from answerback_bridge.fleet import publish

DOC = json.dumps(
    {
        "schema": "answerback.fleet/1",
        "written_at": 1000.0,
        "machines": [{"name": None, "self": True, "role": "coordinator"}],
    }
).encode()


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


@pytest.fixture(autouse=True)
def _no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests are about folders, not about the check of the document.
    monkeypatch.setattr(publish.document, "validate", lambda data: [])


def _bystander(path: Path, *, mode: int = 0o644, text: str = "keep") -> Path:
    """A file that the writer must never touch."""
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _same(path: Path, mode: int, text: str = "keep") -> None:
    assert path.read_text(encoding="utf-8") == text
    assert stat.S_IMODE(path.lstat().st_mode) == mode


def _other_folder(tmp_path: Path) -> Path:
    other = tmp_path / "other"
    other.mkdir()
    other.chmod(0o755)
    _bystander(other / "keep.txt")
    return other


def _untouched(other: Path) -> None:
    assert sorted(os.listdir(other)) == ["keep.txt"]
    assert stat.S_IMODE(other.stat().st_mode) == 0o755
    _same(other / "keep.txt", 0o644)


@pytest.mark.parametrize(
    ("folder", "name"),
    [("fleet.stage", "writer.lock"), ("fleet.stage", "fleet.json.new"), ("fleet", "fleet.json")],
)
def test_an_allowed_name_that_is_a_symbolic_link_is_refused_and_its_target_kept(
    tmp_path: Path, folder: str, name: str
) -> None:
    target = _bystander(tmp_path / "target.txt")
    (tmp_path / folder).mkdir()
    (tmp_path / folder).chmod(0o755)
    (tmp_path / folder / name).symlink_to(target)
    with pytest.raises(publish.PublishRefused, match=name):
        publish.Publisher(tmp_path / "fleet").open()
    _same(target, 0o644)
    assert stat.S_IMODE((tmp_path / folder).stat().st_mode) == 0o755


def test_an_allowed_name_that_is_a_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "fleet.json").mkdir()
    with pytest.raises(publish.PublishRefused, match="fleet.json"):
        publish.Publisher(tmp_path / "fleet").open()


@pytest.mark.parametrize("which", ["fleet", "fleet.stage"])
def test_a_folder_given_as_a_symbolic_link_is_refused_and_its_target_kept(
    tmp_path: Path, which: str
) -> None:
    other = _other_folder(tmp_path)
    (tmp_path / which).symlink_to(other)
    with pytest.raises(publish.PublishRefused, match="link"):
        publish.Publisher(tmp_path / "fleet").open()
    _untouched(other)


@pytest.mark.parametrize("which", ["fleet", "fleet.stage"])
def test_a_folder_swapped_for_a_link_after_the_check_is_not_changed_or_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    other = _other_folder(tmp_path)
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
        (tmp_path / name).chmod(0o755)
    real = publish._inspect
    seen: list[str] = []

    def inspect_then_swap(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        seen.append(path.name)
        if path.name == which:
            # Another process replaces the checked folder by a link to another one.
            path.rename(tmp_path / f"{which}.moved")
            path.symlink_to(other)

    monkeypatch.setattr(publish, "_inspect", inspect_then_swap)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(DOC)
        p.publish(DOC)
    finally:
        p.close()
    assert which in seen
    _untouched(other)
    moved = tmp_path / f"{which}.moved"
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    if which == "fleet":
        assert os.listdir(moved) == ["fleet.json"]
    else:
        assert os.listdir(moved) == ["writer.lock"]
        assert os.listdir(tmp_path / "fleet") == ["fleet.json"]


def test_a_published_folder_swapped_between_two_publications_is_not_written(
    tmp_path: Path,
) -> None:
    other = _other_folder(tmp_path)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(DOC)
        (tmp_path / "fleet").rename(tmp_path / "fleet.moved")
        (tmp_path / "fleet").symlink_to(other)
        p.publish(DOC)
    finally:
        p.close()
    _untouched(other)
    assert os.listdir(tmp_path / "fleet.moved") == ["fleet.json"]


def test_a_lock_replaced_by_a_link_after_the_check_is_refused_and_its_target_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _bystander(tmp_path / "target.txt")
    real = publish._inspect

    def inspect_then_link(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if path.name == "fleet.stage":
            (path / "writer.lock").symlink_to(target)

    monkeypatch.setattr(publish, "_inspect", inspect_then_link)
    p = publish.Publisher(tmp_path / "fleet")
    with pytest.raises(publish.PublishRefused, match="writer.lock"):
        p.open()
    p.close()
    _same(target, 0o644)


def test_a_refused_start_keeps_folders_that_existed_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fleet").mkdir()
    devices = iter(range(100))
    monkeypatch.setattr(publish, "_device", lambda fd: next(devices))
    with pytest.raises(publish.PublishRefused):
        publish.Publisher(tmp_path / "fleet").open()
    # The folder that existed stays; the staging folder this start made stays too (v6).
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]


def test_the_lock_and_the_published_file_have_mode_0600_and_the_folders_0700(
    tmp_path: Path,
) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
        (tmp_path / name).chmod(0o755)
    (tmp_path / "fleet.stage" / "writer.lock").write_bytes(b"")
    (tmp_path / "fleet.stage" / "writer.lock").chmod(0o644)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(DOC)
    finally:
        p.close()
    for name in ("fleet", "fleet.stage"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "fleet.stage" / "writer.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "fleet" / "fleet.json").stat().st_mode) == 0o600


def test_show_reads_the_document_by_its_path(tmp_path: Path, capsys: Any) -> None:
    from argparse import Namespace

    from answerback_bridge.fleet import cli

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(DOC)
    finally:
        p.close()
    args = Namespace(dir=tmp_path / "fleet", config=tmp_path / "c.json", json=True)
    assert cli.show(args) == 0
    assert json.loads(capsys.readouterr().out)["written_at"] == 1000.0


# ---------------------------------------------------------------- v5: one name only


def _folders(tmp_path: Path, mode: int = 0o755) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()
        (tmp_path / name).chmod(mode)


@pytest.mark.parametrize(
    ("folder", "name"),
    [("fleet", "fleet.json"), ("fleet.stage", "writer.lock"), ("fleet.stage", "fleet.json.new")],
)
def test_a_hard_link_under_an_allowed_name_before_the_start_is_refused(
    tmp_path: Path, folder: str, name: str
) -> None:
    target = _bystander(tmp_path / "target.txt")
    _folders(tmp_path)
    entry = tmp_path / folder / name
    os.link(target, entry)
    with pytest.raises(publish.PublishRefused, match="second name"):
        publish.Publisher(tmp_path / "fleet").open()
    _same(target, 0o644)
    assert entry.exists() and os.stat(entry).st_ino == os.stat(target).st_ino
    for each in ("fleet", "fleet.stage"):
        assert stat.S_IMODE((tmp_path / each).stat().st_mode) == 0o755
    if name != "writer.lock":
        expected = [name] if folder == "fleet.stage" else []
        assert os.listdir(tmp_path / "fleet.stage") == expected  # no lock file was made


def test_a_hard_link_put_under_the_lock_name_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _bystander(tmp_path / "target.txt")
    _folders(tmp_path)
    real = publish._inspect

    def inspect_then_link(fd: int, path: Path, allowed: frozenset[str]) -> None:
        real(fd, path, allowed)
        if path.name == "fleet.stage":
            os.link(target, path / "writer.lock")

    monkeypatch.setattr(publish, "_inspect", inspect_then_link)
    with pytest.raises(publish.PublishRefused, match="second name"):
        publish.Publisher(tmp_path / "fleet").open()
    _same(target, 0o644)


@pytest.mark.parametrize("kind", ["hard", "symbolic"])
def test_a_link_put_under_the_staged_name_after_the_start_fails_one_publication(
    tmp_path: Path, kind: str
) -> None:
    target = _bystander(tmp_path / "target.txt")
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        staged = tmp_path / "fleet.stage" / "fleet.json.new"
        if kind == "hard":
            os.link(target, staged)
        else:
            staged.symlink_to(target)
        with pytest.raises(publish.PublishError):
            p.publish(DOC)
        _same(target, 0o644)
        assert not (tmp_path / "fleet" / "fleet.json").exists()
        newer = DOC.replace(b"1000.0", b"2000.0")
        p.publish(newer)
    finally:
        p.close()
    _same(target, 0o644)
    assert json.loads((tmp_path / "fleet" / "fleet.json").read_bytes())["written_at"] == 2000.0
    assert os.listdir(tmp_path / "fleet.stage") == ["writer.lock"]


# ---------------------------------------------------------------- v5: what a refused start removes


def _fail_fchmod_of_files(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(5, "injected I/O error")
        real(fd, mode)

    monkeypatch.setattr(publish.os, "fchmod", fchmod)


def test_a_refused_start_keeps_folders_and_lock_that_were_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path, 0o700)
    lock = tmp_path / "fleet.stage" / "writer.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    _fail_fchmod_of_files(monkeypatch)
    with pytest.raises(OSError, match="injected"):
        publish.Publisher(tmp_path / "fleet").open()
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]
    assert os.listdir(tmp_path / "fleet.stage") == ["writer.lock"]
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    for name in ("fleet", "fleet.stage"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o700


@pytest.mark.parametrize("stage_there", [False, True])
def test_a_start_that_finds_the_lock_held_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage_there: bool
) -> None:
    # With the staging folder there before, this start made only the published folder,
    # which it could remove although the holder of the lock uses it.
    if stage_there:
        (tmp_path / "fleet.stage").mkdir(mode=0o700)
    real = publish._device
    holder = publish.Publisher(tmp_path / "fleet")
    taken: list[bool] = []

    def device(fd: int) -> int:
        if not taken:
            taken.append(True)
            holder.open()  # takes the lock in the folders the first start just made
        return real(fd)

    monkeypatch.setattr(publish, "_device", device)
    try:
        with deadline("the start beside a writer that holds the lock"):
            with pytest.raises(publish.LockHeld):
                publish.Publisher(tmp_path / "fleet").open()
        assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]
        holder.publish(DOC)
    finally:
        holder.close()
    assert os.listdir(tmp_path / "fleet") == ["fleet.json"]
