"""T-81: cap brief reads in bytes while preserving UTF-8 and boundary behaviour."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from answerback_bridge.routing.cli import MAX_BRIEF_BYTES, _brief


def test_oversized_brief_reads_at_most_one_byte_past_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: observe the bytes pulled from the file, not just its rejection."""
    path = tmp_path / "brief.md"
    path.write_bytes(b"x" * (MAX_BRIEF_BYTES + 50_000))
    reads: list[int] = []
    original_open = Path.open

    class Reader:
        def __init__(self, handle: BinaryIO) -> None:
            self.handle = handle

        def read(self, size: int = -1) -> bytes:
            data = self.handle.read(size)
            reads.append(len(data))
            return data

    @contextmanager
    def tracked_open(selected: Path, *args: Any, **kwargs: Any) -> Iterator[Reader]:
        assert selected == path
        with original_open(selected, *args, **kwargs) as handle:
            yield Reader(handle)

    monkeypatch.setattr(Path, "open", tracked_open)
    with pytest.raises(ValueError, match=f"^Brief exceeds {MAX_BRIEF_BYTES} bytes$"):
        _brief(path)
    assert 0 < sum(reads) <= MAX_BRIEF_BYTES + 1


@pytest.mark.parametrize("text", ["", "Fix the port.\n", "x" * MAX_BRIEF_BYTES, "é" * 49_152])
def test_brief_within_the_byte_limit_is_unchanged(tmp_path: Path, text: str) -> None:
    """pins_existing: empty, short and exact-limit ASCII/UTF-8 bytes decode unchanged."""
    path = tmp_path / "brief.md"
    path.write_bytes(text.encode("utf-8"))
    assert _brief(path) == text


def test_invalid_utf8_within_the_limit_still_fails(tmp_path: Path) -> None:
    """pins_existing: the size cap does not replace strict UTF-8 decoding."""
    path = tmp_path / "brief.md"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        _brief(path)


def test_oversized_invalid_utf8_is_rejected_for_size_first(tmp_path: Path) -> None:
    """pins_existing: size rejection precedes decoding."""
    path = tmp_path / "brief.md"
    path.write_bytes(b"\xff" * (MAX_BRIEF_BYTES + 1))
    with pytest.raises(ValueError, match=f"^Brief exceeds {MAX_BRIEF_BYTES} bytes$"):
        _brief(path)


def test_brief_expands_a_home_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: expansion still occurs before opening the file."""
    path = tmp_path / "brief.md"
    path.write_text("Fix the port.", encoding="utf-8")
    original_expanduser = Path.expanduser
    monkeypatch.setattr(
        Path,
        "expanduser",
        lambda selected: path if selected == Path("~/brief.md") else original_expanduser(selected),
    )
    assert _brief(Path("~/brief.md")) == "Fix the port."
