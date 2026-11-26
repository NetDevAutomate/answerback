"""Execution deadline that excludes nested waits for a human decision."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar

T = TypeVar("T")


class ActiveBudget:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._expired = False
        self._remaining = seconds
        self._clock = clock
        self._since = clock()
        self._pauses = 0
        self._timeout: asyncio.Timeout | None = None

    @property
    def seconds(self) -> float:
        """The active seconds this budget was made with."""
        return self._seconds

    @property
    def expired(self) -> bool:
        """Whether this budget's own limit expired during its last use of ``limit()``.

        False for a TimeoutError that the body raised itself before the limit was reached.
        """
        return self._expired

    @property
    def remaining(self) -> float:
        elapsed = 0 if self._pauses else self._clock() - self._since
        return max(0, self._remaining - elapsed)

    def pause(self) -> None:
        if not self._pauses:
            self._remaining = self.remaining
        self._pauses += 1
        self._schedule()

    def resume(self) -> None:
        if not self._pauses:
            raise RuntimeError("Execution budget is not paused")
        self._pauses -= 1
        if not self._pauses:
            self._since = self._clock()
        self._schedule()

    def _schedule(self) -> None:
        if self._timeout is not None and not self._timeout.expired():
            deadline = None if self._pauses else asyncio.get_running_loop().time() + self.remaining
            self._timeout.reschedule(deadline)

    @asynccontextmanager
    async def limit(self) -> AsyncIterator[None]:
        if self._timeout is not None:
            raise RuntimeError("Execution budget is already in use")
        async with asyncio.timeout(None) as timeout:
            self._timeout = timeout
            self._schedule()
            try:
                yield
            finally:
                self._expired = timeout.expired()
                self._timeout = None

    async def run(self, operation: Coroutine[Any, Any, T]) -> T:
        async with self.limit():
            return await operation
