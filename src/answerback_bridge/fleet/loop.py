"""The writer's cycles and its publication decisions (effects; spec sections 3 and 9).

The clock, the sleep, the child runner, the service call and the publisher are passed in.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .. import __version__
from ..placement.assess import Assessment
from ..placement.probe import LOCAL, Reading
from ..process import BridgeError, CommandCleanupError
from . import advice, agents, bounds, cadence, document, readings, receipts
from .agents import DEFAULT_LABEL
from .probes import Listing, ListingError, Runner, list_core, measure
from .publish import PublishError
from .service import NO_TIME, Answer, Call, SnapshotMemory, ask, unexpected

# The exceptions whose message was made on purpose and may enter the document. Every other
# exception is unexpected: its entry names its type and never its message.
EXPECTED = (OSError, BridgeError, ListingError)


@dataclass(frozen=True)
class Timings:
    """Deadlines in seconds; the defaults are those of the specification."""

    hc_ls: float = 3.0
    coordinator: float = 5.0
    worker: float = 8.0
    service: float = 2.0
    cycle: float = 12.0
    reserve: float = 2.0  # of the cycle, kept for ending and reaping children


@dataclass(frozen=True)
class Clock:
    """The wall clock, the monotonic clock and a sleep, all replaceable in tests."""

    wall: Callable[[], float]
    mono: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]


class Sink(Protocol):
    """What the loop publishes to: a ``Publisher`` or a test double."""

    def publish(self, data: bytes) -> None:
        """Publish ``data`` or raise ``PublishError``."""
        ...


@dataclass
class Settings:
    """The writer's settings: all of them come from the command line."""

    machines: list[str] = field(default_factory=list)
    ceilings: dict[str, int] = field(default_factory=dict)
    hc_binary: str = "hc"
    hc_session: str | None = None
    lane_label: str = DEFAULT_LABEL
    timings: Timings = field(default_factory=Timings)


class Writer:
    """One fleet writer: its memory between cycles and its publication decisions."""

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner,
        call: Call,
        sink: Sink,
        clock: Clock,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.call = call
        self.sink = sink
        self.clock = clock
        self.writer = {"version": __version__, "instance_id": str(uuid.uuid4()), "pid": os.getpid()}
        self.memos: dict[str | None, readings.Memo] = {}
        self.links: dict[str, str] = {}
        self.order: list[str] = list(settings.machines)
        self.active: dict[str | None, int] = {}
        self.lanes: dict[str | None, int] | None = None
        self.standing: set[str] = set()
        self.addresses: frozenset[str] | None = None
        self.launches: tuple[advice.Launch, ...] = ()
        self.cadence = cadence.Cadence("normal", cadence.INTERVALS["normal"], None)
        self.paused_since: float | None = None
        self.taken_at: float | None = None
        self.health = receipts.unreachable(None, error="service not asked yet")
        self.good: receipts.Good | None = None
        self.snapshots = SnapshotMemory()
        self.out_of_time = False
        self.receipts = receipts.view(None, stale=True, error=None, source="status+task_list")
        self.errors: deque[dict[str, Any]] = deque(maxlen=bounds.ERRORS)
        self.last_cycle: tuple[float, float] | None = None
        self.last_write: float | None = None
        self.last_attempt: float | None = None
        self.steps = 0
        self.last_key: bytes | None = None
        self.pending = False
        self.published = 0
        self.skipped = 0
        self.cycles = 0

    # ------------------------------------------------------------------ errors

    def error(self, where: str, text: str) -> None:
        """Add one error entry; the same text from the same place is not repeated."""
        if self.errors and self.errors[-1]["where"] == where and self.errors[-1]["text"] == text:
            return
        self.errors.append({"at": self.clock.wall(), "where": where, "text": text})

    def standing_error(self, where: str, text: str) -> None:
        """One error entry for a condition that lasts; none again until it has ended."""
        if where not in self.standing:
            self.standing.add(where)
            self.error(where, text)

    def resolved(self, where: str) -> None:
        """The lasting condition of ``where`` has ended."""
        self.standing.discard(where)

    # ------------------------------------------------------------------ one cycle

    async def cycle(self) -> None:
        """Measure, list, probe, ask and remember, within the cycle's budget.

        A step waits until second 10 of the cycle at most and starts only while time is
        left before it. The last 2 seconds are kept for ending children.
        """
        t = self.settings.timings
        loop = asyncio.get_running_loop()
        steps_end = loop.time() + t.cycle - t.reserve
        sent_at, sent_mono = self.clock.wall(), self.clock.mono()
        self.cycles += 1
        self._check_clocks(sent_at, sent_mono)
        self.last_cycle = (sent_at, sent_mono)
        self.taken_at = sent_at
        self.out_of_time = False

        def left(limit: float) -> float:
            return max(0.0, min(limit, steps_end - loop.time()))

        def may_start() -> bool:
            if loop.time() < steps_end:
                return True
            self._say_no_time()
            return False

        # 2. The coordinator, from load and memory only. It runs first and alone.
        local = await self._measure(LOCAL, sent_at, left(t.coordinator))
        self.memos[None] = readings.record(
            self.memos.get(None, readings.Memo()),
            local,
            sent_at,
            sent_mono,
            active=None,
            ceiling=None,
        )
        last = self.memos[None].last
        # The mode follows the last good reading while it is at most 60 s old.
        current = readings.is_fresh(self.memos[None], sent_mono)
        assessment: Assessment | None = last.assessment if last and current else None
        reading: Reading | None = last.reading if last and current else None
        # 3. The mode.
        chosen = cadence.choose(reading, assessment)
        if chosen.pause is not None and self.paused_since is None:
            self.paused_since = sent_at
        elif chosen.pause is None:
            self.paused_since = None
        self.cadence = chosen
        # 4. hc ls --json, and at the same time the listing of the labelled lanes.
        listing = labelled = None
        if may_start():
            listing, labelled = await self._listings(left(t.hc_ls))
        self._take_listing(listing, labelled, sent_at, sent_mono)
        # 5 and 6. Workers and the service at the same time, within what is left.
        probing = chosen.pause is None
        targets = [n for n in self.order if self.links.get(n) == "connected"] if probing else []
        tasks: dict[str, asyncio.Task[Reading]] = {}
        service: asyncio.Task[Answer] | None = None
        if may_start():
            tasks = {
                name: asyncio.create_task(self._measure(name, sent_at, left(t.worker)))
                for name in targets
            }
            service = asyncio.create_task(
                ask(
                    self.call,
                    timeout=t.service,
                    deadline=steps_end,
                    memory=self.snapshots,
                    now=sent_mono,
                )
            )
        everything: set[asyncio.Task[Any]] = {*tasks.values()}
        if service is not None:
            everything.add(service)
        try:
            if everything:
                await asyncio.wait(everything, timeout=left(t.cycle))
        finally:
            # Unfinished steps end and reap their children at the same time, also when
            # the cycle itself is cancelled by SIGTERM or SIGINT.
            pending = [task for task in everything if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for name in self.order:
            memo = self.memos.get(name, readings.Memo())
            if self.links.get(name) != "connected":
                memo = readings.fail(memo, readings.NO_LINK)
            elif not probing:
                memo = readings.wait(memo, readings.PAUSED)
            elif name not in tasks:
                memo = readings.fail(memo, NO_TIME)
            else:
                memo = self._probe_result(name, tasks[name], memo, sent_at, sent_mono)
            self.memos[name] = memo
        if service is None:
            answer = Answer(None, NO_TIME, None, "status+task_list", None)
        elif service.cancelled():
            answer = Answer(None, "service did not answer in time", None, "status+task_list", None)
        elif (failure := service.exception()) is not None:
            answer = Answer(None, unexpected(failure), None, "status+task_list", None)
        else:
            answer = service.result()
        try:
            self._take_answer(answer, sent_at)
        except Exception as error:  # mapping an answer never stops the cycle
            text = unexpected(error)
            self._take_answer(Answer(None, text, None, "status+task_list", None), sent_at)

    def _probe_result(
        self,
        name: str,
        task: asyncio.Task[Reading],
        memo: readings.Memo,
        sent_at: float,
        sent_mono: float,
    ) -> readings.Memo:
        if task.cancelled():
            return readings.fail(memo, readings.ERRORS["timeout"])
        error = task.exception()
        if isinstance(error, TimeoutError):
            return readings.fail(memo, readings.ERRORS["timeout"])
        if error is not None:
            self.error("probe", f"unexpected error in the probe of {name}: {type(error).__name__}")
            return readings.fail(memo, readings.ERRORS["unreachable"])
        return readings.record(
            memo,
            task.result(),
            sent_at,
            sent_mono,
            active=self.active.get(name),
            ceiling=self.settings.ceilings.get(name),
        )

    def _check_clocks(self, wall: float, mono: float) -> None:
        """Expire every reading when the two clocks disagree by more than 5 s.

        Runs at the start of a cycle and before every rewrite between cycles, against the
        clocks of the last cycle. After a jump the clocks of now are the new reference.
        """
        if self.last_cycle is None:
            return
        jump = cadence.clock_jump(*self.last_cycle, wall, mono)
        if jump is None:
            return
        self.memos = {name: readings.expire(memo) for name, memo in self.memos.items()}
        self.error(
            "clock",
            f"wall and monotonic clocks differ by {jump:.1f} s; "
            "every reading is treated as expired",
        )
        self.last_cycle = (wall, mono)

    async def _run(self, argv: list[str]) -> tuple[int, str | bytes]:
        """Run one child through the runner; say so when its end is not confirmed."""
        try:
            return await self.runner(argv)
        except CommandCleanupError:
            self.error("child", "the system did not confirm the end of a child within 2 s")
            raise

    async def _measure(self, name: str, sent_at: float, timeout: float) -> Reading:  # noqa: ASYNC109
        try:
            return await measure(
                name,
                runner=self._run,
                sent_at=sent_at,
                timeout=timeout,
                hc_binary=self.settings.hc_binary,
                hc_session=self.settings.hc_session,
            )
        except EXPECTED as error:  # text made on purpose
            self.error("probe", bounds.clean(str(error), bounds.TEXT) or "probe failed")
            return Reading(name, sent_at, error="unreachable")
        except Exception as error:  # unexpected: its type only, never its message
            self.error("probe", f"unexpected error in the probe of {name}: {type(error).__name__}")
            return Reading(name, sent_at, error="unreachable")

    async def _listings(
        self,
        timeout: float,  # noqa: ASYNC109
    ) -> tuple[Listing | None, Listing | None]:
        """Both listings at the same time. Whatever ends this stage, both are ended."""
        first = asyncio.create_task(self._list(timeout))
        lanes = asyncio.create_task(self._list_lanes(timeout))
        try:
            await asyncio.wait({first, lanes})
        finally:
            # A signal or an error that ends the stage: no listing, and no child of
            # one, outlives it.
            pending = [task for task in (first, lanes) if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return first.result(), lanes.result()

    async def _list(self, timeout: float) -> Listing | None:  # noqa: ASYNC109
        try:
            return await list_core(self._run, self.settings.hc_binary, timeout=timeout)
        except TimeoutError:
            self.error("hc", "hc ls --json did not answer in time")
        except EXPECTED as error:  # text made on purpose
            text = bounds.clean(str(error), bounds.TEXT) or "unusable answer"
            self.error("hc", f"hc ls --json failed: {text}")
        except Exception as error:  # an unexpected error fails this listing only
            self.error("hc", f"hc ls --json failed: unexpected {type(error).__name__}")
        return None

    async def _list_lanes(self, timeout: float) -> Listing | None:  # noqa: ASYNC109
        label = self.settings.lane_label
        try:
            labelled = await list_core(
                self._run, self.settings.hc_binary, timeout=timeout, label=label
            )
        except TimeoutError:
            self.standing_error("hc-label", f"hc ls --label {label} did not answer in time")
        except EXPECTED as error:  # text made on purpose
            text = bounds.clean(str(error), bounds.TEXT) or "unusable answer"
            self.standing_error("hc-label", f"hc ls --label {label} failed: {text}")
        except Exception as error:  # an unexpected error fails this listing only
            name = type(error).__name__
            self.standing_error("hc-label", f"hc ls --label {label} failed: unexpected {name}")
        else:
            self.resolved("hc-label")
            return labelled
        return None

    def _take_listing(
        self, listing: Listing | None, labelled: Listing | None, wall: float, mono: float
    ) -> None:
        if listing is None:
            # Without a listing the link of every worker is not known, nor its agents.
            self.links = {name: "unknown" for name in self.order}
            self.active, self.lanes = {}, None
            return
        for name in listing.refused[:3]:
            self.error("hc", f"machine name refused: {name}")
        listed = [name for name, _ in listing.machines]
        if self.settings.machines:
            wanted = self.settings.machines
            order = [n for n in listed if n in wanted] + [n for n in wanted if n not in listed]
        else:
            order = listed
        if len(order) > bounds.MACHINES - 1:
            self.error("machines", f"{len(order)} workers; only the first 15 are measured")
            order = order[: bounds.MACHINES - 1]
        self.order = order
        states = dict(listing.machines)
        self.links = {name: states.get(name, "unknown") for name in order}
        active, lanes = agents.count(
            listing.terminals, None if labelled is None else labelled.terminals
        )
        counted = [None, *(name for name in order if name in states)]
        self.active = {name: active.get(name, 0) for name in counted}
        self.lanes = None if lanes is None else {name: lanes.get(name, 0) for name in counted}
        self.launches = advice.launches(
            self.addresses, listing.addresses, self.launches, now_wall=wall, now_mono=mono
        )
        self.addresses = listing.addresses

    def _say_no_time(self) -> None:
        """One error entry for each cycle whose time was used up before a step or call."""
        if not self.out_of_time:
            self.out_of_time = True
            self.error("cycle", NO_TIME)

    def _take_answer(self, answer: Answer, wall: float) -> None:
        if answer.status is None:
            text = answer.status_error or "service did not answer"
            self.health = receipts.unreachable(self.health, error=text)
            self.receipts = receipts.view(
                self.good, stale=True, error=text, source=self.receipts["source"]
            )
            if text == NO_TIME:
                self._say_no_time()
            else:
                self.error("service", text)
            return
        self.health = receipts.health(answer.status, read_at=wall, version=answer.version)
        if answer.tasks is None:
            text = answer.tasks_error or "receipts could not be read"
            self.receipts = receipts.view(self.good, stale=True, error=text, source=answer.source)
            if text == NO_TIME:
                self._say_no_time()
            else:
                self.error("service", text)
            return
        self.good = receipts.good(answer.source, wall, answer.status, answer.tasks, answer.counts)
        self.receipts = receipts.view(self.good, stale=False, error=None, source=answer.source)

    # ------------------------------------------------------------------ publication

    def inputs(self) -> document.Inputs:
        """The values the next document is built from."""
        return document.Inputs(
            writer=dict(self.writer),
            cadence=self.cadence,
            paused_since=self.paused_since,
            taken_at=self.taken_at,
            node_order=list(self.order),
            links=dict(self.links),
            memos=dict(self.memos),
            active=dict(self.active),
            lanes=None if self.lanes is None else dict(self.lanes),
            ceilings={n: c for n, c in self.settings.ceilings.items() if n in self.order},
            launches=self.launches,
            health=self.health,
            receipts=self.receipts,
            errors=list(self.errors),
        )

    def offer(self, *, force: bool = False) -> bool:
        """Build the document now and publish it when the write gate says so.

        An unexpected error while the document is built, keyed or encoded is a failed
        publication: one error entry with its type, the previous file stays, and the gap
        of 5 seconds holds before the next attempt.
        """
        now = self.clock.mono()
        self._check_clocks(self.clock.wall(), now)
        tried = None if self.last_attempt is None else now - self.last_attempt
        try:
            doc = document.build(self.inputs(), written_at=self.clock.wall(), now_mono=now)
            key = document.content_key(doc)
            changed = key != self.last_key
            since = None if self.last_write is None else now - self.last_write
            if not force and not cadence.should_write(changed, since, tried):
                self.pending = changed or self.pending
                return False
            self.last_attempt = now
            data = document.encode(doc)
        except Exception as error:
            if force or tried is None or tried >= cadence.MIN_GAP_S:
                self.last_attempt = now  # an attempt the gap allowed, and it failed
            self.error("publish", f"the document could not be made: {type(error).__name__}")
            self.pending = True
            return False
        try:
            self.sink.publish(data)
        except PublishError as error:
            self.error("publish", str(error))
            self.pending = True
            return False
        self.last_write, self.last_key, self.pending = now, key, False
        self.published += 1
        return True

    async def guarded_cycle(self) -> None:
        """One cycle; an unexpected error ends it with one error entry naming its type.

        A cancellation by a signal is not such an error and passes through.
        """
        try:
            await self.cycle()
        except Exception as error:
            self.error("cycle", f"unexpected error ended the cycle: {type(error).__name__}")

    async def once(self) -> bool:
        """One cycle and one publication; True when a document was published."""
        await self.guarded_cycle()
        return self.offer(force=True)

    async def once_until(self, stop: asyncio.Event) -> bool | None:
        """One cycle and one publication unless ``stop`` comes first (then None)."""
        if not await self._until_stop(self.guarded_cycle(), stop):
            return None
        return self.offer(force=True)

    async def serve(self, stop: asyncio.Event) -> None:
        """Cycles until ``stop`` is set. A tick inside a running cycle is skipped."""
        tick = self.clock.mono()
        while not stop.is_set():
            scheduled = tick
            if not await self._until_stop(self.guarded_cycle(), stop):
                return
            self.offer()
            tick, skipped = cadence.next_tick(scheduled, self.cadence.interval_s, self.clock.mono())
            self.skipped += skipped
            while not stop.is_set():
                # Every step of this loop sleeps: a publication that fails every time
                # costs a few attempts in a cycle, never a busy processor.
                self.steps += 1
                wake = cadence.next_wake(
                    tick=tick,
                    last_write=self.last_write,
                    pending=self.pending,
                    last_attempt=self.last_attempt,
                )
                delay = max(cadence.LEAST_SLEEP_S, wake - self.clock.mono())
                if not await self._until_stop(self.clock.sleep(delay), stop):
                    return
                if self.clock.mono() >= tick:
                    break
                self.offer()

    @staticmethod
    async def _until_stop(work: Awaitable[Any], stop: asyncio.Event) -> bool:
        """Run ``work`` unless ``stop`` comes first; then cancel it and wait for its end."""
        task = asyncio.ensure_future(work)
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not task.done():
                # SIGTERM or SIGINT: end the running cycle; its children are reaped.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if stop.is_set():
            return False
        task.result()
        return True
