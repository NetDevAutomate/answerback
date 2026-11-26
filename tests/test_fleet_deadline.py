"""fleet: one deadline for calls that must return at once; a blocked call fails a test."""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Iterator
from types import FrameType

import pytest

DEADLINE_S = 10.0


class Blocked(AssertionError):
    """A call that should return at once was still blocked when its deadline came."""


@contextlib.contextmanager
def deadline(what: str, seconds: float = DEADLINE_S) -> Iterator[None]:
    """Fail with ``Blocked`` when the block takes longer than ``seconds``.

    An interval timer of the main thread interrupts the blocked system call: its handler
    raises, so the call ends with that exception and no thread or child is left behind.
    The handler that was there before is restored and the timer is cleared, also on
    success. The helper is for a test process that runs no other interval timer (this
    suite has none).
    """

    def expired(signum: int, frame: FrameType | None) -> None:
        raise Blocked(f"{what} was still blocked after {seconds:g} s")

    before = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def test_a_blocked_call_ends_as_a_failure_within_its_deadline() -> None:
    handler = signal.getsignal(signal.SIGALRM)
    read_end, write_end = os.pipe()
    begun = time.monotonic()
    try:
        with pytest.raises(Blocked, match="a read from an empty pipe was still blocked"):
            with deadline("a read from an empty pipe", 0.2):
                os.read(read_end, 1)  # nobody writes: this blocks
    finally:
        os.close(read_end)
        os.close(write_end)
    assert time.monotonic() - begun < 2.0
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_call_that_returns_leaves_the_timer_and_handler_as_before() -> None:
    handler = signal.getsignal(signal.SIGALRM)
    with deadline("nothing", 0.2):
        pass
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
