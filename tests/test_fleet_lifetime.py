"""fleet: every descriptor the publisher opens is closed once, and only its own."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import gc
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_publish import _doc

import answerback_bridge
from answerback_bridge.fleet import publish


def _open_descriptors() -> set[int]:
    """The descriptors open in this process now: only those still open when the listing ended.

    The listing of ``/dev/fd`` names its own descriptor, which is closed when the listing ends;
    that number is free again and the next file opened gets it, so it is left out.
    An object an earlier test left in a reference cycle may own a descriptor (a socket, a
    file); it is collected first, so that it cannot be closed between two listings. The
    publisher's own descriptors are plain numbers, which no collection closes.
    """
    gc.collect()
    return set(_still_open([int(name) for name in os.listdir("/dev/fd")]))


def _open_files() -> dict[int, tuple[int, int]]:
    """Each open descriptor with the device and inode of its open file."""
    files = {}
    for fd in _open_descriptors():
        with contextlib.suppress(OSError):
            info = os.fstat(fd)
            files[fd] = (info.st_dev, info.st_ino)
    return files


@contextlib.contextmanager
def no_descriptor_left() -> Iterator[None]:
    """Fail when a descriptor is open after the block that was not open before on that file.

    The one check around every fleet test that opens a publisher: each such file enters it in
    an ``autouse`` fixture. Garbage is collected before each listing.
    """
    before = _open_files()
    yield
    after = _open_files()
    left = sorted(fd for fd, key in after.items() if before.get(fd) != key)
    if left:
        files = ", ".join(f"{fd}: device {after[fd][0]} inode {after[fd][1]}" for fd in left)
        raise AssertionError(f"descriptors left open by the test: {left} ({files})")


def lock_state(stage: Path) -> str:
    """How the writer lock looks to another open of the lock file.

    One of "exclusive", "shared" or "free".
    """
    try:
        fd = os.open(stage / "writer.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "free"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "exclusive"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "shared"
        return "free"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)  # only what this descriptor took
        finally:
            os.close(fd)  # also when giving the lock back raised


def test_the_lock_state_closes_its_descriptor_when_giving_the_lock_back_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "writer.lock").touch()
    real = fcntl.flock

    def flock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "injected error at the unlock")
        real(fd, operation)

    before = _open_descriptors()
    with monkeypatch.context() as m:
        m.setattr(fcntl, "flock", flock)
        with pytest.raises(OSError, match="injected error at the unlock"):
            lock_state(tmp_path)
    assert _open_descriptors() == before


def test_the_lock_is_free_without_a_lock_file_and_with_one_nobody_locks(tmp_path: Path) -> None:
    assert lock_state(tmp_path) == "free"
    (tmp_path / "writer.lock").touch()
    assert lock_state(tmp_path) == "free"


@pytest.mark.parametrize(("how", "seen"), [(fcntl.LOCK_EX, "exclusive"), (fcntl.LOCK_SH, "shared")])
def test_the_lock_state_names_a_lock_another_descriptor_holds_and_leaves_it_held(
    tmp_path: Path, how: int, seen: str
) -> None:
    lock = tmp_path / "writer.lock"
    lock.touch()
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, how | fcntl.LOCK_NB)
        assert lock_state(tmp_path) == seen
        assert lock_state(tmp_path) == seen  # the helper released nothing of the holder
        third = os.open(lock, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(third, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(third)
    finally:
        os.close(holder)
    assert lock_state(tmp_path) == "free"


# The names a child process of a fleet test takes from the test process, each when it is set.
CHILD_ENVIRONMENT = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
)


def child_environment() -> dict[str, str]:
    """The environment of a child process of a fleet test: made, not inherited.

    It holds only the names of ``CHILD_ENVIRONMENT`` that the test process has, and
    ``PYTHON_COLORS=0``. The source folder of the package the tests import comes first on
    ``PYTHONPATH``, so that the child imports the same copy. A setting that makes the
    interpreter print (allocator figures, import times, verbose imports, colours) does not
    reach the child, and neither does one that makes a run stricter (``PYTHONWARNINGS``,
    ``PYTHONDEVMODE``, ``PYTHONFAULTHANDLER``): a child runs with the interpreter's defaults,
    whatever the test process was started with.
    """
    env = {name: os.environ[name] for name in CHILD_ENVIRONMENT if name in os.environ}
    package = str(Path(answerback_bridge.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([package, *filter(None, [env.get("PYTHONPATH")])])
    env["PYTHON_COLORS"] = "0"
    return env


def _tail(path: Path, size: int = 2000) -> str:
    data = path.read_bytes() if path.exists() else b""
    return data[-size:].decode("utf-8", errors="replace")


class Child:
    """A child process of a fleet test, with the environment of ``child_environment``.

    Its standard output and standard error go into two files under ``folder``, never into a
    pipe, so that a child that writes much never waits for the test to read. The files are
    read after the child has ended. A child that outlives its time limit is killed, and the
    failure names the end of both files. Use it in a ``with`` block: a child still running at
    its end is killed.
    """

    def __init__(self, args: list[str], folder: Path, *, name: str = "child", **more: Any) -> None:
        self.out_path = folder / f"{name}.out"
        self.err_path = folder / f"{name}.err"
        with self.out_path.open("wb") as out, self.err_path.open("wb") as err:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=child_environment(),
                **more,
            )

    def __enter__(self) -> Child:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def send_signal(self, signum: int) -> None:
        self.proc.send_signal(signum)

    def running(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float) -> int:
        """The exit code; a child still running after ``timeout`` seconds is killed."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(
                f"the child ran longer than {timeout:g} s and was killed;"
                f" the end of its standard output: {_tail(self.out_path)!r};"
                f" the end of its standard error: {_tail(self.err_path)!r}"
            ) from None

    @property
    def stdout(self) -> str:
        return self.out_path.read_text(encoding="utf-8", errors="replace")

    @property
    def stderr(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")


def test_a_child_gets_a_made_environment_whatever_the_test_process_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printing = (
        "PYTHONMALLOCSTATS",
        "PYTHONPROFILEIMPORTTIME",
        "PYTHONVERBOSE",
        "PYTHONWARNDEFAULTENCODING",
        "FORCE_COLOR",
    )
    for name in printing:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("PYTHONWARNINGS", "error")
    env = child_environment()
    assert not set(printing) & set(env)
    assert env["PYTHON_COLORS"] == "0"
    python = {name for name in env if name.startswith("PYTHON")}
    assert python <= {name for name in CHILD_ENVIRONMENT if name.startswith("PYTHON")} | {
        "PYTHON_COLORS"
    }
    assert set(env) <= set(CHILD_ENVIRONMENT) | {"PYTHON_COLORS"}


def test_a_child_that_outlives_its_limit_is_killed_and_its_output_is_named(
    tmp_path: Path,
) -> None:
    program = (
        "import sys, time\n"
        "print('out-mark', flush=True)\n"
        "print('err-mark', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    with Child([sys.executable, "-c", program], tmp_path) as child:
        until = time.monotonic() + 20
        while "err-mark" not in child.stderr and time.monotonic() < until:
            time.sleep(0.05)
        with pytest.raises(AssertionError) as failed:
            child.wait(0.5)
        assert not child.running()
    assert "out-mark" in str(failed.value) and "err-mark" in str(failed.value)


def _umask_now() -> int:
    old = os.umask(0)
    os.umask(old)
    return old


_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)


def _process_state() -> dict[str, Any]:
    return {
        "umask": _umask_now(),
        "working directory": os.getcwd(),
        **{f"handler of {sig.name}": signal.getsignal(sig) for sig in _SIGNALS},
        "real-time interval timer": signal.getitimer(signal.ITIMER_REAL),
        "blocked signals": signal.pthread_sigmask(signal.SIG_BLOCK, []),
    }


def _put_back(what: str, value: Any) -> None:
    if what == "umask":
        os.umask(value)
    elif what == "working directory":
        os.chdir(value)
    elif what == "real-time interval timer":
        signal.setitimer(signal.ITIMER_REAL, *value)
    elif what == "blocked signals":
        signal.pthread_sigmask(signal.SIG_SETMASK, value)
    else:
        sig = next(s for s in _SIGNALS if what == f"handler of {s.name}")
        signal.signal(sig, signal.SIG_DFL if value is None else value)


def _shown(what: str, value: Any) -> str:
    return oct(value) if what == "umask" else repr(value)


@contextlib.contextmanager
def process_unchanged() -> Iterator[None]:
    """Fail when the body leaves the umask, the working directory, a handler or a timer changed.

    The handlers are those of SIGINT, SIGTERM and SIGALRM; the timer is the real-time one; the
    set of signals blocked for this thread is compared too. What
    changed is put back first, so that one failure does not fail every test after it.
    """
    before = _process_state()
    yield
    after = _process_state()
    changed = [what for what in before if after[what] != before[what]]
    for what in changed:
        _put_back(what, before[what])
    if changed:
        raise AssertionError(
            "the test left the process changed: "
            + "; ".join(
                f"{what} from {_shown(what, before[what])} to {_shown(what, after[what])}"
                for what in changed
            )
        )


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


def test_the_process_guard_puts_back_and_names_a_changed_umask() -> None:
    before = _umask_now()
    with pytest.raises(AssertionError) as failed, process_unchanged():
        os.umask(before ^ 0o007)
    assert str(failed.value) == (
        f"the test left the process changed: umask from {oct(before)} to {oct(before ^ 0o007)}"
    )
    assert _umask_now() == before


def test_the_process_guard_puts_back_and_names_a_changed_working_directory(
    tmp_path: Path,
) -> None:
    before = os.getcwd()
    with pytest.raises(AssertionError) as failed, process_unchanged():
        os.chdir(tmp_path)
    assert str(failed.value) == (
        "the test left the process changed: working directory"
        f" from {before!r} to {os.fspath(tmp_path)!r}"
    )
    assert os.getcwd() == before


def test_the_process_guard_puts_back_and_names_a_replaced_signal_handler() -> None:
    before = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, frame: Any) -> None:
        pass

    with pytest.raises(AssertionError) as failed, process_unchanged():
        signal.signal(signal.SIGTERM, handler)
    assert str(failed.value) == (
        f"the test left the process changed: handler of SIGTERM from {before!r} to {handler!r}"
    )
    assert signal.getsignal(signal.SIGTERM) == before


def test_the_process_guard_puts_back_and_names_a_running_timer() -> None:
    before = signal.getitimer(signal.ITIMER_REAL)
    with pytest.raises(AssertionError) as failed, process_unchanged():
        signal.setitimer(signal.ITIMER_REAL, 300)
    assert signal.getitimer(signal.ITIMER_REAL) == before == (0.0, 0.0)
    assert str(failed.value).startswith(
        "the test left the process changed: real-time interval timer from (0.0, 0.0) to (29"
    )


def test_the_process_guard_puts_back_and_names_a_blocked_signal() -> None:
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    assert signal.SIGTERM not in before
    with pytest.raises(AssertionError) as failed, process_unchanged():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    assert str(failed.value).startswith("the test left the process changed: blocked signals from ")
    assert "SIGTERM" in str(failed.value)
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before


def test_the_process_guard_passes_when_the_body_puts_each_back(tmp_path: Path) -> None:
    with process_unchanged():
        old = os.umask(0o077)
        os.umask(old)
        here = os.getcwd()
        os.chdir(tmp_path)
        os.chdir(here)
        handler = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, handler)
        signal.setitimer(signal.ITIMER_REAL, 300)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


def test_the_check_names_exactly_a_descriptor_left_open(tmp_path: Path) -> None:
    with pytest.raises(AssertionError) as failed, no_descriptor_left():
        left = os.open(tmp_path, os.O_RDONLY)
    try:
        info = os.stat(tmp_path)
        assert str(failed.value) == (
            f"descriptors left open by the test: [{left}]"
            f" ({left}: device {info.st_dev} inode {info.st_ino})"
        )
    finally:
        os.close(left)


def test_the_check_passes_when_every_descriptor_is_closed_again(tmp_path: Path) -> None:
    with no_descriptor_left():
        os.close(os.open(tmp_path, os.O_RDONLY))
        p = publish.Publisher(tmp_path / "fleet")
        p.open()
        p.close()


def test_the_check_names_a_number_that_now_belongs_to_another_file(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = os.open(tmp_path / "a", os.O_RDONLY)
    try:
        with pytest.raises(AssertionError) as failed, no_descriptor_left():
            os.close(first)
            again = os.open(tmp_path / "b", os.O_RDONLY)  # the lowest free number: ``first``
        assert again == first
        info = os.stat(tmp_path / "b")
        assert str(failed.value) == (
            f"descriptors left open by the test: [{first}]"
            f" ({first}: device {info.st_dev} inode {info.st_ino})"
        )
    finally:
        os.close(first)


def _folders(tmp_path: Path) -> None:
    for name in ("fleet", "fleet.stage"):
        (tmp_path / name).mkdir()


def _own_descriptors(tmp_path: Path, count: int) -> list[int]:
    """Descriptors of the test itself: each takes the lowest number that is free."""
    return [os.open(tmp_path, os.O_RDONLY) for _ in range(count)]


def _still_open(fds: list[int]) -> list[int]:
    alive = []
    for fd in fds:
        try:
            os.fstat(fd)
        except OSError:
            continue
        alive.append(fd)
    return alive


def _file_of(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def test_the_listing_of_open_descriptors_names_only_open_ones(tmp_path: Path) -> None:
    snapshot = _open_descriptors()
    for fd in snapshot:
        os.fstat(fd)  # raises for a number that is not open
    mine = os.open(tmp_path, os.O_RDONLY)
    try:
        # The raw listing would hold the number of its own descriptor, which this open gets.
        assert mine not in snapshot
    finally:
        os.close(mine)


def test_a_descriptor_opened_between_two_listings_is_their_difference(tmp_path: Path) -> None:
    first = _open_descriptors()
    mine = os.open(tmp_path, os.O_RDONLY)
    try:
        second = _open_descriptors()
        assert second - first == {mine}
        assert first - second == set()
        assert _file_of(mine) == (os.stat(tmp_path).st_dev, os.stat(tmp_path).st_ino)
    finally:
        os.close(mine)
    assert _open_descriptors() - first == set()
    assert _open_descriptors() == first


def test_repeated_publications_that_succeed_or_fail_leave_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    failing = {"on": False}

    def fsync(fd: int) -> None:
        if failing["on"] and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected I/O error at the sync of the staged file")
        real_fsync(fd)

    folder = tmp_path / "fleet"
    p = publish.Publisher(folder)
    p.open()
    try:
        p.publish(_doc(0))
        before = _open_descriptors()
        monkeypatch.setattr(publish.os, "fsync", fsync)
        for n in range(1, 26):
            failing["on"] = False
            p.publish(_doc(n))
            failing["on"] = True
            with pytest.raises(publish.PublishError, match="injected"):
                p.publish(_doc(100 + n))
            assert json.loads((folder / "fleet.json").read_bytes())["written_at"] == 1000.0 + n
        after = _open_descriptors()
        failing["on"] = False
        p.publish(_doc(99))
    finally:
        p.close()
    assert after == before
    assert json.loads((folder / "fleet.json").read_bytes())["written_at"] == 1099.0


def test_a_second_close_closes_no_descriptor_that_belongs_to_someone_else(
    tmp_path: Path,
) -> None:
    p = publish.Publisher(tmp_path / "fleet")
    p.open()
    p.close()
    mine = _own_descriptors(tmp_path, 3)  # they take the numbers the publisher had
    try:
        p.close()
        assert _still_open(mine) == mine
    finally:
        for fd in _still_open(mine):
            os.close(fd)


def test_close_after_a_start_that_failed_with_the_lock_taken_closes_nothing_of_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders(tmp_path)
    real_fchmod = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        # With both folders there before, a folder's mode is set only after the lock.
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected I/O error at the mode of a folder")
        real_fchmod(fd, mode)

    p = publish.Publisher(tmp_path / "fleet")
    with monkeypatch.context() as m:
        m.setattr(publish.os, "fchmod", fchmod)
        with pytest.raises(OSError, match="injected"):
            p.open()
    mine = _own_descriptors(tmp_path, 3)
    try:
        p.close()
        assert _still_open(mine) == mine
    finally:
        for fd in _still_open(mine):
            os.close(fd)


def test_an_interrupt_at_the_lock_step_closes_every_descriptor_and_is_passed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(fd: int, operation: int) -> None:
        raise KeyboardInterrupt

    before = _open_descriptors()
    monkeypatch.setattr(publish.fcntl, "flock", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish.Publisher(tmp_path / "fleet").open()
    assert _open_descriptors() == before


def test_a_refused_start_with_both_folders_there_before_leaves_no_descriptor_open(
    tmp_path: Path,
) -> None:
    _folders(tmp_path)
    (tmp_path / "fleet.stage" / "notes.txt").write_text("not the writer's", encoding="utf-8")
    before = _open_descriptors()
    with pytest.raises(publish.PublishRefused, match="notes.txt"):
        publish.Publisher(tmp_path / "fleet").open()
    assert _open_descriptors() == before


def test_a_refused_start_that_made_both_folders_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After both folders are made, the only refusal there is two file systems; no test can
    # put the folders on two of them, so ``_device`` reports two for this one test.
    devices = iter(range(100))
    monkeypatch.setattr(publish, "_device", lambda fd: next(devices))
    before = _open_descriptors()
    with pytest.raises(publish.PublishRefused, match="one file system"):
        publish.Publisher(tmp_path / "fleet").open()
    assert _open_descriptors() == before
    assert sorted(os.listdir(tmp_path)) == ["fleet", "fleet.stage"]


CHILD = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    from answerback_bridge.fleet import publish

    os.close(0)
    p = publish.Publisher(Path(sys.argv[1]) / "fleet")
    p.open()
    try:
        import stat
        got_zero = stat.S_ISDIR(os.fstat(0).st_mode)
    except OSError:
        got_zero = False
    p.close()
    try:
        os.fstat(0)
        zero_open_after = True
    except OSError:
        zero_open_after = False
    print(json.dumps({"got_zero": got_zero, "zero_open_after": zero_open_after}))
    """
)


def test_a_publisher_folder_on_descriptor_zero_is_closed_by_close(tmp_path: Path) -> None:
    # Standard input of the child is the null device: descriptor 0 is open in it, whatever
    # is here, and the child closes it itself.
    with Child([sys.executable, "-c", CHILD, str(tmp_path)], tmp_path) as child:
        code = child.wait(30)
    assert code == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["got_zero"], "no folder of the publisher got descriptor 0 in the child"
    assert result["zero_open_after"] is False, "descriptor 0 stayed open after close()"
