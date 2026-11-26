"""fleet: a publication ends its write loop; a broken loop fails within seconds.

This file's name sorts before every other fleet test file. A sweep of single edits stops at
the first failing test: with a write loop that never ends, every earlier publication would
hang, and this test, which fails at once, would not be reached.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_lifetime import no_descriptor_left, process_unchanged
from test_fleet_publish import _doc

from answerback_bridge.fleet import publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def test_a_publication_ends_its_write_loop_when_every_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write
    calls = {"n": 0}

    def write(fd: int, data: Any) -> int:
        calls["n"] += 1
        if calls["n"] > 1000:  # far more calls than one document needs
            raise AssertionError("os.write was called more than 1000 times for one document")
        return real(fd, data)

    data = _doc(1, pad=4000)
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    try:
        monkeypatch.setattr(publish.os, "write", write)
        with deadline("one publication"):
            p.publish(data)
    finally:
        p.close()
    assert (tmp_path / "fleet" / "fleet.json").read_bytes() == data
