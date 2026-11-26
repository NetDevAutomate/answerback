"""T-127: no test leaves another umask behind for the tests that run after it.

`answerback-bridge serve` sets umask 0o077 in the test process (two tests of
tests/test_crash_loop_guard.py run it). Before this row nothing set it back, so every later
test of the session ran under 0o077. The autouse fixture umask_restored in tests/conftest.py
now restores the umask after each test.

The tests here compare with the umask this module saw when pytest imported it. Pytest imports
every test module during collection, before the first test runs, so that value is the umask
the session started with, whatever ran before these tests. The pair at the end relies on the
order of tests inside one file, which pytest keeps; its second test also passes alone.
Without the fixture, the second test of the pair fails, and
test_no_earlier_test_left_another_umask fails when this file runs after
tests/test_crash_loop_guard.py in one session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from conftest import read_umask

# Read at import, which is during collection: no test of this session has run yet.
UMASK_AT_COLLECTION = read_umask()
# A value the pair sets on purpose; it differs from the session's umask in every case.
OTHER_UMASK = 0o027 if UMASK_AT_COLLECTION != 0o027 else 0o077


@pytest.fixture(scope="module", autouse=True)
def session_umask_after_this_module() -> Iterator[None]:
    """Without the conftest fixture the pair below would leave OTHER_UMASK to later files."""
    yield
    os.umask(UMASK_AT_COLLECTION)


def test_no_earlier_test_left_another_umask() -> None:
    assert read_umask() == UMASK_AT_COLLECTION


def test_read_umask_returns_the_umask_and_does_not_change_it() -> None:
    before = os.umask(OTHER_UMASK)
    try:
        assert read_umask() == OTHER_UMASK
        # A second read sees the same value, so the first one changed nothing.
        assert read_umask() == OTHER_UMASK
        assert os.umask(before) == OTHER_UMASK
    finally:
        os.umask(before)
    assert read_umask() == before


def test_pair_first_sets_another_umask_and_leaves_it() -> None:
    """First of the pair: stands for a test whose product code sets a umask."""
    os.umask(OTHER_UMASK)
    assert read_umask() == OTHER_UMASK


def test_pair_second_sees_the_umask_from_before() -> None:
    """Second of the pair: red without the fixture, green with it, and green alone."""
    assert read_umask() == UMASK_AT_COLLECTION
