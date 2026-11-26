"""Unit tests for the hermetic-suite helpers in conftest.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import basetemp_untraversable_component, launchd_live_opt_in

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
COMPONENT_FLAGS = DIRECTORY_FLAGS | os.O_NOFOLLOW


class FakeDirectoryOpens:
    """Stand-in for os.open/os.close that refuses one component with a chosen error."""

    def __init__(self, refused: str, error: type[OSError] = PermissionError) -> None:
        self.refused = refused
        self.error = error
        self.calls: list[tuple[str, int, int | None]] = []
        self.closed: list[int] = []
        self._next_fd = 100

    def open(self, path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        self.calls.append((path, flags, dir_fd))
        if path == self.refused:
            raise self.error(1, "Operation not permitted", path)
        self._next_fd += 1
        return self._next_fd

    def close(self, fd: int) -> None:
        self.closed.append(fd)

    @property
    def opened(self) -> list[int]:
        return list(range(101, self._next_fd + 1))


def probe(monkeypatch: pytest.MonkeyPatch, fake: FakeDirectoryOpens, path: Path) -> str | None:
    with monkeypatch.context() as patched:
        patched.setattr(os, "open", fake.open)
        patched.setattr(os, "close", fake.close)
        return basetemp_untraversable_component(path)


@pytest.mark.parametrize("refused", ["alpha", "beta", "gamma"])
def test_probe_names_the_first_refused_component(
    monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    fake = FakeDirectoryOpens(refused)

    assert probe(monkeypatch, fake, Path("/alpha/beta/gamma")) == refused


def test_probe_walks_from_root_with_the_parent_fd_and_stops_at_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDirectoryOpens("beta")

    assert probe(monkeypatch, fake, Path("/alpha/beta/gamma")) == "beta"
    assert fake.calls == [
        ("/", DIRECTORY_FLAGS, None),
        ("alpha", COMPONENT_FLAGS, 101),
        ("beta", COMPONENT_FLAGS, 102),
    ]
    assert sorted(fake.closed) == fake.opened


def test_probe_returns_none_and_closes_every_fd_when_traversable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDirectoryOpens("never-present")

    assert probe(monkeypatch, fake, Path("/alpha/beta/gamma")) is None
    assert [call[0] for call in fake.calls] == ["/", "alpha", "beta", "gamma"]
    assert all(flags & os.O_NOFOLLOW for _, flags, dir_fd in fake.calls if dir_fd is not None)
    assert sorted(fake.closed) == fake.opened


def test_probe_lets_other_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDirectoryOpens("beta", error=FileNotFoundError)

    with pytest.raises(FileNotFoundError):
        probe(monkeypatch, fake, Path("/alpha/beta/gamma"))
    assert sorted(fake.closed) == fake.opened


def test_probe_on_the_real_basetemp_returns_none_or_one_of_its_components(
    tmp_path: Path,
) -> None:
    component = basetemp_untraversable_component(tmp_path)

    assert component is None or component in tmp_path.parts[1:]
    if component is None:
        fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.close(fd)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"ANSWERBACK_LIVE_LAUNCHD": "1"}, True),
        ({"ANSWERBACK_LIVE_LAUNCHD": "yes"}, False),
        ({"ANSWERBACK_LIVE_LAUNCHD": "true"}, False),
        ({"ANSWERBACK_LIVE_LAUNCHD": "0"}, False),
        ({"ANSWERBACK_LIVE_LAUNCHD": ""}, False),
        ({"ANSWERBACK_LIVE_LAUNCHD": " 1"}, False),
    ],
)
def test_launchd_live_opt_in_requires_the_exact_value_one(
    environ: dict[str, str], expected: bool
) -> None:
    assert launchd_live_opt_in(environ) is expected


def test_launchd_live_opt_in_reads_the_given_mapping_not_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANSWERBACK_LIVE_LAUNCHD", raising=False)

    assert launchd_live_opt_in({"ANSWERBACK_LIVE_LAUNCHD": "1"}) is True
    assert launchd_live_opt_in(os.environ) is False
