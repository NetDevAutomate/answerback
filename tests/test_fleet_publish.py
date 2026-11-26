"""fleet: publication by rename; the published folder only ever holds fleet.json."""

import itertools
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline

from answerback_bridge.fleet import document, publish


def _doc(n: int, pad: int = 0) -> bytes:
    """A complete document from the builder, written at ``1000 + n``, made longer by ``pad``."""
    from test_fleet_document import _inputs

    doc = document.build(_inputs(), written_at=1000.0 + n, now_mono=1)
    doc["errors"] = [{"at": 1000.0, "where": "pad", "text": "x" * min(pad, 200)}] * min(
        20, max(1, pad // 200)
    )
    return json.dumps(doc).encode()


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from test_fleet_lifetime import (  # that module imports this one
        no_descriptor_left,
        process_unchanged,
    )

    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


@pytest.fixture
def publisher(tmp_path: Path) -> Any:
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    yield p
    p.close()


def test_folders_are_0700_and_the_file_0600_and_staging_is_elsewhere(publisher: Any) -> None:
    publisher.publish(_doc(1))
    assert stat.S_IMODE(publisher.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(publisher.stage.stat().st_mode) == 0o700
    assert stat.S_IMODE((publisher.folder / "fleet.json").stat().st_mode) == 0o600
    assert publisher.stage.parent == publisher.folder.parent
    assert publisher.stage != publisher.folder


def test_publication_never_puts_anything_but_fleet_json_into_the_published_folder(
    publisher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings: list[list[str]] = []
    real = os.replace

    def watched(src: Any, dst: Any, **kwargs: Any) -> None:
        listings.append(sorted(os.listdir(publisher.folder)))
        real(src, dst, **kwargs)
        listings.append(sorted(os.listdir(publisher.folder)))

    monkeypatch.setattr(publish.os, "replace", watched)
    for n in range(5):
        publisher.publish(_doc(n))
    assert listings[0] == [] and all(items == ["fleet.json"] for items in listings[1:])
    assert sorted(os.listdir(publisher.folder)) == ["fleet.json"]


LEAST_READS = 50


def test_a_reader_parsing_all_the_time_during_200_publications_never_gets_a_broken_document(
    publisher: Any,
) -> None:
    publisher.publish(_doc(0))
    stop = threading.Event()
    failures: list[str] = []
    reads = [0]

    def reader() -> None:
        path = publisher.folder / "fleet.json"
        while not stop.is_set():
            # Every error of reading or parsing is recorded; nothing may end this thread.
            try:
                text = path.read_text(encoding="utf-8")
                problems = document.validate(text.encode())
                if problems:
                    failures.append(f"refused: {problems} {text[:40]!r}")
                names = os.listdir(publisher.folder)
                if names != ["fleet.json"]:
                    failures.append(f"folder holds {names}")
            except Exception as error:  # noqa: BLE001 - the test records every kind
                failures.append(f"{type(error).__name__}: {error}")
            reads[0] += 1

    thread = threading.Thread(target=reader)
    thread.start()
    alive_at_end = False
    try:
        for n in range(1, 201):
            publisher.publish(_doc(n, pad=n * 97))
            if n % 20 == 0:
                time.sleep(0.005)  # let the reader in, also on a fast disk
        alive_at_end = thread.is_alive()
    finally:
        stop.set()
        thread.join()
    assert failures == []
    assert alive_at_end, "the reader thread ended before the publications did"
    assert reads[0] >= LEAST_READS, reads[0]
    assert (
        json.loads((publisher.folder / "fleet.json").read_text(encoding="utf-8"))["written_at"]
        == 1200.0
    )


def test_a_publication_whose_rename_raises_leaves_the_previous_file(
    publisher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher.publish(_doc(1))

    def broken(src: Any, dst: Any, **kwargs: Any) -> None:
        raise OSError("disk said no")

    monkeypatch.setattr(publish.os, "replace", broken)
    with pytest.raises(publish.PublishError):
        publisher.publish(_doc(2))
    assert (
        json.loads((publisher.folder / "fleet.json").read_text(encoding="utf-8"))["written_at"]
        == 1001.0
    )
    assert os.listdir(publisher.folder) == ["fleet.json"]
    assert [p.name for p in publisher.stage.iterdir()] == ["writer.lock"]


def test_a_publication_that_fails_the_check_leaves_the_previous_file(publisher: Any) -> None:
    publisher.publish(_doc(1))
    bad = json.loads(_doc(2))
    bad["machines"] = []
    with pytest.raises(publish.PublishError, match="machines"):
        publisher.publish(json.dumps(bad).encode())
    with pytest.raises(publish.PublishError):
        publisher.publish(b"{half")
    assert (
        json.loads((publisher.folder / "fleet.json").read_text(encoding="utf-8"))["written_at"]
        == 1001.0
    )


def test_a_second_writer_for_the_same_folder_is_refused(publisher: Any) -> None:
    second = publish.Publisher(publisher.folder)
    with deadline("the start of a second writer"):
        with pytest.raises(publish.LockHeld):
            second.open()


def test_folders_on_two_file_systems_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = publish.Publisher(tmp_path / "fleet")
    devices = itertools.count()
    monkeypatch.setattr(publish, "_device", lambda where: next(devices))
    with pytest.raises(publish.PublishRefused, match="one file system"):
        p.open()


def test_default_folder_is_fleet_beside_the_runtime_folder(tmp_path: Path) -> None:
    assert (
        publish.default_folder(tmp_path / "cfg" / "config.json")
        == (tmp_path / "cfg" / "fleet").resolve()
    )


def test_a_published_folder_that_holds_another_file_is_refused_and_left_as_it_is(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(publish.PublishRefused, match="notes.txt"):
        publish.Publisher(folder).open()
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
    assert not folder.with_name("fleet.stage").exists()


def test_a_staging_folder_that_holds_another_file_is_refused_and_left_as_it_is(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fleet"
    stage = tmp_path / "fleet.stage"
    stage.mkdir()
    stage.chmod(0o755)
    (stage / "other").write_text("x", encoding="utf-8")
    with pytest.raises(publish.PublishRefused, match="other"):
        publish.Publisher(folder).open()
    assert (stage / "other").read_text(encoding="utf-8") == "x"
    assert stat.S_IMODE(stage.stat().st_mode) == 0o755


def test_folders_with_only_the_writers_own_files_are_accepted(tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    (folder / "fleet.json").write_bytes(_doc(1))
    stage = tmp_path / "fleet.stage"
    stage.mkdir()
    (stage / "writer.lock").write_bytes(b"")
    (stage / "fleet.json.new").write_bytes(b"{")
    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_doc(2))
    finally:
        p.close()
    assert sorted(os.listdir(folder)) == ["fleet.json"]


def _spoiled(change: Any) -> bytes:
    doc = json.loads(_doc(9))
    change(doc)
    return json.dumps(doc).encode()


SPOILS = [
    lambda d: d["machines"][0]["reading"].update(pressure="memory 1.3"),
    lambda d: d["machines"][0]["reading"].update(decided_by=[7]),
    lambda d: d["machines"][0].update(link=["self"]),
    lambda d: d["machines"][0].update(reading=[]),
    lambda d: d.update(writer="x", health=3, capacity=[], receipts={"needs_person": 5}),
    lambda d: d["receipts"].update(needs_person=[None], recent=["x"]),
    lambda d: d["machines"].append(None),
]


@pytest.mark.parametrize("spoil", SPOILS)
def test_the_check_never_raises_and_a_wrong_type_is_a_publish_error(
    publisher: Any, spoil: Any
) -> None:
    publisher.publish(_doc(1))
    data = _spoiled(spoil)
    assert document.validate(data) != []
    with pytest.raises(publish.PublishError):
        publisher.publish(data)
    assert (
        json.loads((publisher.folder / "fleet.json").read_text(encoding="utf-8"))["written_at"]
        == 1001.0
    )


async def test_the_loop_records_a_document_that_fails_the_check(
    publisher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_fleet_fakes import Fleet, linux, writer

    w, _, _ = writer(Fleet({"local": linux()}), sink=publisher)
    assert await w.once()
    real = document.build

    def spoiled(*args: Any, **kwargs: Any) -> dict:
        doc = real(*args, **kwargs)
        doc["machines"][0]["reading"]["pressure"] = "memory 1.3"
        return doc

    monkeypatch.setattr(document, "build", spoiled)
    assert not w.offer(force=True)
    assert any(e["where"] == "publish" for e in w.errors)
    assert (
        json.loads((publisher.folder / "fleet.json").read_text(encoding="utf-8"))["machines"][0][
            "reading"
        ]["pressure"]
        is None
    )


@pytest.mark.parametrize(
    ("names", "listed"),
    [
        (["d.txt", "b.txt", "c.txt", "a.txt"], "a.txt, b.txt, c.txt, ..."),
        (["c.txt", "a.txt", "b.txt"], "a.txt, b.txt, c.txt"),
    ],
)
def test_the_refusal_lists_the_first_three_other_names_sorted_and_says_when_there_are_more(
    tmp_path: Path, names: list[str], listed: str
) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    folder.chmod(0o755)
    for n, name in enumerate(names):  # made in an order that is not the sorted one
        (folder / name).write_text(f"content {n}", encoding="utf-8")
        (folder / name).chmod(0o640)
    with pytest.raises(publish.PublishRefused) as refused:
        publish.Publisher(folder).open()
    assert str(refused.value).endswith(f"holds files the fleet writer does not own: {listed}")
    assert "d.txt" not in str(refused.value)
    for n, name in enumerate(names):
        assert (folder / name).read_text(encoding="utf-8") == f"content {n}"
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o640
    assert sorted(os.listdir(folder)) == sorted(names)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
