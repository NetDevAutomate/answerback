"""Questions to the bridge service over its local socket (effects; spec section 7)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..process import BridgeError
from ..transport import TransportUnavailable
from . import bounds, receipts

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
TASK_LIMIT = 100


@dataclass(frozen=True)
class Answer:
    """What the service said in one cycle; ``status`` None means it did not answer."""

    status: dict[str, Any] | None
    status_error: str | None
    tasks: list[Any] | None
    source: str
    tasks_error: str | None
    counts: dict[str, Any] | None = None  # the snapshot's own counts, when it gave them
    version: str | None = None  # the snapshot's ``health.version``, when it gave one


SNAPSHOT_PAUSE_S = 300.0  # at most 288 requests a day for a method that fails every time


@dataclass(frozen=True)
class Refusal:
    """What the current instance did with ``fleet_snapshot`` (instance None: no id).

    ``until`` None: a refusal, not asked again. Else: asked again at that monotonic time.
    """

    instance: str | None
    until: float | None = None


@dataclass
class SnapshotMemory:
    """The last failure of ``fleet_snapshot`` by the current instance; one value, never a set."""

    refused: Refusal | None = None

    def asks(self, instance: str | None, now: float) -> bool:
        """True unless this very instance refused, or failed less than 300 s before ``now``.

        A new instance forgets what the earlier one did.
        """
        r = self.refused
        if r is None:
            return True
        if r.instance != instance:
            self.refused = None
            return True
        return r.until is not None and now >= r.until

    def refuse(self, instance: str | None) -> None:
        """Remember a refusal (``operation_unknown``, or an answer that cannot be used)."""
        self.refused = Refusal(instance)

    def pause(self, instance: str | None, now: float) -> None:
        """Remember another error answer: ask again ``SNAPSHOT_PAUSE_S`` after ``now``."""
        self.refused = Refusal(instance, now + SNAPSHOT_PAUSE_S)

    def forget(self) -> None:
        """The method answered: nothing to remember."""
        self.refused = None


def _operation_unknown(error: BaseException) -> bool:
    from ..errors import PublicBridgeError

    return isinstance(error, PublicBridgeError) and error.code == "operation_unknown"


POLL_S = 0.01
NO_TIME = "no time left in the cycle"


class Remote:
    """The client for the service's socket, built once, outside the event loop.

    Building it reads files, which an async deadline cannot interrupt. So it is built in
    one thread, one build at a time. ``prepare`` waits for it at most ``timeout`` seconds;
    a build that finishes later is used from the next cycle. A failed build is tried again
    at the next ``prepare``.
    """

    def __init__(self, build: Callable[[], Any]) -> None:
        self._build = build
        self._client: Any = None
        self._pending: concurrent.futures.Future[Any] | None = None

    def _start(self) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(self._build())
            except BaseException as error:  # handed to the waiting cycle
                future.set_exception(error)

        threading.Thread(target=run, name="fleet-service-client", daemon=True).start()
        return future

    async def prepare(self, timeout: float) -> None:  # noqa: ASYNC109
        """Have the client ready within ``timeout`` seconds, or raise ``TimeoutError``."""
        if self._client is not None:
            return
        if self._pending is None:
            self._pending = self._start()
        future = self._pending
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0.0, timeout)
        while not future.done():
            left = end - loop.time()
            if left <= 0:
                raise TimeoutError
            await asyncio.sleep(min(POLL_S, left))
        self._pending = None
        self._client = future.result()  # a failed build raises here and is tried again

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One call through the built client."""
        if self._client is None:
            raise RuntimeError("the service client is not built yet")
        return await self._client.call(method, params)


def remote(config_path: Path) -> Remote:
    """The client of the bridge service of ``config_path``."""

    def build() -> Any:
        from .. import transport

        return transport.RemoteBridge(config_path)

    return Remote(build)


class Unusable(ValueError):
    """An answer came and cannot be used."""


UNUSABLE = "service answer cannot be used"


def unexpected(error: BaseException) -> str:
    """The text for an unexpected error of the service step."""
    return f"{UNUSABLE}: unexpected {type(error).__name__}"


def _why(error: BaseException) -> str:
    """Say what happened: no answer, an error answer, or an answer that cannot be used."""
    text = bounds.clean(str(error), bounds.TEXT)
    if isinstance(error, Unusable):
        return f"{UNUSABLE}: {text}" if text else UNUSABLE
    if isinstance(error, TransportUnavailable | OSError | TimeoutError):
        return f"service did not answer: {text}" if text else "service did not answer"
    if isinstance(error, BridgeError):
        return (
            f"service answered with an error: {text}" if text else "service answered with an error"
        )
    return unexpected(error)


def instance_of(status: dict[str, Any]) -> str | None:
    """The service instance a status answer names; None when it names none."""
    return bounds.clean(status.get("instance_id"), bounds.NAME)


async def ask(
    call: Call,
    *,
    timeout: float,  # noqa: ASYNC109
    deadline: float | None = None,
    memory: SnapshotMemory | None = None,
    now: float | None = None,
) -> Answer:
    """``status``, then ``fleet_snapshot``, else ``task_list``.

    Each call has ``timeout`` seconds at most and none starts at or after ``deadline``, the
    loop time at which the cycle's steps end. ``memory`` holds the current instance when it
    refused ``fleet_snapshot`` or answered what cannot be used: it is not asked again, since
    the service logs an error line for every request it refuses. Another error answer pauses
    the question for 300 s, judged at ``now`` (the writer's monotonic clock; default: the
    loop's clock). A timeout is no refusal.
    """
    memory = SnapshotMemory() if memory is None else memory
    loop = asyncio.get_running_loop()
    now = loop.time() if now is None else now
    end = loop.time() + timeout if deadline is None else deadline

    def window() -> float:
        """Seconds for the next call; raises when the deadline has come."""
        left = end - loop.time()
        if left <= 0:
            raise _NoTime
        return min(timeout, left)

    try:
        prepare = getattr(call, "prepare", None)
        if prepare is not None:
            await prepare(window())
        async with asyncio.timeout(window()):
            status = await call("status", {})
        problem = receipts.status_problem(status)
        if problem is not None:
            raise Unusable(problem)
    except _NoTime:  # the cycle's time was used up before the call: it never started
        return Answer(None, NO_TIME, None, "status+task_list", None)
    except TimeoutError:
        return Answer(None, "service did not answer in time", None, "status+task_list", None)
    except Exception as error:  # any failure of a local socket call is a service failure
        return Answer(None, _why(error), None, "status+task_list", None)
    instance = instance_of(status)
    try:
        if memory.asks(instance, now):
            try:
                async with asyncio.timeout(window()):
                    snapshot = await call("fleet_snapshot", {})
            except _NoTime:
                raise
            except TimeoutError:
                pass  # case 1, no answer in time: asked again in the next cycle
            except (TransportUnavailable, OSError):
                pass  # case 1, no usable reply (the service's timeout answer too)
            except BridgeError as error:
                if _operation_unknown(error):  # case 2: a service built before the method was added
                    memory.refuse(instance)
                else:  # case 3: the method exists and failed, perhaps a busy store
                    memory.pause(instance, now)
            except Exception as error:  # case 6: unexpected; the service step ends
                return Answer(status, None, None, "status+task_list", unexpected(error))
            else:
                if _incomplete(snapshot):
                    usable = None  # case 5: asked again in the next cycle, nothing remembered
                elif (usable := _snapshot(snapshot)) is None:
                    memory.refuse(instance)  # case 4: an answer that cannot be used
                if usable is not None:
                    memory.forget()
                    tasks, counts = usable
                    version = _snapshot_version(snapshot)
                    return Answer(status, None, tasks, "fleet_snapshot", None, counts, version)
        async with asyncio.timeout(window()):
            listed = await call("task_list", {"limit": TASK_LIMIT})
        tasks = listed.get("tasks") if isinstance(listed, dict) else None
        problem = receipts.tasks_problem(tasks)
        if problem is not None:
            raise Unusable(problem)
        assert isinstance(tasks, list)
    except _NoTime:
        return Answer(status, None, None, "status+task_list", NO_TIME)
    except TimeoutError:
        return Answer(status, None, None, "status+task_list", "task_list did not answer in time")
    except Exception as error:
        return Answer(status, None, None, "status+task_list", _why(error))
    return Answer(status, None, tasks, "status+task_list", None)


def _incomplete(answer: Any) -> bool:
    """True when ``recent`` or ``needs_person`` is there and is ``null``: a part not read."""
    return isinstance(answer, dict) and any(
        key in answer and answer[key] is None for key in ("recent", "needs_person")
    )


def _snapshot(answer: Any) -> tuple[list[Any], dict[str, Any] | None] | None:
    """The receipts and counts of a ``fleet_snapshot`` answer; None when it cannot be used.

    ``counts`` absent or null: the counts of ``status`` are used. ``counts`` that cannot be
    used make the whole answer unusable.
    """
    if not isinstance(answer, dict):
        return None
    tasks = answer.get("tasks")
    if receipts.tasks_problem(tasks) is not None:
        return None
    assert isinstance(tasks, list)
    counts = answer.get("counts")
    if counts is None:
        return tasks, None
    if receipts.counts_problem(counts) is not None:
        return None
    assert isinstance(counts, dict)
    return tasks, counts


def _snapshot_version(answer: dict[str, Any]) -> str | None:
    """The bridge's version as the snapshot's ``health`` says it; None when it says none.

    The document's ``health.service.version`` prefers it to the version of ``status``; the
    figure is bounded like every text of the service.
    """
    health = answer.get("health")
    if not isinstance(health, dict):
        return None
    return bounds.clean(health.get("version"), bounds.NAME)


class _NoTime(Exception):
    """The deadline of the cycle has come: no further call is started."""
