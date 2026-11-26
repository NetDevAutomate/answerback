"""Only disposable children and inert signal targets; no installed clients."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from answerback_bridge import process
from answerback_bridge._waitid import peek_exit
from answerback_bridge.codex import HistoryReader


@pytest.mark.parametrize("cleanup_seconds", [None, 0.1])
async def test_reaped_pid_is_never_a_signal_target(
    monkeypatch: pytest.MonkeyPatch, cleanup_seconds: float | None
) -> None:
    """The numeric PID now denotes another process; a cached object grants nothing."""
    signals = []
    monkeypatch.setattr(process.os, "killpg", lambda *args: signals.append(args))
    reaped = SimpleNamespace(pid=123456789, returncode=0, wait=AsyncMock(return_value=0))
    try:
        await process.stop_child(reaped, cleanup_seconds=cleanup_seconds)
    except process.CommandCleanupError:
        pass
    assert signals == []


@pytest.mark.parametrize("cleanup_seconds", [None, 0.1])
async def test_stale_asyncio_returncode_does_not_establish_group_ownership(
    monkeypatch: pytest.MonkeyPatch, cleanup_seconds: float | None
) -> None:
    """The child watcher may reap before its loop callback publishes returncode."""
    signals = []
    monkeypatch.setattr(process.os, "killpg", lambda *args: signals.append(args))
    stale = SimpleNamespace(pid=123456789, returncode=None, wait=AsyncMock(return_value=0))
    with pytest.raises(process.CommandCleanupError, match="ownership"):
        await process.stop_child(stale, cleanup_seconds=cleanup_seconds)
    assert signals == []


async def test_default_grace_does_not_signal_again_after_wait_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = []
    proc = SimpleNamespace(pid=123456789, returncode=None)

    async def reaped() -> int:
        proc.returncode = 0
        await asyncio.sleep(0)
        return 0

    proc.wait = reaped
    monkeypatch.setattr(process.os, "killpg", lambda *args: signals.append(args))
    try:
        await process.stop_child(proc)
    except process.CommandCleanupError:
        pass
    assert not signals, "An unowned asyncio process must never authorize group signals"


async def test_wait_retains_leader_for_descendant_cleanup() -> None:
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']); "
        "raise SystemExit(7)"
    )
    proc = await process.spawn_owned(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    try:
        assert await asyncio.wait_for(proc.wait(), 2) == 7
        assert peek_exit(proc.pid) == 7
        assert peek_exit(proc.pid) == 7
        await process.stop_child(proc, cleanup_seconds=1)
        assert await asyncio.wait_for(proc.stdout.read(), 2) == b""
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)  # noqa: ASYNC222 — explicitly nonblocking
        # Idempotent cleanup must not signal a newly reused PID.
        await process.stop_child(proc, cleanup_seconds=1)
    finally:
        await process.stop_child(proc, cleanup_seconds=1)


async def test_competing_reap_refuses_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = await process.spawn_owned(sys.executable, "-c", "pass", start_new_session=True)
    await asyncio.wait_for(proc.wait(), 2)
    os.waitpid(proc.pid, os.WNOHANG)  # noqa: ASYNC222 — wait() already observed exit
    signals = []
    monkeypatch.setattr(process.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(process.CommandCleanupError, match="ownership"):
        await process.stop_child(proc, cleanup_seconds=1)
    assert signals == []


async def test_exit_status_signal_is_preserved() -> None:
    proc = await process.spawn_owned(
        sys.executable,
        "-c",
        "import os,signal; os.kill(os.getpid(),signal.SIGTERM)",
        start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(proc.wait(), 2) == -signal.SIGTERM
    finally:
        await process.stop_child(proc, cleanup_seconds=1)


async def test_open_descendant_pipe_keeps_cleanup_incomplete() -> None:
    """An escaped short-lived fixture must not be mistaken for completed group cleanup."""
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(.4)'],"
        "start_new_session=True)"
    )
    proc = await process.spawn_owned(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    await asyncio.wait_for(proc.wait(), 2)
    try:
        with pytest.raises(process.CommandCleanupError, match="cleanup"):
            await process.stop_child(proc, cleanup_seconds=0.05)
    finally:
        # This fixture intentionally leaves the owned group; never signal it by PID.
        await asyncio.sleep(0.5)


async def test_cancel_during_creation_settles_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling spawn must not lose the child created by its blocking syscall."""
    ready = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    created = []
    original = process.subprocess.Popen

    def delayed(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        created.append(child)
        loop.call_soon_threadsafe(ready.set)
        release.wait(2)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", delayed)
    started = loop.time()
    task = asyncio.create_task(
        process.spawn_owned(
            sys.executable, "-c", "import time; time.sleep(5)", start_new_session=True
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert loop.time() - started < 1, "Process creation must not block the event loop"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert created[0].returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(created[0].pid, os.WNOHANG)  # noqa: ASYNC222 — explicitly nonblocking
    finally:
        release.set()
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, process.OwnedChild):
            await process.stop_child(result, cleanup_seconds=1)


async def test_attachment_failure_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    created = []

    async def broken(self: process.OwnedChild, limit: int) -> None:
        created.append(self)
        raise OSError("fixture pipe attachment failed")

    monkeypatch.setattr(process.OwnedChild, "_connect", broken)
    with pytest.raises(OSError, match="fixture pipe"):
        await process.spawn_owned(
            sys.executable, "-c", "import time; time.sleep(5)", start_new_session=True
        )
    assert created[0]._reaped
    assert created[0].returncode == -signal.SIGKILL


async def test_auto_reaping_policy_refuses_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    launches = []
    monkeypatch.setattr(process.signal, "getsignal", lambda _: signal.SIG_IGN)
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: launches.append(a))
    with pytest.raises(process.CommandCleanupError, match="SIGCHLD"):
        await process.spawn_owned(sys.executable, "-c", "pass", start_new_session=True)
    assert not launches


async def test_windows_bounded_cleanup_closes_handles_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt"))
    never = asyncio.Event()
    proc = SimpleNamespace(
        returncode=None, kill=Mock(), wait=never.wait, _transport=SimpleNamespace(close=Mock())
    )
    with pytest.raises(process.CommandCleanupError, match="reaping"):
        await process.stop_child(proc, cleanup_seconds=0.01)
    proc.kill.assert_called_once()
    proc._transport.close.assert_called_once()


async def test_default_run_repeated_cancel_cannot_skip_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children = []
    spawn, gather = process.spawn_owned, asyncio.gather

    async def capture(*args: Any, **kwargs: Any) -> process.ChildProcess:
        child = await spawn(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    def delayed_gather(*tasks: Any, **kwargs: Any) -> Any:
        if (
            kwargs.get("return_exceptions")
            and tasks
            and all(
                isinstance(task, asyncio.Task)
                and "run.<locals>.read" in task.get_coro().__qualname__
                for task in tasks
            )
        ):

            async def delay() -> list[Any]:
                draining.set()
                await release.wait()
                return await gather(*tasks, **kwargs)

            return delay()
        return gather(*tasks, **kwargs)

    monkeypatch.setattr(process, "spawn_owned", capture)
    monkeypatch.setattr(process.asyncio, "gather", delayed_gather)
    task = asyncio.create_task(process.run([sys.executable, "-c", "import time; time.sleep(5)"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(draining.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert children[0]._reaped
    finally:
        release.set()
        monkeypatch.setattr(process.asyncio, "gather", gather)
        await gather(task, return_exceptions=True)
        for child in children:
            await process.stop_child(child, cleanup_seconds=1)


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_history_close_cancellation_settles_child(cancellations: int) -> None:
    child = await process.spawn_owned(
        sys.executable,
        "-c",
        "import time; time.sleep(5)",
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert isinstance(child, process.OwnedChild)
    reader = HistoryReader("/unused/fixture")
    reader.proc = child
    reader.stderr_task = asyncio.create_task(reader._read_stderr())
    entered = asyncio.Event()
    original_wait = child.wait

    async def observed_wait() -> int:
        entered.set()
        return await original_wait()

    child.wait = observed_wait
    task = asyncio.create_task(reader.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert child._reaped
        assert reader.stderr_task.done()
    finally:
        child.wait = original_wait
        await process.stop_child(child, cleanup_seconds=1)
        await asyncio.gather(task, reader.stderr_task, return_exceptions=True)


class _StandIn:
    """Answers the names it was given; every other attribute is the real object's.

    For a module as ``process`` sees it: ``_StandIn(sys, platform="linux")`` changes the
    platform alone, so a product path that reads, say, ``sys.executable`` under the
    stand-in gets the real value and not an AttributeError.
    """

    def __init__(self, real: Any, **given: Any) -> None:
        self._real = real
        vars(self).update(given)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
async def signal_window(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[process.OwnedChild, Any]]:
    """Inert kernel boundary for the EPERM-before-waitid sequence in T-125's probe."""
    state = SimpleNamespace(
        code=None,
        report_later=True,
        leader_only=True,
        error=PermissionError,
        signals=[],
        peeks=[],
        snapshots=[],
        reaps=[],
    )
    child = SimpleNamespace(pid=123456789, returncode=None, stdin=None, stdout=None, stderr=None)
    proc = process.OwnedChild(child)
    loop = asyncio.get_running_loop()

    def peek(pid: int) -> int | None:
        assert pid == proc.pid
        state.peeks.append(state.code)
        return state.code

    def killpg(pid: int, sig: signal.Signals) -> None:
        assert pid == proc.pid
        assert not proc._reaped and not proc._lost
        state.signals.append(sig)
        if state.report_later:
            loop.call_soon(setattr, state, "code", 0)
        if state.error:
            raise state.error()

    def leader_only(pid: int) -> bool:
        assert pid == proc.pid and state.code is not None
        state.snapshots.append(pid)
        return state.leader_only

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        assert pid == proc.pid and options == os.WNOHANG
        assert state.code is not None
        state.reaps.append(pid)
        return pid, 0

    monkeypatch.setattr(process, "sys", _StandIn(sys, platform="darwin"))
    monkeypatch.setattr(process, "peek_exit", peek)
    monkeypatch.setattr(process.os, "killpg", killpg)
    monkeypatch.setattr(process.os, "waitpid", waitpid)
    monkeypatch.setattr(process, "darwin_only_retained_leader", leader_only)
    pending_before = set(process._pending_reaps)
    try:
        yield proc, state
    finally:
        state.code = 0
        await asyncio.gather(*(process._pending_reaps - pending_before))


async def test_signal_window_sys_stand_in_changes_only_the_platform(
    signal_window, monkeypatch
) -> None:
    """The fixture's ``sys`` answers ``platform`` as told and is the real ``sys`` otherwise.

    Guards the fixture, not the product: a stand-in that knows ``platform`` alone makes
    every test of the fixture fail with a bare AttributeError the day a cleanup path
    reads another attribute. Input form: ``process.sys`` as the fixture left it, then as
    test_signal_window_other_system_does_not_wait replaces it.
    """
    assert process.sys is not sys
    assert process.sys.platform == "darwin"
    assert process.sys.executable == sys.executable
    monkeypatch.setattr(process, "sys", _StandIn(sys, platform="linux"))
    assert process.sys.platform == "linux"
    assert process.sys.executable == sys.executable
    assert process.module_launch_argv("/fixture/config.json", "serve") == [
        sys.executable,
        "-P",
        "-m",
        "answerback_bridge",
        "--config",
        "/fixture/config.json",
        "serve",
    ]


@pytest.mark.parametrize("cleanup_seconds", [None, 1], ids=["term", "kill_only"])
async def test_signal_window_reaps_after_delayed_exit(signal_window, cleanup_seconds) -> None:
    """red_at_pretip: stop_child accepts the probe's delayed exit, then reaps.

    Kills wait-removed and second-look-removed; call_soon requires a live event loop.
    """
    proc, state = signal_window
    await process.stop_child(proc, cleanup_seconds=cleanup_seconds)
    assert proc._reaped and proc.returncode == 0
    assert state.reaps == [proc.pid]
    assert state.signals == (
        [signal.SIGTERM, signal.SIGKILL] if cleanup_seconds is None else [signal.SIGKILL]
    )
    assert state.snapshots


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_signal_window_preserves_run_error(signal_window, monkeypatch, ending) -> None:
    """red_at_pretip: run's caller cancellation/timeout survives the probe's window.

    Kills wait-removed and second-look-removed through run's real cleanup.
    """
    proc, _ = signal_window
    started = asyncio.Event()
    proc.stdout, proc.stderr = asyncio.StreamReader(), asyncio.StreamReader()

    async def spawn(*args: Any, **kwargs: Any) -> process.OwnedChild:
        started.set()
        return proc

    monkeypatch.setattr(process, "spawn_owned", spawn)
    task = asyncio.create_task(
        process.run(["inert-export"], max_seconds=0.01 if ending == "timeout" else 30)
    )
    await started.wait()
    if ending == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if ending == "cancel" else process.CommandTimeout):
        await task
    assert proc._reaped


@pytest.mark.parametrize("cleanup_seconds", [None, 0.03, 2], ids=["default", "short", "long"])
async def test_signal_window_never_reported_stays_bounded(signal_window, cleanup_seconds) -> None:
    """pins_existing: unconfirmed termination refuses with the original error.

    Kills bound-removed; a late fail-safe report prevents a mutant hanging pytest.
    """
    proc, state = signal_window
    state.report_later = False
    bound = 1 if cleanup_seconds is None else min(1, cleanup_seconds)
    loop = asyncio.get_running_loop()
    late = loop.call_later(bound + 0.2, setattr, state, "code", 0)
    started = loop.time()
    try:
        with pytest.raises(
            process.CommandCleanupError,
            match="^Command cleanup cannot confirm process-group termination$",
        ):
            await process.stop_child(proc, cleanup_seconds=cleanup_seconds)
        assert loop.time() - started < bound + 0.15
        assert not state.snapshots
    finally:
        late.cancel()


@pytest.mark.parametrize(
    ("cleanup_seconds", "expected"),
    [(None, 1001.0), (2, 1001.125), (0.25, 1000.25)],
    ids=["no_deadline", "later_deadline", "earlier_deadline"],
)
async def test_signal_window_report_wait_ends_one_second_after_eperm_or_at_an_earlier_deadline(
    signal_window, monkeypatch, cleanup_seconds, expected
) -> None:
    """pins_existing: the report wait's deadline is the EPERM's time plus exactly 1 second,
    or the cleanup deadline when that one is earlier.

    Fails for any other bound, 0.5 s and 1.1 s included, which the elapsed-time tests
    let through. Input form: ``process`` sees an asyncio whose loop clock is scripted,
    1000.0 and then 0.125 more at every reading (binary fractions, so the sums are
    exact), and whose ``timeout_at`` records the deadline it is handed. The first one
    handed over is the report wait's: without a cleanup deadline the EPERM reads
    1000.0; with one, _stop_owned reads 1000.0 for it and the EPERM reads 1000.125.
    The recorded wait then runs the same distance on the real loop; no test sleeps.
    """
    proc, _ = signal_window
    loop = asyncio.get_running_loop()
    readings: list[float] = []
    deadlines: list[float] = []

    def time() -> float:
        readings.append(1000.0 + 0.125 * len(readings))
        return readings[-1]

    def timeout_at(when: float) -> asyncio.Timeout:
        deadlines.append(when)
        return asyncio.timeout_at(loop.time() + when - readings[-1])

    scripted = _StandIn(
        asyncio, get_running_loop=lambda: _StandIn(loop, time=time), timeout_at=timeout_at
    )
    monkeypatch.setattr(process, "asyncio", scripted)
    await process.stop_child(proc, cleanup_seconds=cleanup_seconds)
    assert deadlines[0] == expected
    assert proc._reaped


@pytest.mark.parametrize("cleanup_seconds", [None, 1], ids=["term", "kill_only"])
@pytest.mark.parametrize("reported", [False, True], ids=["delayed", "already"])
async def test_signal_window_other_group_member_refuses(
    signal_window, reported, cleanup_seconds
) -> None:
    """pins_existing: EPERM with another member is never accepted.

    Every case fails if the product accepts EPERM without asking whether the leader is
    alone in its group. The delayed cases also fail if the product, once the report wait
    has seen the exit, returns without taking that look at the group, or takes the look
    and returns whatever it answered.
    """
    proc, state = signal_window
    state.code = 0 if reported else None
    state.leader_only = False
    with pytest.raises(
        process.CommandCleanupError,
        match="^Command cleanup cannot confirm process-group termination$",
    ):
        await process.stop_child(proc, cleanup_seconds=cleanup_seconds)
    assert state.signals == [signal.SIGTERM if cleanup_seconds is None else signal.SIGKILL]


async def test_signal_window_reap_during_report_wait_refuses(signal_window, monkeypatch) -> None:
    """red_at_pretip: another owned reap during the report wait releases group authority.

    Input form: the product's _reap_ready path runs from another loop callback.
    Fails, on the KILL-only path, if the product drops its check of _reaped after the
    report wait and goes on to look at the process group of a child it has reaped.
    """
    proc, state = signal_window
    state.report_later = False
    loop = asyncio.get_running_loop()
    original_killpg = process.os.killpg

    def reap() -> None:
        state.code = 0
        assert proc._reap_ready()

    def killpg(pid: int, sig: signal.Signals) -> None:
        loop.call_soon(reap)
        original_killpg(pid, sig)

    monkeypatch.setattr(process.os, "killpg", killpg)
    with pytest.raises(
        process.CommandCleanupError,
        match="^Child ownership released; refusing process-group signals$",
    ):
        await process.stop_child(proc, cleanup_seconds=1)
    assert state.snapshots == []
    assert state.signals == [signal.SIGKILL]


async def test_signal_window_other_system_does_not_wait(signal_window, monkeypatch) -> None:
    """pins_existing: non-macOS EPERM makes only the existing immediate second look.

    Kills wait-on-every-system; teardown's reap check is counted separately.
    """
    proc, state = signal_window
    monkeypatch.setattr(process, "sys", _StandIn(sys, platform="linux"))
    with pytest.raises(
        process.CommandCleanupError,
        match="^Command cleanup cannot confirm process-group termination$",
    ):
        await process.stop_child(proc)
    assert state.peeks[:3] == [None, None, None]
    assert not state.snapshots
    assert state.signals == [signal.SIGTERM]


async def test_signal_window_already_reported_is_accepted(signal_window) -> None:
    """pins_existing: the probe's already-waitable leader remains accepted.

    Guard against changing the existing immediate EPERM rule; no wait mutant applies.
    """
    proc, state = signal_window
    state.code = 0
    await process.stop_child(proc)
    assert proc._reaped
    assert state.signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("error", [None, ProcessLookupError], ids=["sent", "empty_group"])
async def test_signal_window_unopposed_signal_is_unchanged(signal_window, error) -> None:
    """pins_existing: successful signals and missing groups still complete cleanup.

    Control for the EPERM mutants; none should alter these outcomes.
    """
    proc, state = signal_window
    state.error = error
    await process.stop_child(proc)
    assert proc._reaped
    assert not state.snapshots


async def test_signal_window_shares_completion_deadline(signal_window) -> None:
    """red_at_pretip: explicit cleanup includes the report wait and pipe completion.

    Kills a restarted-completion-budget mutant, using an unfinished output pipe.
    """
    proc, state = signal_window
    state.report_later = False
    loop = asyncio.get_running_loop()
    closed = loop.create_future()
    proc._read_closed.append(closed)
    report = loop.call_later(0.4, setattr, state, "code", 0)
    started = loop.time()
    try:
        with pytest.raises(
            process.CommandCleanupError,
            match="^Command cleanup failed to confirm owned child reaping$",
        ):
            await process.stop_child(proc, cleanup_seconds=0.5)
        assert loop.time() - started < 0.8
        assert proc._reaped
    finally:
        report.cancel()
        closed.set_result(None)
