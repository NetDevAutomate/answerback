"""fleet: a publication writes the bytes it was given, all of them, unchanged, and only them.

The documents here are made by the writer's own encoder (``document.encode``: UTF-8, not
escaped to ASCII, compact), unlike the ASCII-only helper of the other tests. Also here: the
staged file gets its mode before a byte is written, and no descriptor of the publisher crosses
into a child process.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_document import WALL, _inputs, _receipt
from test_fleet_lifetime import Child, _open_descriptors, no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import document, publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


# A composed and a decomposed letter, letters of two, three and four bytes in UTF-8, and a
# carriage return and a line feed inside a value.
TEXT = "caf\u00e9 cafe\u0301 \u00df \u4e00 \U0001f600 a\r\nb"
SEQUENCES = {
    "composed": b"caf\xc3\xa9",
    "decomposed": b"cafe\xcc\x81",
    "two bytes": b"\xc3\x9f",
    "three bytes": b"\xe4\xb8\x80",
    "four bytes": b"\xf0\x9f\x98\x80",
    "escaped line end": b"a\\r\\nb",
}


def _document_1() -> bytes:
    doc = document.build(_inputs(), written_at=WALL, now_mono=1)
    doc["errors"] = [{"at": WALL, "where": "receipts", "text": TEXT}]
    data = document.encode(doc)
    for name, sequence in SEQUENCES.items():
        assert sequence in data, f"the document lost its point: no {name}"
    assert b"\r" not in data and b"\n" not in data  # the line end is escaped, in four bytes
    assert document.validate(data) == []
    return data


def _document_3() -> bytes:
    needs = [_receipt(n, ("\u00e9\u4e00" + TEXT) * 4) for n in range(200)]
    for r in needs:
        r["title"] = r["title"][:120]
        r["recipient"]["text"] = "\u00df\u4e00" * 40
        r["request_key"] = "\u00e9\u4e00" * 60
    view = {
        "source": "status+task_list",
        "read_at": WALL,
        "stale": False,
        "error": None,
        "counts": {"uncertain": 200, "blocked": 0, "total": 200},
        "needs_person": needs,
        "needs_person_complete": True,
        "needs_person_omitted": 0,
        "recent": needs[:8],
    }
    data = document.encode(document.build(_inputs(receipts=view), written_at=WALL, now_mono=1))
    assert 140_000 < len(data) < document.LIMIT
    assert json.loads(data)["receipts"]["needs_person_complete"] is True  # nothing dropped
    assert data.count(SEQUENCES["four bytes"]) > 100
    assert document.validate(data) == []
    return data


def _published(tmp_path: Path) -> bytes:
    return (tmp_path / "fleet" / "fleet.json").read_bytes()


def _stage(tmp_path: Path) -> list[str]:
    return os.listdir(tmp_path / "fleet.stage")


@pytest.mark.parametrize("which", ["text outside ASCII", "white space around", "large"])
def test_a_publication_writes_exactly_the_bytes_of_the_document(tmp_path: Path, which: str) -> None:
    if which == "large":
        data = _document_3()
    else:
        data = _document_1()
        if which == "white space around":
            data = b" " + data + b"\n"
            assert document.validate(data) == []
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(data)
    finally:
        p.close()
    assert _published(tmp_path) == data


@pytest.mark.parametrize(("which", "part"), [("text outside ASCII", 3), ("large", 4093)])
def test_writes_that_take_only_part_of_the_bytes_still_give_the_whole_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str, part: int
) -> None:
    data = _document_1() if which != "large" else _document_3()
    real = os.write
    sizes: list[int] = []

    def short_write(fd: int, chunk: Any) -> int:
        written = real(fd, bytes(chunk[:part]))  # cuts a sequence of several bytes too
        sizes.append(written)
        return written

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        monkeypatch.setattr(publish.os, "write", short_write)
        p.publish(data)
    finally:
        p.close()
    assert len(sizes) > 1 and sum(sizes) == len(data)
    assert _published(tmp_path) == data


def test_a_failed_sync_keeps_the_previous_file_and_the_next_publication_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, data = _document_3(), _document_1()
    real = os.fsync
    failing = {"on": False}

    def fsync(fd: int) -> None:
        if failing["on"] and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected error at the sync of the staged file")
        real(fd)

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(first)
        monkeypatch.setattr(publish.os, "fsync", fsync)
        failing["on"] = True
        with pytest.raises(publish.PublishError, match="injected error at the sync"):
            p.publish(data)
        assert _published(tmp_path) == first
        assert _stage(tmp_path) == ["writer.lock"]
        failing["on"] = False
        p.publish(data)
    finally:
        p.close()
    assert _published(tmp_path) == data


class _Unusual(BaseException):
    """An exception of none of the standard classes: it derives from ``BaseException`` alone."""


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt("injected interrupt at a write"),
        SystemExit("injected exit at a write"),
        RuntimeError("injected error at a write"),
        _Unusual("injected exception of its own class at a write"),
    ],
    ids=["interrupt", "exit", "runtime", "own"],
)
def test_an_exception_that_is_no_oserror_at_a_write_goes_out_of_publish_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    folder = tmp_path / "fleet"
    first, second, third = _doc(1, pad=4000), _doc(2, pad=4000), _doc(3)
    real = os.write
    calls: list[int] = []

    def write(fd: int, chunk: Any) -> int:
        calls.append(len(chunk))
        if len(calls) == 2:
            raise error
        return real(fd, bytes(chunk[: len(chunk) // 2]))  # the first part only

    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(first)
        fields = (p._lock_fd, p._stage_fd, p._folder_fd)
        held = _open_descriptors()
        with monkeypatch.context() as m:
            m.setattr(publish.os, "write", write)
            with pytest.raises(BaseException) as failed:
                p.publish(second)
        assert failed.value is error
        assert not isinstance(failed.value, publish.PublishError)
        assert len(calls) == 2
        assert _published(tmp_path) == first
        assert (p._lock_fd, p._stage_fd, p._folder_fd) == fields
        assert _open_descriptors() == held  # the staged file's descriptor was closed
    finally:
        p.close()
    other = publish.Publisher(folder)
    other.open()
    try:
        other.publish(third)
    finally:
        other.close()
    assert _published(tmp_path) == third


def test_the_staged_file_gets_its_mode_before_a_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fchmod, real_write = os.fchmod, os.write
    sizes: list[int] = []
    writes: list[int] = []

    def fchmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            sizes.append(info.st_size)
            raise OSError(errno.EIO, "injected error at the mode of the staged file")
        real_fchmod(fd, mode)

    def write(fd: int, chunk: Any) -> int:
        writes.append(fd)
        return real_write(fd, chunk)

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        p.publish(_doc(1))
        with monkeypatch.context() as m:
            m.setattr(publish.os, "fchmod", fchmod)
            m.setattr(publish.os, "write", write)
            with pytest.raises(publish.PublishError) as failed:
                p.publish(_doc(2))
        assert str(failed.value) == (
            "publication failed: injected error at the mode of the staged file"
        )
        assert sizes == [0]
        assert writes == []
        assert _published(tmp_path) == _doc(1)
        assert _stage(tmp_path) == ["writer.lock"]
        p.publish(_doc(3))
    finally:
        p.close()
    assert _published(tmp_path) == _doc(3)


def test_no_descriptor_of_the_publisher_is_inheritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write
    staged: list[bool] = []

    def write(fd: int, chunk: Any) -> int:
        staged.append(os.get_inheritable(fd))
        return real(fd, chunk)

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        held = [p._lock_fd, p._stage_fd, p._folder_fd]
        assert [os.get_inheritable(fd) for fd in held if fd is not None] == [False] * 3
        monkeypatch.setattr(publish.os, "write", write)
        p.publish(_doc(1, pad=4000))
    finally:
        p.close()
    assert staged and not any(staged)


CHILD = "import json, os, sys\n" + (
    "found = []\n"
    "for number in map(int, sys.argv[1:]):\n"
    "    try:\n"
    "        os.fstat(number)\n"
    "        found.append(number)\n"
    "    except OSError:\n"
    "        pass\n"
    "print(json.dumps(found))\n"
)


def test_a_child_started_while_the_publisher_is_open_gets_none_of_its_descriptors(
    tmp_path: Path,
) -> None:
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        held = [fd for fd in (p._lock_fd, p._stage_fd, p._folder_fd) if fd is not None]
        assert len(held) == 3
        args = [sys.executable, "-I", "-S", "-c", CHILD, *map(str, held)]
        with Child(args, tmp_path, close_fds=False) as child:
            code = child.wait(30)
    finally:
        p.close()
    assert code == 0, child.stderr
    assert json.loads(child.stdout) == []


def test_a_publication_of_the_same_bytes_again_makes_a_new_file_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = tmp_path / "fleet" / "fleet.json"
    data = _document_1()
    real = os.replace
    renames: list[int] = []

    def replace(*args: Any, **kwargs: Any) -> None:
        renames.append(1)
        real(*args, **kwargs)

    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        monkeypatch.setattr(publish.os, "replace", replace)
        p.publish(data)
        first = os.stat(published).st_ino
        p.publish(data)
        assert len(renames) == 2
        # The first file still existed when the second was made: their numbers differ.
        assert os.stat(published).st_ino != first
        with open(published, "r+b") as outside:
            outside.truncate()
            outside.write(b"other bytes")
        p.publish(data)
        assert len(renames) == 3
        assert published.read_bytes() == data
        published.unlink()
        p.publish(data)
        assert len(renames) == 4
        assert published.read_bytes() == data
    finally:
        p.close()
