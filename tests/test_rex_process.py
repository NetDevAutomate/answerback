"""Real disposable child cleanup; no installed provider or foreign process."""

import asyncio
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias

import pytest

from answerback_bridge import process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Rex uses a local Unix endpoint")
OwnedProcesses: TypeAlias = list[tuple[process.ChildProcess, Callable[[], Awaitable[int]]]]

CHILD = """
import os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    time.sleep(1)
"""


async def ready(path: Path) -> None:
    def wait() -> None:
        deadline = time.monotonic() + 3
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned test child did not signal readiness")
            time.sleep(0.01)

    await asyncio.to_thread(wait)


@pytest.fixture
def owned_processes(monkeypatch: pytest.MonkeyPatch) -> OwnedProcesses:
    """Observe real launches and retain original wait for fail-safe test cleanup."""
    created: OwnedProcesses = []
    launch = process.spawn_owned

    async def capture(*args: Any, **kwargs: Any) -> process.ChildProcess:
        proc = await launch(*args, **kwargs)
        created.append((proc, proc.wait))
        return proc

    monkeypatch.setattr(process, "spawn_owned", capture)
    return created


async def reap_created(created: OwnedProcesses) -> None:
    for proc, wait in created:
        proc.wait = wait
        try:
            await asyncio.wait_for(process.stop_child(proc, cleanup_seconds=1), 3)
        except process.CommandCleanupError:
            # An earlier timeout receipt remains failed; actual wait/reap is independent.
            assert isinstance(proc, process.OwnedChild)
            assert proc._reap_ready()


async def test_bounded_timeout_kills_and_reaps_real_child(
    tmp_path: Path, owned_processes: OwnedProcesses
) -> None:
    marker = tmp_path / "child-ready"
    task = asyncio.create_task(
        process.run(
            [sys.executable, "-c", CHILD, str(marker)],
            max_seconds=0.5,
            cleanup_seconds=1,
        )
    )
    try:
        await ready(marker)
        with pytest.raises(process.CommandTimeout, match="delivery may be uncertain"):
            await asyncio.wait_for(task, 3)
        proc, _ = owned_processes[0]
        assert proc.returncode == -signal.SIGKILL
        assert proc.stdout is not None and proc.stdout.at_eof()
        assert proc.stderr is not None and proc.stderr.at_eof()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reap_created(owned_processes)


async def test_bounded_cancellation_kills_and_reaps_real_child(
    tmp_path: Path, owned_processes: OwnedProcesses
) -> None:
    marker = tmp_path / "child-ready"
    task = asyncio.create_task(
        process.run([sys.executable, "-c", CHILD, str(marker)], cleanup_seconds=1)
    )
    try:
        await ready(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        proc, _ = owned_processes[0]
        assert proc.returncode == -signal.SIGKILL
        assert proc.stdout is not None and proc.stdout.at_eof()
        assert proc.stderr is not None and proc.stderr.at_eof()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reap_created(owned_processes)


async def test_repeated_cancellation_waits_for_owned_child_cleanup(
    tmp_path: Path, owned_processes: OwnedProcesses
) -> None:
    marker = tmp_path / "child-ready"
    task = asyncio.create_task(
        process.run([sys.executable, "-c", CHILD, str(marker)], cleanup_seconds=1)
    )
    waiting, release = asyncio.Event(), asyncio.Event()
    try:
        await ready(marker)
        proc, real_wait = owned_processes[0]

        async def controlled_wait() -> int:
            waiting.set()
            await release.wait()
            return await real_wait()

        # Delay wait completion at the asyncio process boundary, after a real
        # kill. Repeated cancellations must not abandon that owned cleanup task.
        proc.wait = controlled_wait
        task.cancel()
        await asyncio.wait_for(waiting.wait(), 3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert proc.returncode == -signal.SIGKILL
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reap_created(owned_processes)


@pytest.mark.parametrize("through_run", [False, True])
async def test_cleanup_budget_exhaustion_is_explicit(
    tmp_path: Path, owned_processes: OwnedProcesses, through_run: bool
) -> None:
    marker = tmp_path / "child-ready"
    waiting = asyncio.Event()
    never = asyncio.Event()
    task = None
    if through_run:
        task = asyncio.create_task(
            process.run([sys.executable, "-c", CHILD, str(marker)], cleanup_seconds=0.05)
        )
    else:
        await process.spawn_owned(
            sys.executable,
            "-c",
            CHILD,
            str(marker),
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        await ready(marker)
        proc, _ = owned_processes[0]

        async def unavailable_wait() -> int:
            waiting.set()
            await never.wait()
            raise AssertionError("The controlled wait must expire")

        proc.wait = unavailable_wait
        if through_run:
            task.cancel()
        else:
            task = asyncio.create_task(process.stop_child(proc, cleanup_seconds=0.05))
        await asyncio.wait_for(waiting.wait(), 3)
        with pytest.raises(process.CommandCleanupError, match="reaping"):
            await asyncio.wait_for(task, 3)
        assert proc.stdout is not None
        assert proc.stderr is not None
        # Budget exhaustion closes the actual owned pipes, independently of
        # the blocked wait seam; EOF proves there are no retained handles.
        assert await asyncio.wait_for(proc.stdout.read(), 3) == b""
        assert await asyncio.wait_for(proc.stderr.read(), 3) == b""
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await reap_created(owned_processes)


@pytest.mark.parametrize("cleanup_seconds", [None, 1])
async def test_stop_child_cleans_owned_process_group(
    tmp_path: Path, owned_processes: OwnedProcesses, cleanup_seconds: float | None
) -> None:
    parent_marker, child_marker = tmp_path / "parent-ready", tmp_path / "child-ready"
    parent = """
import pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[3]])
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
while True:
    time.sleep(1)
"""
    proc = await process.spawn_owned(
        sys.executable,
        "-c",
        parent,
        CHILD,
        str(parent_marker),
        str(child_marker),
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await ready(parent_marker)
        await ready(child_marker)
        assert parent_marker.read_text() == child_marker.read_text()
        await asyncio.wait_for(process.stop_child(proc, cleanup_seconds=cleanup_seconds), 3)
        assert proc.returncode == -signal.SIGKILL
        assert proc.stdout is not None and proc.stderr is not None
        # The grandchild inherits both pipe writers. EOF cannot arrive if it
        # survives group cleanup, even after the direct child has been reaped.
        assert await asyncio.wait_for(proc.stdout.read(), 3) == b""
        assert await asyncio.wait_for(proc.stderr.read(), 3) == b""
    finally:
        await reap_created(owned_processes)


async def test_default_cleanup_preserves_term_grace(
    tmp_path: Path, owned_processes: OwnedProcesses
) -> None:
    marker, terminated = tmp_path / "child-ready", tmp_path / "term-observed"
    child = """
import pathlib, signal, sys, time
def stopped(signum, frame):
    pathlib.Path(sys.argv[2]).write_text("SIGTERM")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stopped)
pathlib.Path(sys.argv[1]).write_text("ready")
while True:
    time.sleep(1)
"""
    proc = await process.spawn_owned(
        sys.executable,
        "-c",
        child,
        str(marker),
        str(terminated),
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await ready(marker)
        await asyncio.wait_for(process.stop_child(proc), 3)
        assert terminated.read_text() == "SIGTERM"
        assert proc.returncode == 0
    finally:
        await reap_created(owned_processes)
