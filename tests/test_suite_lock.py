"""The suite lock, tested with real processes and a lock file of the test's own.

Every test names its lock file through ANSWERBACK_SUITE_LOCK, so none touches the lock
of the machine, which the run that executes this file may itself hold. Each test waits
for a line that a process wrote, for a pipe to close or for a process to end, and no
test asks that something happens within a short time. LIMIT bounds every such wait. It
ends a test of a broken tool, and it is a bound on the machine as well: a machine that
is suspended, or loaded so that a step takes longer than LIMIT, fails a test here, and
no test can promise otherwise. Where the tool itself acts after a time (its reports,
and what it sends to a command that ignores a signal), the test runs it with that time
made short and waits for the act.

Labels (stage contract). Tests of the first round are `red_at_pretip` against 5c135ad,
where tools/suite_lock.py does not exist. Tests of fix round 1 carry "fix 1" in their
label: `red_at_pretip` there means red at 607e026, the tip that the review read, and
`pins_existing` means green there on purpose. Tests of fix round 2 carry "fix 2":
`red_at_pretip` there means red at c6b5a31, the tip that the second review read. Tests
of fix round 3 carry "fix 3": `red_at_pretip` there means red at 71b0f30, the tip that
the third review read. Tests of fix round 3b carry "fix 3b": `red_at_pretip` there means
red at e3deb4c. Their inputs come from the reviewer's scripts (in/adversarial.py,
in/extra_checks.py, in/fork_checks.py, in/attack_fixes.py, in/more_attacks.py and
in/review3-scratch/deep_attacks.py of the stage folder) and from the three Linux runs;
each test names its case.

Every test runs under umask 022 (the fixture `umask_of_the_tests`): the umask of the
caller is no input of this file.
"""

from __future__ import annotations

import ast
import fcntl
import importlib.util
import json
import os
import pwd
import re
import resource
import select
import shlex
import signal
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "suite_lock.py"
# The interpreter that runs the tool. Set this to /usr/bin/python3 on macOS to run every
# test against the system Python 3.9.
PYTHON = os.environ.get("ANSWERBACK_SUITE_LOCK_TEST_PYTHON", sys.executable)
PATH_ENV = "ANSWERBACK_SUITE_LOCK"
HELD_ENV = "ANSWERBACK_SUITE_LOCK_HELD"
BUSY = 75
USAGE = 64
NO_LOCK_FILE = 73
NO_DESCRIPTOR = 76
NOT_HELD = 77
LIMIT = 30.0

# The first lines of every program of the tests that SIGQUIT can end with its default
# action (fix 8): on Linux the program makes itself not dumpable, after its last exec
# (which sets the attribute to 1 again) and before it says that it is ready. A call that
# fails ends the program with an error. See default_signals().
NOT_DUMPABLE = """\
import ctypes
import os
import sys

if sys.platform.startswith("linux"):
    PR_SET_DUMPABLE = 4  # linux/prctl.h
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, "prctl(PR_SET_DUMPABLE, 0): " + os.strerror(number))
"""

# The wrapped command of most tests: it records its start, says so, and runs until its
# standard input closes. The log is opened for append, so its order is the real order.
HOLD = (
    NOT_DUMPABLE
    + """\
import os
import sys

log, label = sys.argv[1], sys.argv[2]


def note(text):
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, (text + "\\n").encode())
    os.close(fd)


note("start " + label)
sys.stdout.write("started\\n")
sys.stdout.flush()
sys.stdin.read()
note("end " + label)
"""
)


class Wrapped:
    """One `suite_lock.py run` process whose output the test reads line by line."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self._pending = {"stdout": b"", "stderr": b""}

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self, name: str) -> bytes:
        fd = getattr(self.proc, name).fileno()
        ready, _, _ = select.select([fd], [], [], LIMIT)
        if not ready:
            pytest.fail(f"nothing on {name} within {LIMIT} s; unread: {self._pending[name]!r}")
        return os.read(fd, 4096)

    def line(self, name: str) -> str:
        while b"\n" not in self._pending[name]:
            chunk = self._read(name)
            if not chunk:
                pytest.fail(f"{name} closed before a line; unread: {self._pending[name]!r}")
            self._pending[name] += chunk
        line, _, self._pending[name] = self._pending[name].partition(b"\n")
        return line.decode()

    def line_with(self, name: str, text: str) -> str:
        while True:
            line = self.line(name)
            if text in line:
                return line

    def rest(self, name: str) -> str:
        """Read to the end: it comes only when every writer of the pipe has gone."""
        while True:
            chunk = self._read(name)
            if not chunk:
                break
            self._pending[name] += chunk
        text, self._pending[name] = self._pending[name].decode(), b""
        return text

    def so_far(self, name: str) -> tuple[str, bool]:
        """What is written by now, and whether every writer of the pipe has gone. It
        does not wait: after the wrapper has ended, all that it wrote is here."""
        fd = getattr(self.proc, name).fileno()
        ended = False
        while not ended and select.select([fd], [], [], 0)[0]:
            chunk = os.read(fd, 4096)
            ended = not chunk
            self._pending[name] += chunk
        text, self._pending[name] = self._pending[name].decode(), b""
        return text, ended

    def release(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def wait(self) -> int:
        return self.proc.wait(timeout=LIMIT)


@pytest.fixture(autouse=True)
def umask_of_the_tests() -> Iterator[None]:
    """Every test runs under umask 022, whatever the caller's is (fix 3b, finding 1 of
    in/P6-linux-3.md). A folder that a test makes with `Path.mkdir` gets its mode from
    the umask: 775 under umask 002, which the account on the Linux node that was tested
    has (a LAN Linux node), and the tool refuses a lock folder that its group can write. The
    umask was a hidden input of the tests.

    A tool that a test starts as a child process inherits this umask. That is the one
    the tests need: what the tool makes must not depend on it, and the test below that
    starts the tool under 002, 022 and 077 pins that.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    # The run makes the folder `state`, for this user alone. What is above it is not
    # asked (fix 3), so pytest's temporary folder can be anywhere.
    return tmp_path / "state" / "suite.lock"


@pytest.fixture
def env(lock: Path) -> dict[str, str]:
    environ = {key: value for key, value in os.environ.items() if key != HELD_ENV}
    environ[PATH_ENV] = str(lock)
    return environ


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "events.log"


@pytest.fixture
def hold(tmp_path: Path, log: Path) -> Callable[[str], list[str]]:
    script = tmp_path / "hold.py"
    script.write_text(HOLD)
    return lambda label: [sys.executable, str(script), str(log), label]


def default_signals() -> None:
    """Runs in the new process before the tool starts. A shell starts a background job
    with SIGINT and SIGQUIT ignored, and `nohup` ignores SIGHUP; a process inherits
    that, and the tool passes on no signal that it was told to ignore. The tests send
    these signals, so they give the tool the dispositions of a foreground command.

    The soft core limit is set too (fix 6, fix 7), the hard limit kept. SIGQUIT, which the
    tests send, ends a process with a core dump by default (signal(7)). This machine's
    core_pattern is a pipe to systemd-coredump, and core(5) says that RLIMIT_CORE is not
    enforced for a piped dump: with a limit of 0 the kernel still starts the collector for
    every such process, which then stores nothing. For a limit of exactly 1 this kernel
    (Linux 7.0) ends the dump before it starts the collector and logs
    "coredump: <pid>(python): RLIMIT_CORE is set to 1, aborting core". Where the pattern is a
    file name, getrlimit(2) says that a nonzero limit truncates the core file to that size, so
    a limit of 1 allows a file of at most one byte. On every other system the value is 0, the
    value that asks for no core file. A soft limit above the hard one is refused, so a hard
    limit below the value gives the hard limit. A limit is inherited across fork and exec, so
    the wrapper, its command and every process below them get it.

    Under a hard limit of 0 the soft limit is 0 as well, and the collector starts again (fix
    8, R6-C). So on Linux the programs that SIGQUIT ends (HOLD and TREE) also make themselves
    not dumpable, with prctl(PR_SET_DUMPABLE, 0) (NOT_DUMPABLE): PR_SET_DUMPABLE(2const) says
    that the attribute "determines whether core dumps are produced for the calling process
    upon delivery of a signal whose default behavior is to produce a core dump", and on this
    kernel a process that is not dumpable starts no collector, under a hard limit of 0 too.
    The call is in those programs and not here, because execve(2) says that "the process's
    'dumpable' attribute is set to the value 1" by an exec of an ordinary program. The limit
    of 1 stays as the second measure, for a process that the tests end with SIGQUIT and that
    is not one of those programs."""
    for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, signal.SIG_DFL)
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    soft = 1 if sys.platform.startswith("linux") else 0
    if hard != resource.RLIM_INFINITY and hard < soft:
        soft = hard
    resource.setrlimit(resource.RLIMIT_CORE, (soft, hard))


@pytest.fixture
def start(env: dict[str, str], tmp_path: Path) -> Iterator[Callable[..., Wrapped]]:
    started: list[Wrapped] = []

    def _start(
        *arguments: str,
        environ: dict[str, str] | None = None,
        through: Path | None = None,
        prepare: Callable[[], None] = default_signals,
    ) -> Wrapped:
        # `through` is a script that loads the tool, changes one constant and runs it.
        # `prepare` sets the signal dispositions that the tool inherits.
        proc = subprocess.Popen(
            [PYTHON, *([str(through)] if through else []), str(TOOL), *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ or env,
            cwd=tmp_path,
            bufsize=0,
            preexec_fn=prepare,
        )
        started.append(Wrapped(proc))
        return started[-1]

    yield _start
    # Closing every standard input ends every wrapped command, also one whose wrapper a
    # test killed; a wrapper that still waited for the lock then gets it and ends too.
    for wrapped in started:
        if wrapped.proc.stdin is not None and not wrapped.proc.stdin.closed:
            wrapped.proc.stdin.close()
    for wrapped in started:
        try:
            wrapped.proc.wait(timeout=LIMIT)
        except subprocess.TimeoutExpired:
            wrapped.proc.kill()
            wrapped.proc.wait()
        for stream in (wrapped.proc.stdout, wrapped.proc.stderr):
            if stream is not None:
                stream.close()


def tool(
    env: dict[str, str],
    *arguments: str,
    cwd: Path | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the tool to its end. The timeout is what fails a call that blocks."""
    return subprocess.run(
        [PYTHON, str(TOOL), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=LIMIT,
        pass_fds=pass_fds,
        preexec_fn=default_signals,
    )


def events(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def status_json(env: dict[str, str]) -> dict[str, object]:
    done = tool(env, "status", "--json")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def free_report(lock: Path) -> dict[str, object]:
    """What `status --json` says of a lock that nobody holds, when it found no problem
    with the folder of the lock file (a folder that is not there is not checked)."""
    return {"state": "free", "lock": str(lock), "holder": None, "usable": True}


def exits_with(code: int) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def reaped_pid() -> int:
    """The process ID of a process that has ended and has been waited for."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


# red_at_pretip
def test_second_wrapper_waits_and_starts_only_after_the_first_ends(
    start: Callable[..., Wrapped], hold: Callable[[str], list[str]], log: Path
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    second = start("run", "--", *hold("second"))
    waiting = second.line_with("stderr", "waiting for")
    assert f"pid {first.pid}" in waiting
    # The second wrapper has reported that it waits, so its command cannot have started.
    assert events(log) == ["start first"]

    first.release()
    assert first.wait() == 0
    assert second.line("stdout") == "started"
    assert events(log) == ["start first", "end first", "start second"]
    second.release()
    assert second.wait() == 0
    assert events(log)[-1] == "end second"


# red_at_pretip (fix 1, finding 2, case SIGKILL-holder of in/adversarial.py). It replaces
# test_waiter_gets_the_lock_when_the_holder_is_killed, which pinned the defect.
def test_lock_lasts_while_the_command_of_a_killed_wrapper_runs(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    second = start("run", "--", *hold("second"))
    second.line_with("stderr", "waiting for")

    first.proc.kill()
    assert first.wait() == -signal.SIGKILL
    # The command of the killed wrapper still runs and still holds the lock.
    refused = tool(env, "run", "--no-wait", "--", *hold("third"))
    assert refused.returncode == BUSY
    assert f"pid {first.pid}" in refused.stderr
    report = status_json(env)
    assert report["state"] == "held"
    assert report["holder"] is None
    left = report["stale_record"]
    assert isinstance(left, dict)
    assert left["pid"] == first.pid
    text = tool(env, "status").stdout
    assert text.splitlines()[0] == "held"
    assert f"pid {first.pid}" in text and "has ended" in text
    assert events(log) == ["start first"]

    # The command ends: only now does the waiter start.
    first.release()
    assert second.line("stdout") == "started"
    assert events(log) == ["start first", "end first", "start second"]
    holder = status_json(env)["holder"]
    assert isinstance(holder, dict)
    assert holder["pid"] == second.pid


# red_at_pretip
def test_no_wait_against_a_held_lock_exits_busy_and_runs_nothing(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"

    refused = tool(env, "run", "--no-wait", "--", *hold("second"))

    assert refused.returncode == BUSY
    assert refused.stdout == ""
    assert f"pid {first.pid}" in refused.stderr
    assert "nothing was run" in refused.stderr
    assert events(log) == ["start first"]


# red_at_pretip
def test_bounded_wait_gives_up_busy_and_names_the_holder_once(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"

    refused = tool(env, "run", "--wait", "0.5", "--", *hold("second"))

    assert refused.returncode == BUSY
    # Many attempts were made in that half second: the holder is named once, not per try.
    assert refused.stderr.count("waiting for") == 1
    assert f"waiting for pid {first.pid}" in refused.stderr
    assert events(log) == ["start first"]


# red_at_pretip
# Fix 1, finding 9 (pins_existing, cases command-exit-* of in/adversarial.py): 64, 73, 75,
# 126 and 127 are also codes of the wrapper itself, and a command that ends with one of
# them, or with 255, is passed through all the same. Green at 607e026.
@pytest.mark.parametrize("code", [0, 1, 2, 5, 42, 64, 73, 75, 126, 127, 255])
def test_exit_code_of_the_command_is_passed_through(env: dict[str, str], code: int) -> None:
    done = tool(env, "run", "--", *exits_with(code))
    assert done.returncode == code
    assert status_json(env)["state"] == "free"


# red_at_pretip
@pytest.mark.parametrize("signum", [signal.SIGKILL, signal.SIGTERM])
def test_signal_death_of_the_command_is_128_plus_the_signal(
    env: dict[str, str], signum: signal.Signals
) -> None:
    command = [sys.executable, "-c", f"import os; os.kill(os.getpid(), {int(signum)})"]
    done = tool(env, "run", "--", *command)
    assert done.returncode == 128 + signum
    assert f"signal {int(signum)}" in done.stderr
    assert status_json(env)["state"] == "free"


# red_at_pretip
def test_command_that_cannot_start_is_127_and_leaves_the_lock_free(
    env: dict[str, str], tmp_path: Path
) -> None:
    done = tool(env, "run", "--", str(tmp_path / "no-such-program"))
    assert done.returncode == 127
    assert status_json(env)["state"] == "free"


# red_at_pretip
def test_status_without_a_lock_file_is_free_and_creates_nothing(
    env: dict[str, str], lock: Path
) -> None:
    done = tool(env, "status")
    assert done.returncode == 0
    assert done.stdout.splitlines()[0] == "free"
    assert status_json(env) == free_report(lock)
    assert not lock.exists()
    assert not lock.parent.exists()


# red_at_pretip (fix 3, R3-E of in/P6-review3-codex.md: `status` said `free` for a path
# that `run` refused, so a reader took an unusable path for free capacity.) `status` asks
# the folder what `run` asks it, makes nothing, and says the same reason as `run`.
@pytest.mark.parametrize(
    ("folder", "reason"),
    [
        ("missing", None),
        ("private", None),
        ("group-writable", "chmod go-w"),
        ("symbolic-link", "is a symbolic link"),
    ],
)
def test_status_says_whether_run_can_use_the_path_and_makes_nothing(
    env: dict[str, str], lock: Path, tmp_path: Path, folder: str, reason: str | None
) -> None:
    if folder == "symbolic-link":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        lock.parent.symlink_to(tmp_path / "elsewhere")
    elif folder != "missing":
        lock.parent.mkdir(mode=0o700)
        lock.parent.chmod(0o770 if folder == "group-writable" else 0o700)
    before = sorted(tmp_path.rglob("*"))

    report = status_json(env)
    text = tool(env, "status")
    assert text.returncode == 0 and text.stdout.splitlines()[0] == "free"
    assert report["state"] == "free" and report["usable"] is (reason is None)
    if reason is None:
        assert "unusable_because" not in report
        assert "usable: yes" in text.stdout.splitlines()
    else:
        because = report["unusable_because"]
        assert isinstance(because, str) and reason in because
        assert f"usable: no. {because}" in text.stdout.splitlines()
    assert sorted(tmp_path.rglob("*")) == before

    done = tool(env, "run", "--no-wait", "--", *exits_with(3))
    if reason is None:
        assert done.returncode == 3
    else:
        assert done.returncode == NO_LOCK_FILE
        assert f"suite lock: {report['unusable_because']}; nothing was run" in done.stderr


# pins_existing (fix 4, R4-C of in/P6-review4-codex.md, case uncreatable-missing-folder
# of in/review4-path_attacks.py). It pins the limit that the docstring of `probe` states:
# `usable` is true "also when the folder does not exist: that one is not checked at all,
# and `run` can still fail to make it". Here the folder above lets nobody write.
def test_usable_is_no_promise_for_a_lock_folder_that_is_not_there(
    env: dict[str, str], tmp_path: Path
) -> None:
    above = tmp_path / "above"
    above.mkdir(mode=0o700)
    lock = above / "missing" / "suite.lock"
    environ = {**env, PATH_ENV: str(lock)}
    above.chmod(0o500)
    try:
        report = status_json(environ)
        text = tool(environ, "status")
        done = tool(environ, "run", "--no-wait", "--", *exits_with(3))
    finally:
        above.chmod(0o700)

    assert report == free_report(lock)
    assert text.returncode == 0 and text.stdout.splitlines()[0] == "free"
    assert "usable: yes" in text.stdout.splitlines()
    assert done.returncode == NO_LOCK_FILE
    assert f"cannot use the folder {lock.parent}" in done.stderr
    assert "nothing was run" in done.stderr
    assert list(above.iterdir()) == []


# red_at_pretip (fix 3, R3-E: the key `usable` is new). A held lock is `held` whatever
# `usable` says: the folder was opened to others after the run had started.
def test_held_lock_stays_held_when_run_would_refuse_the_path_now(
    start: Callable[..., Wrapped], hold: Callable[[str], list[str]], env: dict[str, str], lock: Path
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    lock.parent.chmod(0o770)
    try:
        report = status_json(env)
        lines = tool(env, "status").stdout.splitlines()
    finally:
        lock.parent.chmod(0o700)
    assert report["state"] == "held" and report["usable"] is False
    holder = report["holder"]
    assert isinstance(holder, dict) and holder["pid"] == first.pid
    assert lines[0] == "held" and f"pid: {first.pid}" in lines
    assert any(line.startswith("usable: no. ") for line in lines)


# red_at_pretip
def test_status_while_held_names_the_holder_and_does_not_block(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"

    report = status_json(env)
    assert report["state"] == "held"
    assert report["lock"] == str(lock)
    holder = report["holder"]
    assert isinstance(holder, dict)
    assert holder["pid"] == first.pid
    assert holder["command"] == hold("first")
    assert os.path.samefile(holder["cwd"], tmp_path)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", holder["started"])
    assert abs(holder["started_epoch"] - time.time()) < 3600

    text = tool(env, "status")
    assert text.returncode == 0
    lines = text.stdout.splitlines()
    assert lines[0] == "held"
    assert f"pid: {first.pid}" in lines
    assert any(line.startswith("started: ") for line in lines)
    assert any(line.startswith("command: ") and "hold.py" in line for line in lines)
    assert any(line.startswith("cwd: ") for line in lines)


# red_at_pretip
def test_clean_end_leaves_the_lock_free_and_no_record(env: dict[str, str], lock: Path) -> None:
    assert tool(env, "run", "--", *exits_with(0)).returncode == 0
    assert lock.read_text() == ""
    assert status_json(env) == free_report(lock)


# red_at_pretip
def test_stale_record_of_a_killed_holder_does_not_block(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    # The record is the one the tool itself left: its holder was killed and could not
    # clear it.
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    first.proc.kill()
    first.wait()
    first.release()
    wait_free(lock)
    assert json.loads(lock.read_text())["pid"] == first.pid

    report = status_json(env)
    assert report["state"] == "free"
    assert report["holder"] is None
    stale = report["stale_record"]
    assert isinstance(stale, dict)
    assert stale["pid"] == first.pid
    assert tool(env, "status").stdout.splitlines()[0] == "free"

    done = tool(env, "run", "--no-wait", "--", *hold("second"))
    assert done.returncode == 0
    assert events(log) == ["start first", "end first", "start second", "end second"]
    assert lock.read_text() == ""


# red_at_pretip
@pytest.mark.parametrize("content", ["live", "not json\n", "[1, 2]\n", '{"pid": "x"}\n'])
def test_record_without_a_lock_never_blocks(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    env: dict[str, str],
    lock: Path,
    content: str,
) -> None:
    if content == "live":
        # A record as the tool writes it, changed to name a process that is alive (this
        # test): a living process in the record is still not a lock.
        first = start("run", "--", *hold("first"))
        assert first.line("stdout") == "started"
        record = json.loads(lock.read_text())
        first.release()
        assert first.wait() == 0
        record["pid"] = os.getpid()
        content = json.dumps(record) + "\n"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content)

    assert status_json(env)["state"] == "free"
    assert tool(env, "run", "--no-wait", "--", *exits_with(3)).returncode == 3


# red_at_pretip. Fix 1, finding 6: SIGINT (case SIGINT-to-wrapper of in/adversarial.py)
# and SIGQUIT are red at 607e026; SIGTERM was the test of the first round, and SIGHUP is
# pins_existing (green at 607e026).
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_signal_to_the_wrapper_alone_ends_the_command_and_frees_the_lock(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    signum: signal.Signals,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"

    # Only the wrapper gets the signal, as from `kill <pid>`.
    first.proc.send_signal(signum)

    # 128 + N is the wrapper's report that its command died of the signal. A wrapper
    # that died of the signal itself, leaving the command behind, would read -N here.
    assert first.wait() == 128 + signum
    assert first.rest("stdout") == ""
    assert events(log) == ["start first"]
    assert lock.read_text() == ""
    assert status_json(env) == free_report(lock)


# red_at_pretip
def test_wrapper_inside_the_holder_runs_without_waiting(env: dict[str, str]) -> None:
    inner = [PYTHON, str(TOOL), "run", "--no-wait", "--", *exits_with(3)]
    done = tool(env, "run", "--", *inner)
    assert done.returncode == 3
    assert "already held" in done.stderr


# red_at_pretip
def test_marker_of_a_process_that_is_not_an_ancestor_does_not_skip_the_lock(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    identity = lock.stat()
    for pid in (first.pid, reaped_pid()):
        # The marker a holder gives its command, in the environment of a process that
        # does not descend from that holder.
        marked = {**env, HELD_ENV: f"{pid}:{identity.st_dev}:{identity.st_ino}"}
        refused = tool(marked, "run", "--no-wait", "--", *hold("second"))
        assert refused.returncode == BUSY
    assert events(log) == ["start first"]


# red_at_pretip
def test_holder_of_one_lock_file_still_waits_for_another(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    tmp_path: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    outer = {**env, PATH_ENV: str(tmp_path / "other" / "suite.lock")}
    inner = [
        "/usr/bin/env",
        f"{PATH_ENV}={env[PATH_ENV]}",
        PYTHON,
        str(TOOL),
        "run",
        "--no-wait",
        "--",
        *hold("second"),
    ]

    done = tool(outer, "run", "--", *inner)

    assert done.returncode == BUSY
    assert events(log) == ["start first"]


# red_at_pretip
def test_lock_file_that_cannot_be_written_still_excludes(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    lock.chmod(0o400)
    lock.parent.chmod(0o500)
    try:
        first = start("run", "--", *hold("first"))
        assert first.line("stdout") == "started"
        refused = tool(env, "run", "--no-wait", "--", *hold("second"))
        assert refused.returncode == BUSY
        assert status_json(env)["state"] == "held"
        first.release()
        assert first.wait() == 0
        assert events(log) == ["start first", "end first"]
        assert tool(env, "run", "--no-wait", "--", *exits_with(4)).returncode == 4
    finally:
        lock.parent.chmod(0o700)


# red_at_pretip
@pytest.mark.parametrize(
    "arguments",
    [
        ["run", sys.executable, "-c", "pass"],
        ["run", "--"],
        ["run", "--wait", "-1", "--", "true"],
        ["run", "--wait", "soon", "--", "true"],
        ["run", "--wait", "5", "--no-wait", "--", "true"],
        ["hold"],
        [],
    ],
)
def test_usage_errors_exit_64_and_touch_no_lock(
    env: dict[str, str], lock: Path, arguments: list[str]
) -> None:
    done = tool(env, *arguments)
    assert done.returncode == USAGE
    assert done.stdout == ""
    assert not lock.exists()


# red_at_pretip
def test_default_lock_is_in_the_account_home_whatever_the_environment_says() -> None:
    spec = importlib.util.spec_from_file_location("suite_lock_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = pwd.getpwuid(os.getuid()).pw_dir
    expected = os.path.join(home, ".local", "state", "answerback", "suite.lock")

    assert module.lock_path({}) == expected
    assert module.lock_path({"HOME": "/elsewhere", "XDG_STATE_HOME": "/elsewhere/s"}) == expected
    assert module.lock_path({PATH_ENV: "/chosen/suite.lock"}) == "/chosen/suite.lock"
    assert module.BUSY == BUSY


# red_at_pretip
def test_tool_is_python_3_9_syntax_and_standard_library_only() -> None:
    tree = ast.parse(TOOL.read_text(), feature_version=(3, 9))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {
        "__future__",
        "argparse",
        "collections",
        "errno",
        "fcntl",
        "json",
        "math",
        "os",
        "pwd",
        "select",
        "shlex",
        "signal",
        "stat",
        "subprocess",
        "sys",
        "time",
        "typing",
    }


# Fix round 1. Helpers for the tests below.

# Blocks until nobody holds the lock: the kernel wakes it, no clock is read.
WAIT_FREE = """\
import fcntl
import os
import sys

fcntl.flock(os.open(sys.argv[1], os.O_RDONLY), fcntl.LOCK_SH)
"""

# The command of an outer run: it starts two runs of its own, one after the other, and
# reports what the second one did while the first one was still running.
SIBLINGS = """\
import json
import subprocess
import sys

run = json.loads(sys.argv[1])
hold = json.loads(sys.argv[2])
# close_fds=False: this program hands the lock descriptor of its holder on.
a = subprocess.Popen(run + hold + ["a"], stdout=subprocess.PIPE, close_fds=False)
a.stdout.readline()
for label in ("b", "c"):
    done = subprocess.run(
        run + hold + [label],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        close_fds=False,
    )
    print(label, done.returncode, flush=True)
    if label == "b":
        sys.stdin.read()
        a.wait()
"""


def wait_free(lock: Path) -> None:
    subprocess.run([sys.executable, "-c", WAIT_FREE, str(lock)], check=True, timeout=LIMIT)


def nested(*arguments: str) -> list[str]:
    return [PYTHON, str(TOOL), *arguments]


# red_at_pretip (fix 1, finding 1, case forged-ancestor-marker of in/adversarial.py)
def test_marker_that_names_a_real_ancestor_without_the_lock_skips_nothing(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    identity = lock.stat()
    unlocked = os.open(lock, os.O_RDONLY)
    elsewhere = os.open(log, os.O_RDONLY)
    closed = os.dup(unlocked)
    os.close(closed)
    pipe = os.pipe()
    try:
        # This test is a real ancestor of the wrapper below and holds no lock. A marker
        # of this round names the lock file by device and inode, and a descriptor: of
        # the lock file but without the lock, of another file, of nothing. Such a run
        # is refused with the exit code of its own (fix 2, R2-B). A marker that names
        # another file, or is no marker (the forms of 607e026 and of fix 1 among them),
        # leaves a run that finds the lock busy like any other run. Fix 2 dropped the
        # pipe from the marker (R2-A), so the form of fix 1 with a pipe is no marker.
        file = f"{identity.st_dev}/{identity.st_ino}"
        markers = [
            (f"{os.getpid()}/{unlocked}/{file}", NO_DESCRIPTOR),
            (f"{first.pid}/{unlocked}/{file}", NO_DESCRIPTOR),
            (f"{first.pid}/{elsewhere}/{file}", NO_DESCRIPTOR),
            (f"{first.pid}/{closed}/{file}", NO_DESCRIPTOR),
            (f"{first.pid}/0/{file}", NO_DESCRIPTOR),
            (f"{first.pid}/{unlocked}/{identity.st_dev}/{identity.st_ino + 1}", BUSY),
            (f"{first.pid}/{10**10}/{file}", BUSY),
            (f"{os.getpid()}:{identity.st_dev}:{identity.st_ino}", BUSY),
            (f"{first.pid}/{unlocked}/{file}/{pipe[1]}", BUSY),
            (f"{first.pid}/{unlocked}/{pipe[1]}", BUSY),
            (f"{first.pid}/{unlocked}", BUSY),
            ("x/y/z,1", BUSY),
        ]
        for marker, code in markers:
            refused = tool(
                {**env, HELD_ENV: marker},
                "run",
                "--no-wait",
                "--",
                *hold("second"),
                pass_fds=(unlocked, elsewhere, pipe[1]),
            )
            assert refused.returncode == code, (marker, refused.stderr)
            assert "nothing was run" in refused.stderr
            assert "which started this run" not in refused.stderr
    finally:
        for number in (unlocked, elsewhere, *pipe):
            os.close(number)
    assert events(log) == ["start first"]


# red_at_pretip (fix 3b, finding 6 of in/P6-linux-3.md: the refusal with 76 said that the
# variable "names pid N as the run above this one that holds it". The run that the
# variable names can have ended, and then another run holds the lock. The tool cannot
# know which, and it does not ask whether pid N is alive.) The text says what is known:
# the lock is held, the variable names pid N, and this process does not have that run's
# lock descriptor. Then the two ways out. It is the same text for both cases.
@pytest.mark.parametrize("named", ["alive", "ended"])
def test_refusal_without_the_descriptor_says_only_what_is_known(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    named: str,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    pid = first.pid
    if named == "ended":
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=LIMIT)
        pid = gone.pid
    identity = lock.stat()
    unlocked = os.open(lock, os.O_RDONLY)
    try:
        marker = f"{pid}/{unlocked}/{identity.st_dev}/{identity.st_ino}"
        refused = tool(
            {**env, HELD_ENV: marker},
            "run",
            "--no-wait",
            "--",
            *hold("second"),
            pass_fds=(unlocked,),
        )
    finally:
        os.close(unlocked)
    text = refused.stderr
    assert refused.returncode == NO_DESCRIPTOR, text
    assert f"{lock} is held. {HELD_ENV} names pid {pid}, and this process does not" in text
    assert "does not have that run's lock descriptor" in text
    assert "holds it" not in text
    assert "as the run above" not in text
    # Fix 4, R4-D: the two waits form a cycle only if the holder's command waits for
    # this run, and the tool does not know that. So "can", and no "would never end".
    assert "can wait for each other without end" in text
    assert "never end" not in text and "the holder waits for this run" not in text
    assert "pass the descriptor on" in text
    assert f"unset {HELD_ENV}" in text
    assert "nothing was run" in text
    assert events(log) == ["start first"]


# red_at_pretip (fix 1, finding 1, case nested-siblings of in/extra_checks.py)
def test_two_runs_below_one_holder_do_not_run_at_once(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    tmp_path: Path,
) -> None:
    script = tmp_path / "siblings.py"
    script.write_text(SIBLINGS)
    run = json.dumps(nested("run", "--no-wait", "--"))
    outer = start("run", "--", sys.executable, str(script), run, json.dumps(hold("")[:-1]))

    # "b" was refused while "a" ran, and nothing of "b" started.
    assert outer.line("stdout") == f"b {BUSY}"
    assert events(log) == ["start a"]
    outer.release()
    # "c" came after "a" had ended and ran.
    assert outer.line("stdout") == "c 0"
    assert outer.wait() == 0
    assert events(log) == ["start a", "end a", "start c", "end c"]


# pins_existing (fix 1, the Linux run's test gap: the holder is two processes up)
def test_wrapper_two_processes_below_the_holder_runs_without_waiting(
    env: dict[str, str],
) -> None:
    inner = nested("run", "--no-wait", "--", *exits_with(3))
    # A shell that cannot replace itself with the inner run: it has more to do after it.
    done = tool(env, "run", "--", "sh", "-c", shlex.join(inner) + "; exit $?")
    assert done.returncode == 3
    assert "already held" in done.stderr
    assert status_json(env)["state"] == "free"


# The command of the "leaves a process behind" tests: it starts HOLD and ends at once.
# `how` says what HOLD gets and where it runs:
#   spawn        a new program with every descriptor of this command
#   fork         a copy of this command with every descriptor, as a daemon starts
#   closed       what Python's subprocess gives when it is told nothing: no descriptor
#   lock-closed  every descriptor but the lock: an independent close (fix 2, R2-A)
#   unlocks      every descriptor, after it unlocked the lock for everybody
#   daemon...    the same in a session of its own, which HOLD leads
COMMAND_LIVES = "SUITE_LOCK_TEST_COMMAND_LIVES"
LEAVE_BEHIND = """\
import fcntl
import os
import subprocess
import sys

how, hold = sys.argv[1], sys.argv[2:]
entries = os.environ["ANSWERBACK_SUITE_LOCK_HELD"].split(",")
held = [int(entry.split("/")[1]) for entry in entries]


def close_lock():
    for number in held:
        os.close(number)


def unlock():
    fcntl.flock(held[-1], fcntl.LOCK_UN)


# Open until this command ends: the test reads the end of the file as that event. No
# process that this command starts gets it.
lives = os.environ["SUITE_LOCK_TEST_COMMAND_LIVES"]
os.open(lives, os.O_WRONLY)
ways = {
    "spawn": {"close_fds": False},
    "closed": {},
    "lock-closed": {"close_fds": False, "preexec_fn": close_lock},
    "unlocks": {"close_fds": False, "preexec_fn": unlock},
    "daemon": {"close_fds": False, "start_new_session": True},
    "daemon-closed": {"start_new_session": True},
    "daemon-lock-closed": {
        "close_fds": False,
        "start_new_session": True,
        "preexec_fn": close_lock,
    },
}
if how == "fork":
    pid = os.fork()
    if pid == 0:
        os.execv(hold[0], hold)
else:
    pid = subprocess.Popen(hold, **ways[how]).pid
with open(lives + ".pid", "w") as pid_file:
    pid_file.write(str(pid))
"""

# A script for `through` of the `start` fixture: the tool with other constants. With
# SUITE_LOCK_TEST_RECORD (JSON) it also writes down every signal that the tool sends
# ("calls": a file; a line "killpg N" for os.killpg, "kill N" for os.kill with a signal
# other than 0) and passes each on to the real call (fix 8, R6-B). With "checkpoint" (a
# FIFO that the test holds open) it writes one byte each time the tool's main loop calls
# select.select (fix 8, R6-A). The shim writes a checkpoint immediately before
# `select.select`, after this pass's signal-dispatch code. A readable select is followed
# by `os.read` to drain the wake pipe (fix 9, R7-A).
#
# With "clock" beside "checkpoint" the tool's time is the shim's (T-129): the tool's
# time.monotonic is the real one plus what the shim has moved it on, and the shim moves
# it at each call of select.select in the main loop, before the checkpoint byte. A call
# with a timeout does not sleep: the clock goes on by the timeout and the call looks
# once at the descriptors, so whatever the tool wanted to be woken for is due at the
# next pass. A call with no timeout is passed on unchanged and waits as before; the
# clock goes on by CLOCK_STEP_SECONDS first, so what a handler defers during that wait
# is due one pass later. Without "clock" nothing of this is in place.
CLOCK_STEP_SECONDS = 3600.0
THROUGH = """\
import importlib.util
import json
import os
import select
import sys
import time
import types

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("suite_lock_with_constants", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, value in CONSTANTS.items():
    # A constant that the tool does not have is an error of the test, not a new one.
    getattr(module, name)
    setattr(module, name, value)
record = os.environ.get("SUITE_LOCK_TEST_RECORD")
if record:
    record = json.loads(record)

    def note(text):
        with open(record["calls"], "a") as file:
            file.write(text + "\\n")

    def killpg(group, signum):
        note("killpg %d" % signum)
        return os.killpg(group, signum)

    def kill(pid, signum):
        if signum != 0:
            note("kill %d" % signum)
        return os.kill(pid, signum)

    module.os = types.SimpleNamespace(**{**vars(os), "killpg": killpg, "kill": kill})
    if record.get("checkpoint"):
        mark = os.open(record["checkpoint"], os.O_WRONLY)
        moved = [0.0]

        def checked(read, write, error, timeout=None):
            if record.get("clock"):
                moved[0] += CLOCK_STEP if timeout is None else timeout
                timeout = None if timeout is None else 0
            os.write(mark, b".")
            return select.select(read, write, error, timeout)

        if record.get("clock"):
            module.time = types.SimpleNamespace(
                **{**vars(time), "monotonic": lambda: time.monotonic() + moved[0]}
            )
        module.select = types.SimpleNamespace(**{**vars(select), "select": checked})
sys.exit(module.main(sys.argv[2:]))
"""


def sent_signals(calls: Path) -> list[str]:
    """What a tool run through THROUGH with SUITE_LOCK_TEST_RECORD sent, in order."""
    return calls.read_text().splitlines() if calls.exists() else []


def with_constants(tmp_path: Path, **constants: float) -> Path:
    script = tmp_path / "through.py"
    script.write_text(
        THROUGH.replace("CONSTANTS", repr(constants)).replace(
            "CLOCK_STEP", repr(CLOCK_STEP_SECONDS)
        )
    )
    return script


def left_behind(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    env: dict[str, str],
    tmp_path: Path,
    how: str,
    through: Path | None = None,
) -> tuple[Wrapped, int]:
    """A run whose command has started HOLD in the way `how` and has ended. Returns the
    run and the process ID of HOLD."""
    script = tmp_path / "leave_behind.py"
    script.write_text(LEAVE_BEHIND)
    fifo = tmp_path / "command-lives"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        first = start(
            *("run", "--", sys.executable, str(script), how, *hold("left")),
            environ={**env, COMMAND_LIVES: str(fifo)},
            through=through,
        )
        assert first.line("stdout") == "started"
        # The end of the file is the end of the command: nobody else has the FIFO open.
        assert select.select([reader], [], [], LIMIT)[0]
        assert os.read(reader, 1) == b""
    finally:
        os.close(reader)
    return first, int((tmp_path / "command-lives.pid").read_text())


# fix 3, the reduction (R3-D of in/P6-review3-codex.md): the wrapper waits for its direct
# command and for nothing else, and the lock lasts exactly as long as some process has
# the open file that `run` locked. A process that kept the descriptor keeps the lock after
# the wrapper has ended (spawn, fork, daemon), and the wrapper says so in one line that
# names nobody. A process that closed it is not covered (closed, lock-closed and the two
# daemons that closed it), and neither is anything after an unlock: the lock is free.
# red_at_pretip for spawn, fork, closed and lock-closed (the tool of 71b0f30 waited for
# the session of the command to be empty), for daemon (it said nothing) and for unlocks
# (it left its record in the file); pins_existing for daemon-closed and
# daemon-lock-closed. It replaces the tests of the strict lifetime of fix 2.
@pytest.mark.parametrize(
    ("how", "state"),
    [
        ("spawn", "held"),
        ("fork", "held"),
        ("daemon", "held"),
        ("closed", "free"),
        ("lock-closed", "free"),
        ("daemon-closed", "free"),
        ("daemon-lock-closed", "free"),
        ("unlocks", "free"),
    ],
)
def test_wrapper_ends_with_its_command_and_the_lock_lasts_as_long_as_the_descriptor(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    how: str,
    state: str,
) -> None:
    first, _left = left_behind(start, hold, env, tmp_path, how)

    assert first.wait() == 0
    said, ended = first.so_far("stderr")
    lines = said.splitlines()
    assert f"(from {PATH_ENV})" in lines[0]
    # The process that was left has the standard error of the run: it is still there.
    assert not ended
    report = status_json(env)
    assert report["state"] == state and report["holder"] is None
    contender = tool(env, "run", "--no-wait", "--", *exits_with(0))
    if state == "held":
        assert len(lines) == 2 and "still held" in lines[1]
        # The line claims no holder: it can be a process of this run, or the next run.
        assert "pid" not in lines[1]
        stale = report["stale_record"]
        assert isinstance(stale, dict) and stale["pid"] == first.pid
        assert contender.returncode == BUSY
        # A run that waits names the holder: the record that the ended run left.
        assert "a run that has ended" in contender.stderr
        assert f"pid {first.pid}" in contender.stderr
    else:
        assert len(lines) == 1
        assert "stale_record" not in report
        assert contender.returncode == 0
    assert events(log) == ["start left"]

    first.release()
    assert first.rest("stdout") == ""
    wait_free(lock)
    assert events(log) == ["start left", "end left"]
    # What the process that was left held is gone with it, and so is the record.
    assert tool(env, "run", "--no-wait", "--", *exits_with(3)).returncode == 3
    assert "stale_record" not in status_json(env)


# red_at_pretip (fix 1, finding 3, case symlink of in/adversarial.py)
@pytest.mark.parametrize("target_exists", [True, False])
def test_symlink_at_the_lock_path_is_refused_and_its_target_untouched(
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    target_exists: bool,
) -> None:
    lock.parent.mkdir(parents=True)
    target = lock.parent / "target"
    if target_exists:
        target.write_text("unrelated contents\n")
    lock.symlink_to(target)

    done = tool(env, "run", "--", *hold("first"))

    assert done.returncode == NO_LOCK_FILE
    assert "nothing was run" in done.stderr
    assert events(log) == []
    assert tool(env, "status").returncode == NO_LOCK_FILE
    assert lock.is_symlink()
    if target_exists:
        assert target.read_text() == "unrelated contents\n"
    else:
        assert not target.exists()


# red_at_pretip for the FIFO (fix 1, findings 3 and 10, case fifo-status of
# in/adversarial.py: at 607e026 `status` waits in `open` until the test gives up);
# pins_existing for the folder, which 607e026 refuses with 73 as well.
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_lock_path_that_is_no_regular_file_is_refused_without_waiting(
    env: dict[str, str], lock: Path, kind: str
) -> None:
    lock.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(lock)
    else:
        lock.mkdir()
    for arguments in (
        ["run", "--", *exits_with(0)],
        ["run", "--no-wait", "--", *exits_with(0)],
        ["status"],
        ["status", "--json"],
    ):
        # A call that waited at the FIFO would end in the timeout of `tool`.
        assert tool(env, *arguments).returncode == NO_LOCK_FILE, arguments


# red_at_pretip (fix 2, R2-G, case hardlink-target of in/attack_fixes.py: the record was
# written into the other file, and its content was gone when the run ended)
def test_lock_file_with_a_second_name_is_refused_and_the_other_file_untouched(
    hold: Callable[[str], list[str]], log: Path, env: dict[str, str], lock: Path
) -> None:
    lock.parent.mkdir(parents=True)
    target = lock.parent / "notes.txt"
    target.write_text("unrelated content\n")
    os.link(target, lock)
    for arguments in (["run", "--", *hold("first")], ["status"], ["status", "--json"]):
        done = tool(env, *arguments)
        assert done.returncode == NO_LOCK_FILE, arguments
        assert "hard link" in done.stderr
    assert target.read_text() == "unrelated content\n"
    assert events(log) == []


# red_at_pretip (fix 3, R3-C of in/P6-review3-codex.md, case hardlink-recovery: a backup
# gave the file of a running holder a second name, the tool of 71b0f30 said "Delete the
# lock path", and the run after the deletion started beside the holder.) The refusal
# stays. Its text forbids the deletion and names the two ways out, and the first of them
# is followed here: the other name is removed when no run is active.
def test_refusal_of_a_second_name_never_says_to_delete_a_lock_that_may_be_held(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    alias = lock.parent / "backup-of-suite.lock"
    os.link(lock, alias)

    refused = tool(env, "run", "--no-wait", "--", *hold("second"))
    assert refused.returncode == NO_LOCK_FILE
    assert "hard link" in refused.stderr and "nothing was run" in refused.stderr
    assert "Delete" not in refused.stderr
    assert "Do not delete or replace the lock path" in refused.stderr
    assert "remove the other name when no run is active" in refused.stderr
    assert f"set {PATH_ENV} to another path" in refused.stderr
    # The holder is untouched, and its file is still the one at the path.
    assert lock.samefile(alias) and first.proc.poll() is None
    assert events(log) == ["start first"]

    first.release()
    assert first.wait() == 0
    alias.unlink()
    assert tool(env, "run", "--no-wait", "--", *exits_with(3)).returncode == 3
    assert status_json(env)["state"] == "free"
    assert events(log) == ["start first", "end first"]


# red_at_pretip (fix 2, R2-C, case group-writable-directory of in/attack_fixes.py: 0770
# and 0720 were accepted at c6b5a31, and so was the sticky 1777; 0777 and 0702 were
# refused there too, with a text that did not say what to do)
@pytest.mark.parametrize("mode", [0o770, 0o720, 0o777, 0o702, 0o1777])
def test_lock_folder_that_group_or_others_can_write_is_refused(
    hold: Callable[[str], list[str]], log: Path, env: dict[str, str], lock: Path, mode: int
) -> None:
    lock.parent.mkdir(parents=True)
    lock.parent.chmod(mode)
    done = tool(env, "run", "--", *hold("first"))
    assert done.returncode == NO_LOCK_FILE
    assert f"chmod go-w {shlex.quote(str(lock.parent))}" in done.stderr
    assert "nothing was run" in done.stderr
    assert events(log) == []
    assert not lock.exists()

    # Others may read the folder: only writing lets them put another file at the path.
    lock.parent.chmod(0o755)
    assert tool(env, "run", "--", *exits_with(3)).returncode == 3


# red_at_pretip (fix 2, R2-C: every folder that a run makes is for this user alone, also
# under a umask that takes nothing away; c6b5a31 made the folders above the last one
# with the mode that the umask left, 0777 here)
def test_run_makes_the_lock_folders_for_this_user_alone(
    env: dict[str, str], tmp_path: Path
) -> None:
    lock = tmp_path / "made" / "state" / "suite.lock"
    run = shlex.join(nested("run", "--", *exits_with(3)))
    done = subprocess.run(
        ["sh", "-c", f"umask 0; exec {run}"],
        env={**env, PATH_ENV: str(lock)},
        capture_output=True,
        text=True,
        timeout=LIMIT,
    )
    assert done.returncode == 3, done.stderr
    for folder in (lock.parent, lock.parent.parent):
        assert folder.stat().st_mode & 0o7777 == 0o700
    assert lock.stat().st_mode & 0o7777 == 0o600


# pins_existing (fix 3b, finding 1 of in/P6-linux-3.md: the tests depended on the umask
# of their caller; the tool does not). The tool starts under umask 002 (the account on
# the Linux node that was tested has it), 022 (the Mac that was tested) and 077 (a strict
# account), with a lock folder that is not there yet. The child gets its umask from the
# shell that starts it, not from the fixture above: what the tool makes is the same under
# each.
@pytest.mark.parametrize("umask", ["002", "022", "077"])
def test_tool_does_not_depend_on_the_umask_of_its_caller(
    env: dict[str, str], tmp_path: Path, umask: str
) -> None:
    lock = tmp_path / "made" / "state" / "suite.lock"
    environ = {**env, PATH_ENV: str(lock)}
    run = shlex.join(nested("run", "--", *exits_with(3)))
    status = shlex.join(nested("status", "--json"))
    done = subprocess.run(
        ["sh", "-c", f"umask {umask}; {run}; echo $?; exec {status}"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=LIMIT,
    )
    assert done.returncode == 0, done.stderr
    code, report = done.stdout.split("\n", 1)
    assert code == "3", done.stderr
    assert json.loads(report)["usable"] is True
    for folder in (lock.parent, lock.parent.parent):
        assert folder.stat().st_mode & 0o7777 == 0o700
    assert lock.stat().st_mode & 0o7777 == 0o600


# pins_existing (fix 4, note a of in/P6-review4-codex.md). Pins the sentence of the file's
# docstring "the umask of the caller is no input of this file" from its other side: the
# fixture does not change the umask of the caller either. It gives back what it found,
# after a test that passes and after one that fails. 027 stands for the caller's umask.
@pytest.mark.parametrize("ending", ["the test passes", "the test fails"])
def test_umask_fixture_gives_the_umask_of_its_caller_back(ending: str) -> None:
    before = os.umask(0o027)
    try:
        fixture = umask_of_the_tests.__wrapped__()
        next(fixture)
        inside = os.umask(0o022)
        if ending == "the test fails":
            with pytest.raises(RuntimeError, match="a failing test"):
                fixture.throw(RuntimeError("a failing test"))
        else:
            with pytest.raises(StopIteration):
                next(fixture)
        after = os.umask(before)
    finally:
        os.umask(before)
    assert (inside, after) == (0o022, 0o027)


# pins_existing (fix 3b, finding 4 of in/P6-linux-3.md: the tool of 71b0f30 made the lock
# folders and then refused the run for the mode of a folder above them). The folders
# above are not asked any more, so that refusal is gone. A run that is refused before a
# first folder could be made leaves the place as it was. Not covered, and not true: a
# name that is too long for the file system is reported by the kernel only when it is
# used, after the folders above it were made.
@pytest.mark.parametrize("case", ["a-file-above", "a-link-to-nowhere-above", "no-command"])
def test_refused_run_for_folders_that_are_not_there_makes_nothing(
    env: dict[str, str], tmp_path: Path, case: str
) -> None:
    place = tmp_path / "place"
    place.mkdir()
    arguments = ["run", "--no-wait", "--", *exits_with(3)]
    expected = NO_LOCK_FILE
    if case == "a-file-above":
        (place / "x").write_text("a file, no folder\n")
    elif case == "a-link-to-nowhere-above":
        (place / "x").symlink_to("nowhere")
    else:
        arguments, expected = ["run"], USAGE
    before = sorted(os.listdir(place))
    done = tool({**env, PATH_ENV: str(place / "x" / "y" / "suite.lock")}, *arguments)
    assert done.returncode == expected, done.stderr
    assert sorted(os.listdir(place)) == before
    assert not (place / "nowhere").exists()


# red_at_pretip (fix 3, R3-A of in/P6-review3-codex.md, case writable-symlink-anchor: the
# folder of the lock file was a symbolic link in a folder that everybody can write. The
# tool of 71b0f30 followed it, so whoever pointed it at another folder between two runs
# gave them two lock files, and both ran.) The folder itself is opened now, never what a
# symbolic link there points to: both runs are refused, wherever the link points.
def test_lock_folder_that_is_a_symbolic_link_is_refused_wherever_it_points(
    hold: Callable[[str], list[str]], log: Path, env: dict[str, str], tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    targets = [tmp_path / "target1", tmp_path / "target2"]
    alias = shared / "alias"
    environ = {**env, PATH_ENV: str(alias / "suite.lock")}
    for target in targets:
        target.mkdir(mode=0o700)
        alias.unlink(missing_ok=True)
        alias.symlink_to(target)
        done = tool(environ, "run", "--no-wait", "--", *hold(target.name))
        assert done.returncode == NO_LOCK_FILE
        assert f"the folder {alias} is a symbolic link" in done.stderr
        assert "nothing was run" in done.stderr
        assert list(target.iterdir()) == []
    assert events(log) == []


# pins_existing (fix 3: a symbolic link above the folder of the lock file is followed,
# as before. The folders above the lock folder are trusted, and a home with a link in
# it, to another volume, is one of them.)
@pytest.mark.parametrize("folder", ["state", "made/state"])
def test_symbolic_link_above_the_lock_folder_is_followed(
    env: dict[str, str], tmp_path: Path, folder: str
) -> None:
    volume = tmp_path / "volume"
    (volume / "state").mkdir(parents=True, mode=0o700)
    (volume / "state").chmod(0o700)
    (tmp_path / "link").symlink_to(volume)
    lock = tmp_path / "link" / folder / "suite.lock"
    assert tool({**env, PATH_ENV: str(lock)}, "run", "--", *exits_with(3)).returncode == 3
    assert (volume / folder / "suite.lock").is_file()
    assert (volume / folder).stat().st_mode & 0o7777 == 0o700


# red_at_pretip (fix 3, R3-A and the decision of this round: one check, of the folder of
# the lock file, the same for every path. The tool of 71b0f30 also asked every folder
# from there up to the home of the account, by a path that it resolved first.)
def test_only_the_folder_of_the_lock_file_is_asked_and_the_ones_above_are_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_tool()
    home = tmp_path / "home"
    middle = home / ".local"
    lock = middle / "state" / "answerback" / "suite.lock"
    lock.parent.mkdir(parents=True)
    # The modes are what this test is about, so none of them is left to the umask.
    for folder in (lock.parent, lock.parent.parent, middle, home):
        folder.chmod(0o700)
    account = types.SimpleNamespace(pw_dir=str(home))
    monkeypatch.setattr(module, "pwd", types.SimpleNamespace(getpwuid=lambda _uid: account))

    # The folders above, below the home of the account or not: nothing is asked of them.
    for folder in (lock.parent.parent, middle, home, tmp_path):
        folder.chmod(0o777)
        os.close(module.open_for_run(str(lock))[0])
        folder.chmod(0o700)
    lock.unlink()

    def refused() -> str:
        with pytest.raises(module.Refusal) as refusal:
            module.open_for_run(str(lock))
        assert refusal.value.code == NO_LOCK_FILE
        assert not lock.exists()
        return str(refusal.value.text)

    lock.parent.chmod(0o775)
    assert f"chmod go-w {shlex.quote(str(lock.parent))}" in refused()
    lock.parent.chmod(0o700)
    # A folder of another user, root too. The test cannot make one: it changes who asks.
    monkeypatch.setattr(module.os, "getuid", lambda: lock.parent.stat().st_uid + 1)
    assert "belongs to another user" in refused()


# red_at_pretip (fix 3, R3-A: the check and the open were two walks of one path at
# 71b0f30). The lock file is opened relative to the descriptor of the folder that was
# checked: here another folder is put at the name right after the check, and the file
# is still made in the folder that was checked.
def test_lock_file_is_opened_in_the_folder_that_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_tool()
    checked, moved, other = (tmp_path / name for name in ("state", "moved", "other"))
    checked.mkdir(mode=0o700)
    other.mkdir(mode=0o700)
    check = module.open_folder

    def check_then_swap(folder: str) -> int:
        descriptor = check(folder)
        checked.rename(moved)
        checked.symlink_to(other)
        return descriptor

    monkeypatch.setattr(module, "open_folder", check_then_swap)
    os.close(module.open_for_run(str(checked / "suite.lock"))[0])
    assert (moved / "suite.lock").is_file()
    assert list(other.iterdir()) == []


# red_at_pretip (fix 4, R4-B of in/P6-review4-codex.md, case check-to-open-probe of
# in/review4-path_attacks.py: `status` closed the folder that it had checked and opened
# the lock file by its path, so one report could join the `usable: true` of one folder
# with the held lock of another). The schedule of the reviewer: the folder of mode 0700
# is checked, then it is renamed away and a folder of mode 0775 with a held lock takes
# its name. The report is about the folder that was checked, and that one has no lock.
def test_status_reads_the_lock_file_in_the_folder_that_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_tool()
    checked, moved, other = (tmp_path / name for name in ("state", "moved", "other"))
    checked.mkdir(mode=0o700)
    other.mkdir(mode=0o700)
    other.chmod(0o775)
    held = os.open(other / "suite.lock", os.O_CREAT | os.O_RDWR, 0o600)
    check = module.open_folder

    def check_then_swap(folder: str) -> int:
        descriptor = check(folder)
        checked.rename(moved)
        other.rename(checked)
        return descriptor

    monkeypatch.setattr(module, "open_folder", check_then_swap)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = module.probe(str(checked / "suite.lock"))
    finally:
        os.close(held)
    assert (checked / "suite.lock").is_file() and list(moved.iterdir()) == []
    assert report == free_report(checked / "suite.lock")


# red_at_pretip (fix 1, finding 4, cases inode-delete and inode-replace of
# in/adversarial.py, seen from a run that already waits)
@pytest.mark.parametrize("change", ["delete", "replace"])
def test_waiter_takes_the_lock_of_the_file_that_is_at_the_path_now(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    change: str,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    second = start("run", "--", *hold("second"))
    second.line_with("stderr", "waiting for")
    if change == "delete":
        lock.unlink()
    else:
        fresh = lock.parent / "fresh"
        fresh.write_text("")
        fresh.replace(lock)

    first.release()
    assert first.wait() == 0
    # The holder says that its lock file is gone from the path.
    assert "deleted or replaced" in first.rest("stderr")
    assert second.line("stdout") == "started"
    second.line_with("stderr", "deleted or replaced")
    # The second run holds the lock of the file that every later run will open.
    refused = tool(env, "run", "--no-wait", "--", *hold("third"))
    assert refused.returncode == BUSY
    holder = status_json(env)["holder"]
    assert isinstance(holder, dict)
    assert holder["pid"] == second.pid
    assert events(log) == ["start first", "end first", "start second"]


# pins_existing (fix 1, finding 4: the limit that stays). A run that starts after the
# file was replaced cannot know of the old file: it runs beside the holder. The holder's
# warning at its end is the new part and is asserted in the test above.
def test_run_that_starts_after_a_replacement_does_not_see_the_old_holder(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    lock.unlink()
    assert tool(env, "run", "--no-wait", "--", *hold("second")).returncode == 0
    assert events(log) == ["start first", "start second", "end second"]


def hostile_record(name: str) -> bytes:
    """Holder data that the tool never writes. "deep", "huge-epoch", "huge-pid" and
    "binary" are cases of `records` in in/adversarial.py; the others are of the same
    kind. The process ID is of this test, which is alive, so `status` formats it."""
    live = os.getpid()
    if name == "deep":
        return b"[" * 2000 + b"0" + b"]" * 2000
    if name == "deep-in-a-record":
        return b'{"pid": %d, "command": ' % live + b"[" * 2000 + b"]" * 2000 + b"}"
    if name == "huge-epoch":
        return json.dumps({"pid": live, "started_epoch": 10**400}).encode()
    if name == "nan-epoch":
        return b'{"pid": %d, "started_epoch": NaN}' % live
    if name == "infinite-epoch":
        return b'{"pid": %d, "started_epoch": -Infinity}' % live
    if name == "huge-pid":
        return json.dumps({"pid": 10**200}).encode()
    if name == "negative-pid":
        return b'{"pid": -1}'
    if name == "binary":
        return b"\x00\xff\xfe"
    assert name == "terminal-escapes"
    text = "\x1b[2J\x07 \x9b0m"
    record = {"pid": live, "started": text, "command": [text, "b\nc"], "cwd": text}
    return json.dumps(record).encode()


HOSTILE = [
    "deep",
    "deep-in-a-record",
    "huge-epoch",
    "nan-epoch",
    "infinite-epoch",
    "huge-pid",
    "negative-pid",
    "binary",
    "terminal-escapes",
]


def assert_plain_report(done: subprocess.CompletedProcess[str], first_line: str) -> None:
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == first_line
    assert not any(mark in done.stdout for mark in ("\x1b", "\x07", "\x9b"))


# Fix 1, finding 5, cases record-* of in/adversarial.py. At 607e026: red_at_pretip are
# nan-epoch, infinite-epoch and terminal-escapes with any interpreter, and deep and
# deep-in-a-record when the tool runs on Python 3.9 (the reviewer's interpreter; newer
# ones parse that depth). pins_existing are huge-epoch, huge-pid, negative-pid, binary.
@pytest.mark.parametrize("name", HOSTILE)
def test_holder_data_of_any_shape_in_a_free_file_changes_nothing(
    env: dict[str, str], lock: Path, name: str
) -> None:
    lock.parent.mkdir(parents=True)
    lock.write_bytes(hostile_record(name))

    assert_plain_report(tool(env, "status"), "free")
    report = tool(env, "status", "--json")
    assert report.returncode == 0, report.stderr
    # Strict JSON: no NaN and no Infinity may come out.
    parsed = json.loads(report.stdout, parse_constant=pytest.fail)
    assert parsed["state"] == "free"
    assert parsed["holder"] is None
    assert tool(env, "run", "--no-wait", "--", *exits_with(3)).returncode == 3


# Fix 1, finding 5, the same cases while the lock is held. At 607e026: red_at_pretip are
# huge-epoch, nan-epoch, infinite-epoch and terminal-escapes with any interpreter, and
# deep and deep-in-a-record on Python 3.9. pins_existing are huge-pid, negative-pid and
# binary.
@pytest.mark.parametrize("name", HOSTILE)
def test_holder_data_of_any_shape_in_a_held_file_keeps_status_and_exit_75(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    name: str,
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    lock.write_bytes(hostile_record(name))

    assert_plain_report(tool(env, "status"), "held")
    report = tool(env, "status", "--json")
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout, parse_constant=pytest.fail)["state"] == "held"
    for bound in (["--no-wait"], ["--wait", "0.05"]):
        refused = tool(env, "run", *bound, "--", *hold("second"))
        assert refused.returncode == BUSY, refused.stderr
        assert "nothing was run" in refused.stderr
        assert not any(mark in refused.stderr for mark in ("\x1b", "\x07", "\x9b"))
    assert events(log) == ["start first"]
    first.release()
    assert first.wait() == 0


# A command with a process of its own below it: it runs HOLD and waits for it.
TREE = (
    NOT_DUMPABLE
    + """\
import subprocess
import sys

sys.exit(subprocess.call(sys.argv[1:]))
"""
)

# Put between TREE and HOLD: the process below the command says which descriptors it has
# on the lock file of the run (the file that the last entry of ANSWERBACK_SUITE_LOCK_HELD
# names by device and inode), its parent and its process group, and then becomes HOLD
# (`exec` keeps the process). TREE starts it with Python's defaults, which close the lock
# descriptor.
LOOKS_FOR_THE_DESCRIPTOR = """\
import os
import sys

device, inode = (int(part) for part in os.environ["ANSWERBACK_SUITE_LOCK_HELD"].split("/")[-2:])
found = []
for fd in range(min(os.sysconf("SC_OPEN_MAX"), 65536)):
    try:
        info = os.fstat(fd)
    except OSError:
        continue
    if (info.st_dev, info.st_ino) == (device, inode):
        found.append(fd)
sys.stdout.write("%d %d %d %r\\n" % (os.getpid(), os.getppid(), os.getpgrp(), found))
sys.stdout.flush()
os.execv(sys.argv[1], sys.argv[1:])
"""


# red_at_pretip (fix 1, finding 6, cases SIGTERM-command-tree and SIGHUP-command-tree of
# in/adversarial.py, and the same with SIGINT)
#
# pins_existing (fix 5, R5-A of in/P6-review5-codex.md, the reviewer's case
# closed_fd_signal.py): the process below the command has no descriptor of the lock file
# and stays in the command's process group, and the signal that the wrapper passes on
# still ends it. It pins the sentence of docs/TESTING-HARNESSES.md "One that stayed in the
# command's process group can still get the signals that the wrapper passes on, until the
# wrapper has waited for the command."
#
# SIGQUIT is pins_existing (fix 5, R5-D): it pins "is passed on to the process group that
# the wrapper created" for the fourth signal, which only the wrapper's direct child was
# shown to get. What SIGQUIT ends would write a core file; since fix 6 every process
# that a test starts through default_signals() has a soft core limit that asks for none
# (since fix 7: 1 on Linux, 0 elsewhere), and
# test_commands_that_a_test_starts_have_the_core_limit_that_starts_no_collector pins that.
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_signal_to_the_wrapper_ends_the_whole_command_tree(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE)
    looks = tmp_path / "looks.py"
    looks.write_text(LOOKS_FOR_THE_DESCRIPTOR)
    first = start(
        "run", "--", sys.executable, str(script), sys.executable, str(looks), *hold("first")
    )
    pid, parent, group, found = first.line("stdout").split(" ", 3)
    assert found == "[]"
    # Its parent is the command, which leads the group and the session that the wrapper
    # made; the process is in that group and does not lead it.
    assert int(group) == int(parent) != int(pid)
    assert os.getsid(int(group)) == int(group)
    assert first.line("stdout") == "started"

    first.proc.send_signal(signum)

    assert first.wait() == 128 + signum
    # The wrapper ended after the whole tree: the lock is free and the record is gone.
    assert lock.read_text() == ""
    assert status_json(env) == free_report(lock)
    assert tool(env, "run", "--no-wait", "--", *exits_with(3)).returncode == 3
    # The pipe ends only when every process that had it is gone, the grandchild too. Its
    # standard input is still open, so only the signal can have ended it.
    assert first.rest("stdout") == ""
    assert events(log) == ["start first"]


# A command that writes its own core limit (soft and hard) with the label in its first
# argument and then runs the rest of its arguments, if there are any, and waits for them.
CORE_LIMIT = """\
import resource
import subprocess
import sys

soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
sys.stdout.write("%s %d %d\\n" % (sys.argv[1], soft, hard))
sys.stdout.flush()
sys.exit(subprocess.call(sys.argv[2:]) if sys.argv[2:] else 0)
"""


def wanted_core_limit(hard: int) -> int:
    """The soft core limit that the rule of default_signals() gives under the hard limit
    `hard`, written out again here so that a change of the rule there is seen."""
    wanted = 1 if sys.platform.startswith("linux") else 0
    return wanted if hard == resource.RLIM_INFINITY or hard >= wanted else hard


# red_at_pretip (fix 6, open question 11 of the hand-back of fix 5: SIGQUIT, which the
# tests send, writes a core dump by default, and a collector that the kernel pipes
# dumps to stores them outside every folder of the test). At 94bc316 both processes
# reported the caller's soft limit.
#
# red_at_pretip (fix 7, part A: at 63b7b91 the value is 0, and with a piped core_pattern a
# limit of 0 still starts the collector for every process that SIGQUIT ends). The
# command of a run that a test starts, and a process below it that TREE starts, have the
# soft core limit of the rule of default_signals(): 1 on Linux, 0 elsewhere, never above
# the hard limit, which is the test process's own. No process of this test gets a signal.
def test_commands_that_a_test_starts_have_the_core_limit_that_starts_no_collector(
    start: Callable[..., Wrapped], tmp_path: Path
) -> None:
    script, tree = tmp_path / "core_limit.py", tmp_path / "tree.py"
    script.write_text(CORE_LIMIT)
    tree.write_text(TREE)
    below = [sys.executable, str(tree), sys.executable, str(script), "below"]
    first = start("run", "--", sys.executable, str(script), "command", *below)
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    soft = wanted_core_limit(hard)

    assert first.line("stdout") == f"command {soft} {hard}"
    assert first.line("stdout") == f"below {soft} {hard}"
    assert first.wait() == 0


# A child that lowers its own hard core limit to 0, then runs default_signals() of the
# test file named in its first argument and writes its core limit. It gets no signal.
UNDER_A_HARD_LIMIT_OF_0 = """\
import importlib.util
import resource
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
spec = importlib.util.spec_from_file_location("suite_lock_tests_in_a_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.default_signals()
sys.stdout.write("%d %d\\n" % resource.getrlimit(resource.RLIMIT_CORE))
"""


# pins_existing (fix 7, part A, the second half of the rule of default_signals(): a hard
# limit below the value gives the hard limit, since a soft limit above the hard one is
# refused). Under a hard limit of 0 the function sets 0 and does not raise.
def test_core_limit_of_the_tests_stays_within_a_hard_limit_of_0(tmp_path: Path) -> None:
    script = tmp_path / "under_a_hard_limit.py"
    script.write_text(UNDER_A_HARD_LIMIT_OF_0)
    done = subprocess.run(
        [sys.executable, str(script), __file__],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=LIMIT,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "0 0\n"


# red_at_pretip (fix 8, R6-C of in/P6-review6-codex.md: under a hard core limit of 0 the
# soft limit is 0 too, and for 0 the kernel of the Linux node that was tested,
# a LAN Linux node, starts the core-dump program of a piped core_pattern for every process
# that SIGQUIT ends). The two programs of the tests that SIGQUIT ends with its default
# action, TREE (the command of the tree test) and HOLD (the command of the alone test and
# the process below TREE), make themselves not dumpable after their last exec and before
# they say that they are ready. The test reads the owner of /proc/<pid>/status, which
# proc_pid(5) says is root:root for the files of a process whose "dumpable" attribute is
# not 1 (measured here: the folder /proc/<pid> itself keeps the user's owner, the files in
# it change; out/scratch/r8/probe_dumpable.py of fix 8): that is visible from
# outside at the moment of the ready line, which other tests pin as it is, while
# PR_GET_DUMPABLE reports only the calling process's own state. No process gets a signal.
def test_programs_that_sigquit_can_end_are_not_dumpable_when_they_are_ready(
    start: Callable[..., Wrapped], hold: Callable[[str], list[str]], tmp_path: Path
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("the dumpable attribute and its /proc owner are Linux's")
    if os.getuid() == 0:
        pytest.skip("run as root: /proc/<pid>/status is root's whether or not it is dumpable")
    script, looks = tmp_path / "tree.py", tmp_path / "looks.py"
    script.write_text(TREE)
    looks.write_text(LOOKS_FOR_THE_DESCRIPTOR)
    first = start(
        "run", "--", sys.executable, str(script), sys.executable, str(looks), *hold("first")
    )
    pid, parent, _group, _found = first.line("stdout").split(" ", 3)
    assert first.line("stdout") == "started"

    owners = {
        name: os.stat(f"/proc/{number}/status").st_uid
        for name, number in (("TREE", parent), ("HOLD", pid))
    }
    assert owners == {"TREE": 0, "HOLD": 0}
    first.release()
    assert first.wait() == 0


# A command that ignores the signals named in its first argument, and so does the
# process that it starts and waits for: what is ignored stays ignored in a new program.
STUBBORN = """\
import signal
import subprocess
import sys

for name in sys.argv[1].split(","):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
sys.exit(subprocess.call(sys.argv[2:]))
"""


# red_at_pretip of fix 2 (minor 2 of in/P6-linux-2.md): after a signal that the wrapper
# passed on, a command that ignores it gets SIGTERM and then SIGKILL, and so does every
# process that is still in its process group. Since fix 3 the signal goes to the group
# and the wrapper ends with the command, so the test waits for the other process of the
# group to end and no longer says that it ended first. The two settle times are made
# short; the result does not depend on them: these processes never end on their own.
#
# pins_existing (fix 5, R5-D of in/P6-review5-codex.md): fix 2 started both cases from
# SIGINT. The contract paragraph says "the signal itself, then SIGTERM, then SIGKILL" of
# each of the four, so every first signal is run here, and the test pins which steps the
# wrapper says it sends, in their order. When the first signal is SIGTERM itself, the
# second step is SIGTERM once more: a command that ignores SIGTERM gets it twice and is
# ended by SIGKILL (one that does not ignore it is ended by the first SIGTERM, in
# test_signal_to_the_wrapper_alone_ends_the_command_and_frees_the_lock).
#
# fix 8, the note of in/P6-review6-codex.md: renamed from
# test_processes_that_ignore_the_signal_are_ended_before_the_lock_is_free; the test sees
# the follow-up signals and the end of the processes, and then that the lock is free, not
# an order between the two.
#
# pins_existing (fix 8, R6-B of in/P6-review6-codex.md, the reviewer's mutant
# omit-repeated-term: the messages and the exit code stay the same when the second SIGTERM
# is not sent). The test also records the calls of os.killpg that the tool makes (THROUGH
# with SUITE_LOCK_TEST_RECORD) and asserts the whole sequence: the first signal, then
# every step, the repeated SIGTERM included.
@pytest.mark.parametrize(
    ("first_signal", "ignored", "steps"),
    [
        (signal.SIGINT, "SIGINT", [signal.SIGTERM]),
        (signal.SIGINT, "SIGINT,SIGTERM", [signal.SIGTERM, signal.SIGKILL]),
        (signal.SIGHUP, "SIGHUP", [signal.SIGTERM]),
        (signal.SIGHUP, "SIGHUP,SIGTERM", [signal.SIGTERM, signal.SIGKILL]),
        (signal.SIGQUIT, "SIGQUIT", [signal.SIGTERM]),
        (signal.SIGQUIT, "SIGQUIT,SIGTERM", [signal.SIGTERM, signal.SIGKILL]),
        (signal.SIGTERM, "SIGTERM", [signal.SIGTERM, signal.SIGKILL]),
    ],
)
def test_processes_that_ignore_the_signal_get_the_follow_up_signals_and_end(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    first_signal: signal.Signals,
    ignored: str,
    steps: list[signal.Signals],
) -> None:
    script = tmp_path / "stubborn.py"
    script.write_text(STUBBORN)
    through = with_constants(tmp_path, TERM_AFTER_SECONDS=0.2, KILL_AFTER_SECONDS=0.2)
    command = [sys.executable, str(script), ignored, *hold("first")]
    calls = tmp_path / "calls.log"
    record = json.dumps({"calls": str(calls)})
    first = start(
        "run", "--", *command, through=through, environ={**env, "SUITE_LOCK_TEST_RECORD": record}
    )
    assert first.line("stdout") == "started"

    first.proc.send_signal(first_signal)

    assert first.wait() == 128 + steps[-1]
    # What the tool sent, each call of os.killpg as it was made.
    assert sent_signals(calls) == [f"killpg {sig.value}" for sig in (first_signal, *steps)]
    # The signal went to the whole group: the pipe ends, so the other process is gone.
    assert first.rest("stdout") == ""
    said = first.rest("stderr")
    sent = [line for line in said.splitlines() if "sending SIG" in line]
    assert [line.rsplit("sending ", 1)[1].split()[0] for line in sent] == [
        step.name for step in steps
    ]
    assert all(f"signal {first_signal.value} was passed on" in line for line in sent)
    # red_at_pretip (fix 5, R5-B of in/P6-review5-codex.md, the reviewer's case
    # escalation_exit_gap.py): the command can end between the loop's last look and the
    # message, so the message says only what that look found.
    assert all("the command had not ended at the last check" in line for line in sent)
    assert "still running" not in said
    assert events(log) == ["start first"]
    assert status_json(env) == free_report(lock)


# A command that says whether it inherited the signal named in its first argument as
# ignored, answers every line of its standard input, and ends with exit code 5 when that
# input closes.
ANSWERS = """\
import signal
import sys

name = sys.argv[1]
ignored = signal.getsignal(getattr(signal, name)) is signal.SIG_IGN
sys.stdout.write("started, %s ignored: %s\\n" % (name, ignored))
sys.stdout.flush()
for _line in sys.stdin:
    sys.stdout.write("still here\\n")
    sys.stdout.flush()
sys.exit(5)
"""


def ignoring(signum: signal.Signals) -> Callable[[], None]:
    """What a wrapper inherits that is started with `signum` ignored: a `&` job of a
    shell ignores SIGINT and SIGQUIT, a process under `nohup` SIGHUP, and a parent can
    set SIGTERM to SIG_IGN before it starts the tool."""

    def prepare() -> None:
        default_signals()
        signal.signal(signum, signal.SIG_IGN)

    return prepare


def next_pass(mark: int) -> None:
    """Wait until the tool's main loop has come round once more (a byte of THROUGH's
    checkpoint FIFO)."""
    if not select.select([mark], [], [], LIMIT)[0]:
        pytest.fail(f"the wrapper's loop did not come round within {LIMIT} s")
    assert os.read(mark, 1) == b"."


# pins_existing (fix 4, the finding of in/P6-linux-4.md). It pins the sentence of the
# contract paragraph "unless the wrapper inherited that signal as ignored: then it stays
# ignored, in the wrapper and in the command, and nothing follows it".
#
# SIGTERM, SIGHUP and SIGQUIT are pins_existing (fix 5, R5-D of in/P6-review5-codex.md):
# the paragraph names four signals, and fix 4 ran SIGINT only. A tool that installs its
# handler for one of them although it was inherited as ignored (the reviewer's mutants
# handle-ignored-SIGHUP, -SIGTERM and -SIGQUIT) gives the command that signal as not
# ignored, and the first line of the command says so.
#
# pins_existing (fix 8, R6-A of in/P6-review6-codex.md, the reviewer's mutant
# late-ignored-forward: a wrapper that stops ignoring the signal once the command has
# started, and passes it on, was not seen, because the command ignores it). Until fix 8
# the test slept one second after the signal; that showed neither that the wrapper had
# run in that second nor what it sent.
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_signal_that_the_wrapper_inherited_as_ignored_stays_ignored_and_nothing_follows(
    start: Callable[..., Wrapped],
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    """The wrapper is run through THROUGH, which writes down every signal that the tool
    sends (its os.killpg and os.kill) and writes a checkpoint byte at each call of
    select.select in the tool's main loop. The shim writes a checkpoint immediately before
    `select.select`, after this pass's signal-dispatch code. A readable select is followed
    by `os.read` to drain the wake pipe. In a pass of the loop `while child.poll() is None`
    the tool first sends what its handlers wrote down, then the follow-up whose time has
    come, and only then waits in select. Both follow-up times are 0 here, so a follow-up of
    a signal that was passed on is sent in the same pass: the constants, not elapsed time,
    say that its time has come.

    The test waits for a first checkpoint (the loop runs, the handlers are in place),
    sends the signal and then SIGCHLD, whose handler in the tool only wakes the loop. The
    first signal has been sent to the wrapper once kill(2) has returned, before SIGCHLD is
    sent; a handler for it (the reviewer's mutant has one) runs before the interpreter
    goes on with the loop that SIGCHLD wakes, so whatever it sends is written down before
    the second checkpoint (the mutant goes red at that point). At the second checkpoint
    the tool has made no call, and the command still runs and answers."""
    script = tmp_path / "answers.py"
    script.write_text(ANSWERS)
    through = with_constants(tmp_path, TERM_AFTER_SECONDS=0.0, KILL_AFTER_SECONDS=0.0)
    calls, checkpoint = tmp_path / "calls.log", tmp_path / "checkpoint"
    os.mkfifo(checkpoint)
    # Read and write: the shim's open for writing does not wait for a reader.
    mark = os.open(checkpoint, os.O_RDWR)
    try:
        record = json.dumps({"calls": str(calls), "checkpoint": str(checkpoint)})
        first = start(
            *("run", "--", sys.executable, str(script), signum.name),
            environ={**env, "SUITE_LOCK_TEST_RECORD": record},
            through=through,
            prepare=ignoring(signum),
        )
        assert first.line("stdout") == f"started, {signum.name} ignored: True"
        next_pass(mark)

        first.proc.send_signal(signum)
        first.proc.send_signal(signal.SIGCHLD)
        next_pass(mark)

        assert sent_signals(calls) == []
        assert first.proc.poll() is None
        assert first.proc.stdin is not None
        first.proc.stdin.write(b"are you there\n")
        assert first.line("stdout") == "still here"
        first.release()
        assert first.wait() == 5
    finally:
        os.close(mark)
    assert sent_signals(calls) == []
    said = first.rest("stderr")
    assert "was passed on" not in said and "sending SIG" not in said
    assert status_json(env) == free_report(lock)


# T-129, the same sentence of the contract. A changed tool that the test above does not
# see: once the command runs it installs a handler for the signal that it inherited as
# ignored, and the handler does not send. It writes down a time a fraction of a second
# later, the loop asks select.select to wake it at that time, and the pass after that
# sends the signal to the command's group, with no follow-up and no message. In the test
# above the command has answered, ended and been waited for before that time comes, so
# nothing is ever sent. Here the tool's clock is the shim's ("clock" of THROUGH), and
# the command is held until the pass in which such a send is due has come round.
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_signal_inherited_as_ignored_is_not_passed_on_later_either(
    start: Callable[..., Wrapped],
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    """No length of time is waited for: every wait is for a checkpoint byte or a line of
    the command, bounded by LIMIT. The follow-up times stay as the tool has them, so no
    constant of this test says when a deferred action is due.

    Passes of the tool's loop, each shown by its checkpoint byte:

    1. The loop runs and waits in select. The test sends the signal and then SIGCHLD,
       which wakes the loop. A handler for the signal (the tool has none) runs before the
       loop goes on.
    2. The first pass after the signal. A changed tool that sends at once has sent here.
       One that defers has a time written down, and here it calls select: with a timeout
       the shim moves the clock on by that timeout and returns without sleeping; with
       none the shim has moved the clock on by CLOCK_STEP_SECONDS and waits. The test
       sends SIGCHLD once more, which ends that wait.
    3. The pass in which the deferred send is due, by either way, and is made before the
       checkpoint byte is written. The tool as it is has no handler, called select with
       no timeout both times, and has sent nothing.

    The command still runs and answers after pass 3; only then its input is closed."""
    script = tmp_path / "answers.py"
    script.write_text(ANSWERS)
    through = with_constants(tmp_path)
    calls, checkpoint = tmp_path / "calls.log", tmp_path / "checkpoint"
    os.mkfifo(checkpoint)
    # Read and write: the shim's open for writing does not wait for a reader.
    mark = os.open(checkpoint, os.O_RDWR)
    try:
        record = json.dumps({"calls": str(calls), "checkpoint": str(checkpoint), "clock": True})
        first = start(
            *("run", "--", sys.executable, str(script), signum.name),
            environ={**env, "SUITE_LOCK_TEST_RECORD": record},
            through=through,
            prepare=ignoring(signum),
        )
        assert first.line("stdout") == f"started, {signum.name} ignored: True"
        next_pass(mark)

        first.proc.send_signal(signum)
        first.proc.send_signal(signal.SIGCHLD)
        next_pass(mark)
        first.proc.send_signal(signal.SIGCHLD)
        next_pass(mark)

        # The command was held up to here: it has not ended, and it answers.
        assert first.proc.poll() is None
        assert first.proc.stdin is not None
        first.proc.stdin.write(b"are you there\n")
        assert first.line("stdout") == "still here"
        assert sent_signals(calls) == []
        first.release()
        assert first.wait() == 5
    finally:
        os.close(mark)
    assert sent_signals(calls) == []
    said = first.rest("stderr")
    assert "was passed on" not in said and "sending SIG" not in said
    assert status_json(env) == free_report(lock)


# pins_existing (fix 2: a signal to the outer wrapper ends the command of the run below
# it as well, and both locks are free after it)
def test_signal_to_the_outer_wrapper_ends_the_command_of_the_run_below_it(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
) -> None:
    first = start("run", "--", *nested("run", "--", *hold("inner")))
    assert first.line("stdout") == "started"

    first.proc.send_signal(signal.SIGTERM)

    assert first.wait() == 128 + signal.SIGTERM
    assert first.so_far("stdout") == ("", True)
    assert events(log) == ["start inner"]
    assert status_json(env) == free_report(lock)
    assert status_json({**env, PATH_ENV: f"{lock}.1"})["state"] == "free"


# A script for `through` of the `start` fixture: the tool with short settle times and
# with review 3's schedule `depart-after-final-check` (R3-B). The process named in the
# pid file leaves at the last moment before a signal that ends processes is sent: before
# SIGTERM or SIGKILL to a process group, and before any signal to its own process ID,
# which is after every look that the tool could have taken at it.
DEPART_SHIM = """\
import importlib.util
import json
import os
import select
import signal
import sys
import types

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("suite_lock_with_departure", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.TERM_AFTER_SECONDS = 0.2
module.KILL_AFTER_SECONDS = 0.2
# JSON, not a string with separators: a path can hold any of them (fix 8, R6-E).
departure = json.loads(os.environ["SUITE_LOCK_TEST_DEPART"])
pid_file, control, ack = departure["pid_file"], departure["control"], departure["ack"]
left = []


def depart():
    if left:
        return
    left.append(True)
    back = os.open(ack, os.O_RDWR)
    go = os.open(control, os.O_WRONLY)
    os.write(go, b"go\\n")
    os.close(go)
    select.select([back], [], [], 20)
    os.close(back)


def kill(pid, signum):
    with open(pid_file) as file:
        if signum != 0 and pid == int(file.read()):
            depart()
    return os.kill(pid, signum)


def killpg(group, signum):
    if signum in (signal.SIGTERM, signal.SIGKILL):
        depart()
    return os.killpg(group, signum)


module.os = types.SimpleNamespace(**{**vars(os), "kill": kill, "killpg": killpg})
sys.exit(module.main(sys.argv[2:]))
"""

# A command that ignores SIGINT and starts DEPARTS, which stays in its process group.
# Both end when the standard input closes, so no test can leave one of them behind.
DEPART_LEADER = """\
import signal
import subprocess
import sys

signal.signal(signal.SIGINT, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, *sys.argv[2:]])
with open(sys.argv[1], "w") as pid_file:
    pid_file.write(str(child.pid))
sys.stdout.write("started\\n")
sys.stdout.flush()
sys.stdin.read()
"""

# The process that leaves: when the control FIFO says so it starts a session of its own
# and answers on the other FIFO. It writes down every signal that reaches it.
DEPARTS = """\
import os
import select
import signal
import sys

log, control, ack = sys.argv[1:4]


def note(signum, _frame):
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, ("signal %d\\n" % signum).encode())
    os.close(fd)


for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(signum, note)
# Read and write: the FIFO then has a writer, and is readable only when one wrote.
go = os.open(control, os.O_RDWR)
if go in select.select([go, 0], [], [])[0]:
    os.setsid()
    back = os.open(ack, os.O_WRONLY)
    os.write(back, ("%d\\n" % os.getsid(0)).encode())
    os.close(back)
    sys.stdin.read()
"""


# red_at_pretip (fix 3, R3-B of in/P6-review3-codex.md, schedule depart-after-final-check:
# the tool of 71b0f30 asked for the session of a process and then signalled its process
# ID; the process that left in between got SIGTERM in its new session). The signal now
# goes to the process group that the wrapper made, while its leader is not waited for
# yet: a process that left that group cannot be reached by it.
def test_process_that_left_the_group_of_the_command_is_never_signalled(
    start: Callable[..., Wrapped],
    env: dict[str, str],
    tmp_path: Path,
) -> None:
    # A colon in the folder's name pins that no path is cut at one (fix 8, R6-E).
    files = tmp_path / "depart:files"
    files.mkdir()
    scripts = {"shim": DEPART_SHIM, "leader": DEPART_LEADER, "departs": DEPARTS}
    for name, text in scripts.items():
        (files / f"{name}.py").write_text(text)
    pid_file, control, ack = (files / name for name in ("departs.pid", "control", "ack"))
    os.mkfifo(control)
    os.mkfifo(ack)
    signals = files / "signals.log"
    departs = [str(files / "departs.py"), str(signals), str(control), str(ack)]
    departure = {"pid_file": str(pid_file), "control": str(control), "ack": str(ack)}
    first = start(
        *("run", "--", sys.executable, str(files / "leader.py"), str(pid_file), *departs),
        environ={**env, "SUITE_LOCK_TEST_DEPART": json.dumps(departure)},
        through=files / "shim.py",
    )
    assert first.line("stdout") == "started"
    left = int(pid_file.read_text())

    first.proc.send_signal(signal.SIGINT)

    # The command ignores SIGINT, so it is ended by the SIGTERM that follows.
    first.line_with("stderr", "sending SIGTERM")
    assert first.wait() == 128 + signal.SIGTERM
    # The process left (it leads a session of its own), runs, and got no signal.
    assert os.getsid(left) == left
    assert not signals.exists()
    assert first.so_far("stdout") == ("", False)

    first.release()
    assert first.rest("stdout") == ""
    assert not signals.exists()


# A script for `through` of the `start` fixture: the tool with a kill function that
# refuses. A signal for the wrapper arrives in the moment after it has waited for its
# command, and every signal that the tool sends from then on is written down and not
# sent: the process ID of the command is free for another process by then.
REAP_SHIM = """\
import importlib.util
import os
import signal
import subprocess
import sys
import types

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("suite_lock_with_refusing_kill", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
violations = os.environ["SUITE_LOCK_TEST_VIOLATIONS"]
reaped = []


class Popen(subprocess.Popen):
    def seen(self, code):
        if code is not None and not reaped:
            reaped.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
        return code

    def poll(self):
        return self.seen(super().poll())

    def wait(self, timeout=None):
        return self.seen(super().wait(timeout))


def refusing(name):
    def send(target, signum):
        if reaped and signum != 0:
            with open(violations, "a") as file:
                file.write("%s(%d, %d) after the reap\\n" % (name, target, signum))
            return None
        return getattr(os, name)(target, signum)

    return send


module.subprocess = types.SimpleNamespace(**{**vars(subprocess), "Popen": Popen})
module.os = types.SimpleNamespace(
    **{**vars(os), "kill": refusing("kill"), "killpg": refusing("killpg")}
)
sys.exit(module.main(sys.argv[2:]))
"""


# red_at_pretip (fix 3, R3-B: the handler of the tool of 71b0f30 passed a signal on to
# the process group of the command also after `wait` had returned its end, when the
# group ID could be another process's). Nothing is signalled after the reap now: the
# wrapper sends signals from its main loop only, between two looks at the command.
def test_no_signal_is_sent_after_the_wrapper_has_waited_for_its_command(
    start: Callable[..., Wrapped],
    env: dict[str, str],
    tmp_path: Path,
) -> None:
    shim = tmp_path / "shim.py"
    shim.write_text(REAP_SHIM)
    violations = tmp_path / "violations.log"
    first = start(
        *("run", "--", *exits_with(7)),
        environ={**env, "SUITE_LOCK_TEST_VIOLATIONS": str(violations)},
        through=shim,
    )

    assert first.wait() == 7
    assert not violations.exists(), violations.read_text()
    assert status_json(env)["state"] == "free"


# A script for `through` of the `start` fixture: the tool as it is, with its process
# group signal written down, and with one schedule (review 5, R5-D, the reviewer's case
# `exit-before-reap` of signal_attacks.py; in/review4-signal_attacks.py has its macOS
# form). At the first look of the main loop that finds the command running, the shim lets
# the command end and waits until the kernel reports that end, without waiting for the
# command: on Linux a pidfd that is readable and then `waitid` with WNOWAIT (a blocking
# `waitid` with WNOWAIT where the interpreter has no `os.pidfd_open`, fix 7), on macOS
# kqueue with NOTE_EXIT. Then SIGTERM reaches the wrapper, and that look returns what it
# found. Every step waits for an event: the open of the FIFO and its byte, the kernel's
# report, and the tool's own handler, which the shim notes when it has run.
WINDOW_SHIM = """\
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import types

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("suite_lock_with_exit_before_reap", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# JSON, not a string with separators: a path can hold any of them (fix 8, R6-E).
window = json.loads(os.environ["SUITE_LOCK_TEST_WINDOW"])
events, go, means = window["events"], window["go"], window["means"]
# How long the shim waits for each of its events (fix 8, R6-D).
bound = float(window.get("bound", 20))
looked = []
# The handler of the tool, wrapped by install() below, writes a byte here when it has run.
handled, handled_in = os.pipe()


class Late(Exception):
    pass


def late(_signum, _frame):
    raise Late("the bound of %s s passed" % bound)


def event(text):
    with open(events, "a") as file:
        file.write(text + "\\n")


def let_the_leader_end():
    fd = os.open(go, os.O_WRONLY)
    os.write(fd, b"x")
    os.close(fd)


def ended_and_not_waited_for(pid):
    if means == "linux" and not hasattr(os, "pidfd_open"):
        # Python 3.9 as uv installs it has no os.pidfd_open. waitid without WNOHANG
        # blocks until the kernel reports the end, and WNOWAIT leaves it not waited for;
        # the kernel timer that ended_in_time() sets ends the wait at the bound.
        let_the_leader_end()
        seen = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        assert seen is not None and seen.si_pid == pid, seen
        return "waitid WNOWAIT"
    if means == "linux":
        fd = os.pidfd_open(pid)
        try:
            let_the_leader_end()
            if not select.select([fd], [], [], bound)[0]:
                raise TimeoutError("the kernel did not report the end within %s s" % bound)
        finally:
            os.close(fd)
        seen = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        assert seen is not None and seen.si_pid == pid, seen
        return "pidfd readable, waitid WNOWAIT"
    queue = select.kqueue()
    try:
        flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE
        watch = select.kevent(pid, select.KQ_FILTER_PROC, flags, select.KQ_NOTE_EXIT)
        queue.control([watch], 0)
        let_the_leader_end()
        seen = queue.control(None, 1, bound)
        if not seen:
            raise TimeoutError("the kernel did not report the end within %s s" % bound)
        assert seen[0].fflags & select.KQ_NOTE_EXIT, seen
    finally:
        queue.close()
    return "kqueue NOTE_EXIT"


# ended_and_not_waited_for() under a kernel timer of the bound: the open of the FIFO and
# the blocking waitid have no timeout of their own.
def ended_in_time(pid):
    before = signal.signal(signal.SIGALRM, late)
    signal.setitimer(signal.ITIMER_REAL, bound)
    try:
        return ended_and_not_waited_for(pid)
    except Late as error:
        raise TimeoutError(str(error)) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


class Popen(subprocess.Popen):
    def poll(self):
        code = super().poll()
        if code is None and not looked:
            looked.append(True)
            try:
                how = ended_in_time(self.pid)
                event("the leader has ended and is not waited for (%s)" % how)
                os.kill(os.getpid(), signal.SIGTERM)
                # The handler of the tool runs at the interpreter's next check for
                # signals; select is interrupted by the signal, runs the handlers and
                # goes on waiting, and the wrapped handler writes the byte.
                if not select.select([handled], [], [], bound)[0]:
                    raise TimeoutError("the tool's handler did not run within %s s" % bound)
            except BaseException:
                # This shim's own direct child, not waited for yet, so its process ID is
                # still its own: end it and wait for it before this process ends.
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except OSError:
                    pass
                subprocess.Popen.wait(self)
                raise
        elif code is not None:
            event("waited for the leader: %d" % code)
        return code


def install(signum, handler):
    if signum == signal.SIGTERM and callable(handler):
        inner = handler

        def handler(number, frame):
            inner(number, frame)
            os.write(handled_in, b"h")

    return signal.signal(signum, handler)


def killpg(group, signum):
    event("killpg %d" % signum)
    return os.killpg(group, signum)


module.signal = types.SimpleNamespace(**{**vars(signal), "signal": install})
module.subprocess = types.SimpleNamespace(**{**vars(subprocess), "Popen": Popen})
module.os = types.SimpleNamespace(**{**vars(os), "killpg": killpg})
sys.exit(module.main(sys.argv[2:]))
"""

# The command of that schedule: it starts WINDOW_LEAF, waits until the leaf has its
# handler, and ends at once when the FIFO in its first argument gives it a byte, or when
# its standard input closes.
WINDOW_LEADER = """\
import os
import select
import subprocess
import sys

ready, ready_in = os.pipe()
subprocess.Popen([sys.executable, sys.argv[2], str(ready_in)], pass_fds=(ready_in,))
os.close(ready_in)
assert os.read(ready, 1) == b"r"
# Read and write: the open does not wait for a writer, and the FIFO is readable only when
# one wrote. The end of standard input ends the leader too (fix 7): a shim that fails
# before it writes the byte leaves no leader behind.
go = os.open(sys.argv[1], os.O_RDWR)
select.select([go, 0], [], [])
os._exit(0)
"""

# A process that stays in the command's process group (it closed the lock descriptor, as
# Python's subprocess does) and writes every SIGTERM that reaches it to its standard
# output. It ends when its standard input closes.
WINDOW_LEAF = """\
import os
import signal
import sys


def received(signum, _frame):
    sys.stdout.write("received %d\\n" % signum)
    sys.stdout.flush()


signal.signal(signal.SIGTERM, received)
ready = int(sys.argv[1])
os.write(ready, b"r")
os.close(ready)
sys.stdin.read()
"""


def exit_without_wait_means() -> str | None:
    """How this system reports that a child has ended without waiting for it: on Linux
    `waitid` with WNOWAIT, after a pidfd that became readable where the interpreter of
    the tool has `os.pidfd_open` (Python 3.9 as uv installs it has not); on macOS kqueue
    with NOTE_EXIT."""
    if sys.platform.startswith("linux") and hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        return "linux"
    if hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_EXIT"):
        return "kqueue"
    return None


# pins_existing (fix 5, R5-D of in/P6-review5-codex.md: the reviewer's schedule
# exit-before-reap, which signal_attacks.py of review 5 runs on Linux and
# in/review4-signal_attacks.py on macOS).
def test_a_process_still_in_the_group_can_get_a_signal_after_the_end_of_the_command(
    start: Callable[..., Wrapped],
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
) -> None:
    """Pins the sentence of the contract paragraph "Between the end of the leader and
    that wait, a process that is still in the group can receive a signal".

    The kernel reports that the command has ended and that nobody has waited for it; then
    a signal reaches the wrapper, which passes it on to the group, and a process that is
    still in the group gets it. The sentence says "can", so this test records that the
    tool sends in this window, as it does at 45638e7. It does not pin that a signal must
    be sent there. A tool that looked again before it sends would turn this test red, and
    that is on purpose: decision D-20261120-11 rejects a fix whose only proof is a look
    followed by an act, and such a look leaves the same window between itself and
    `killpg`. Nothing in the tool is changed for this test; the shim wraps `poll`,
    `signal.signal` and `killpg` of the tool's module.
    """
    means = exit_without_wait_means()
    if means is None:
        pytest.skip(
            "this system has neither waitid with WNOWAIT (Linux) nor kqueue with NOTE_EXIT "
            "(macOS): no means to learn that a child has ended without waiting for it"
        )
    # A colon in the folder's name pins that no path is cut at one (fix 8, R6-E).
    files = tmp_path / "window:files"
    files.mkdir()
    scripts = {"shim": WINDOW_SHIM, "leader": WINDOW_LEADER, "leaf": WINDOW_LEAF}
    for name, text in scripts.items():
        (files / f"{name}.py").write_text(text)
    events_file, go = files / "window.log", files / "go"
    os.mkfifo(go)
    window = {"events": str(events_file), "go": str(go), "means": means}
    first = start(
        *("run", "--", sys.executable, str(files / "leader.py"), str(go)),
        str(files / "leaf.py"),
        environ={**env, "SUITE_LOCK_TEST_WINDOW": json.dumps(window)},
        through=files / "shim.py",
    )

    # The command ended with 0. The events are in the order in which they happened.
    assert first.wait() == 0
    hows = ["pidfd readable, waitid WNOWAIT", "waitid WNOWAIT"]
    seen = events_file.read_text().splitlines()
    assert seen[0] in [
        f"the leader has ended and is not waited for ({how})"
        for how in (hows if means == "linux" else ["kqueue NOTE_EXIT"])
    ]
    assert seen[1:] == [
        f"killpg {signal.SIGTERM.value}",
        "waited for the leader: 0",
    ]
    # The process in the group got it, after the end of the command.
    assert first.line("stdout") == f"received {signal.SIGTERM.value}"
    first.release()
    assert first.rest("stdout") == ""
    assert status_json(env) == free_report(lock)


# The leader of review 6's case window-never-ends (R6-D): WINDOW_LEADER, except that it
# opens the FIFO in its third argument for writing and writes one byte to it, and then
# never ends: it reads a pipe whose write end it holds itself, and does not read its
# standard input. Only the leader holds that FIFO open for writing.
NEVER_ENDING_LEADER = """\
import os
import subprocess
import sys

ready, ready_in = os.pipe()
subprocess.Popen([sys.executable, sys.argv[2], str(ready_in)], pass_fds=(ready_in,))
os.close(ready_in)
assert os.read(ready, 1) == b"r"
lives = os.open(sys.argv[3], os.O_WRONLY)
os.write(lives, b"l")
go = os.open(sys.argv[1], os.O_RDWR)
never, held = os.pipe()
os.read(never, 1)
"""


# red_at_pretip (fix 8, R6-D of in/P6-review6-codex.md, case window-never-ends: the
# shim of c09af50 waited for the end of its command for 20 s, or without a bound with
# Python 3.9, and then failed and left the command running).
def test_window_shim_ends_its_command_when_the_command_does_not_end(
    start: Callable[..., Wrapped], env: dict[str, str], tmp_path: Path
) -> None:
    """The shim of the window test waits for the end of its command for the bound in its
    JSON value (2 s here), and when the bound passes, or anything else fails, it ends its
    own direct child and waits for it before its process ends, while that child is not
    waited for yet and its process ID is still its own. The wrapper ends with a failure,
    and the end of the FIFO that only the command held open shows that the command is gone
    when the wrapper has ended; the leaf ends when its standard input closes."""
    means = exit_without_wait_means()
    if means is None:
        pytest.skip(
            "this system has neither waitid with WNOWAIT (Linux) nor kqueue with NOTE_EXIT "
            "(macOS): the shim has no wait of its own to bound"
        )
    files = tmp_path / "window:files"
    files.mkdir()
    scripts = {"shim": WINDOW_SHIM, "leader": NEVER_ENDING_LEADER, "leaf": WINDOW_LEAF}
    for name, text in scripts.items():
        (files / f"{name}.py").write_text(text)
    events_file, go, lives = files / "window.log", files / "go", files / "lives"
    os.mkfifo(go)
    os.mkfifo(lives)
    # The test is the only reader; the leader becomes the only writer.
    alive = os.open(lives, os.O_RDONLY | os.O_NONBLOCK)
    try:
        window = {"events": str(events_file), "go": str(go), "means": means, "bound": 2.0}
        first = start(
            *("run", "--", sys.executable, str(files / "leader.py"), str(go)),
            *(str(files / "leaf.py"), str(lives)),
            environ={**env, "SUITE_LOCK_TEST_WINDOW": json.dumps(window)},
            through=files / "shim.py",
        )
        assert select.select([alive], [], [], LIMIT)[0], "the leader did not start"
        assert os.read(alive, 1) == b"l"

        assert first.wait() != 0
        # When the wrapper has ended, no process has the FIFO open for writing any more.
        assert select.select([alive], [], [], 0)[0], "the leader still runs"
        assert os.read(alive, 1) == b""
    finally:
        os.close(alive)
    # The leaf ends when its standard input closes; then no process has the output pipes.
    first.release()
    assert first.rest("stdout") == ""
    said = first.rest("stderr")
    assert "TimeoutError" in said, said


def load_tool() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("suite_lock_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# red_at_pretip (fix 1, finding 7, case scheduler-pause-test of in/extra_checks.py: the
# process is not scheduled between its first look at the lock and its first message)
def test_bounded_wait_names_the_holder_once_however_late_the_first_look_is(
    lock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_tool()
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    now = [100.0]

    def pause(_seconds: float) -> None:
        # Every pause lasts longer than the whole bound of the wait.
        now[0] += 0.6

    clock = types.SimpleNamespace(
        monotonic=lambda: now[0],
        sleep=pause,
        time=time.time,
        strftime=time.strftime,
        gmtime=time.gmtime,
    )
    monkeypatch.setattr(module, "time", clock)
    held = os.open(lock, os.O_RDONLY)
    waiting = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert module.acquire(waiting, 0.5) is False
    finally:
        os.close(held)
        os.close(waiting)

    said = capsys.readouterr().err
    assert said.count("waiting for") == 1
    assert said.count("busy after 0.5 s") == 1
    assert said.index("waiting for") < said.index("busy after")


# red_at_pretip (fix 1, finding 8: what the private recipe `_check` asks first)
def test_require_passes_only_below_a_run_that_holds_the_lock(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    env: dict[str, str],
    lock: Path,
) -> None:
    alone = tool(env, "require")
    assert alone.returncode == NOT_HELD
    assert "not below a run that holds the suite lock" in alone.stderr
    assert not lock.parent.exists()

    assert tool(env, "run", "--", *nested("require")).returncode == 0
    shell = shlex.join(nested("require")) + "; exit $?"
    assert tool(env, "run", "--", "sh", "-c", shell).returncode == 0

    # Beside a holder, with the marker of that holder copied but not its descriptor.
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    identity = lock.stat()
    copied = {**env, HELD_ENV: f"{first.pid}/3/{identity.st_dev}/{identity.st_ino}"}
    assert tool(copied, "require").returncode == NOT_HELD
    assert tool(env, "require").returncode == NOT_HELD


# red_at_pretip (fix 1, finding 2: the record says which processes to look for when the
# wrapper is gone)
def test_record_names_the_process_group_of_the_command(
    start: Callable[..., Wrapped], hold: Callable[[str], list[str]], env: dict[str, str]
) -> None:
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    holder = status_json(env)["holder"]
    assert isinstance(holder, dict)
    group = holder["command_pid"]
    assert group != first.pid
    # The command leads a process group and a session of its own.
    assert os.getpgid(group) == group
    assert os.getsid(group) == group
    assert f"command pid: {group}" in tool(env, "status").stdout


# A command that starts a run of its own the way most programs do: Python's subprocess
# closes every descriptor above 2 in the process it starts.
# Before that it can change the holder record in the lock file, which any process of
# the user can do.
DROPS_DESCRIPTORS = """\
import os
import subprocess
import sys

record = {"empty": "", "corrupt": "not json"}.get(sys.argv[1])
if record is not None:
    with open(os.environ["ANSWERBACK_SUITE_LOCK"], "w") as lock_file:
        lock_file.write(record)
sys.exit(subprocess.call(sys.argv[2:]))
"""


# red_at_pretip (fix 1, finding 1: 607e026 let this run in by its process ancestry; now
# the descriptor is the only proof, and a run that cannot prove it is refused at once,
# because this run and the holder can wait for each other without end: they do when
# the holder's command waits for this run, which the tool does not ask; fix 4, R4-D)
#
# Fix 2, R2-B (cases missing-record-nested of in/attack_fixes.py and readonly-recursive
# of in/more_attacks.py): c6b5a31 refused only when the record in the lock file named
# the holder, so with an empty or corrupt record, or a lock file that cannot be written,
# the inner run waited for the outer one, which waited for it. "record" is red there
# for the exit code alone, which is now the refusal's own.
@pytest.mark.parametrize("record", ["record", "empty", "corrupt", "read-only"])
def test_run_below_the_holder_without_its_descriptor_is_refused_and_does_not_wait(
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    lock: Path,
    tmp_path: Path,
    record: str,
) -> None:
    script = tmp_path / "drops.py"
    script.write_text(DROPS_DESCRIPTORS)
    if record == "read-only":
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        lock.chmod(0o400)
    # No --no-wait and no --wait: a deadlock would end in the timeout of `tool`.
    inner = nested("run", "--", *hold("inner"))

    done = tool(env, "run", "--", sys.executable, str(script), record, *inner)

    assert done.returncode == NO_DESCRIPTOR, done.stderr
    assert "does not have that run's lock descriptor" in done.stderr
    assert "nothing was run" in done.stderr
    assert "which started this run" not in done.stderr
    assert events(log) == []
    assert status_json(env)["state"] == "free"


# pins_existing (fix 2, R2-B: the rule above is for the lock file that the environment
# names. A run below a holder of another lock file waits like any other run, with or
# without descriptors, and gets the lock when it is free)
def test_run_below_a_holder_of_another_lock_file_waits_like_any_other_run(
    start: Callable[..., Wrapped],
    hold: Callable[[str], list[str]],
    log: Path,
    env: dict[str, str],
    tmp_path: Path,
) -> None:
    script = tmp_path / "drops.py"
    script.write_text(DROPS_DESCRIPTORS)
    first = start("run", "--", *hold("first"))
    assert first.line("stdout") == "started"
    other = {**env, PATH_ENV: str(tmp_path / "other" / "suite.lock")}
    inner = ["/usr/bin/env", f"{PATH_ENV}={env[PATH_ENV]}", *nested("run", "--")]

    second = start(
        *("run", "--", sys.executable, str(script), "record", *inner, *hold("second")),
        environ=other,
    )
    second.line_with("stderr", "waiting for")
    assert events(log) == ["start first"]
    first.release()
    assert second.line("stdout") == "started"
    assert events(log) == ["start first", "end first", "start second"]


# red_at_pretip (fix 1, finding 1: each run below a holder takes the lock of the next
# level; 607e026 passes the exit code on but makes no lock file for a level)
def test_runs_three_levels_deep_each_take_the_lock_of_their_own_level(
    env: dict[str, str], lock: Path
) -> None:
    innermost = nested("run", "--no-wait", "--", *exits_with(3))
    middle = nested("run", "--no-wait", "--", *innermost)

    done = tool(env, "run", "--", *middle)

    assert done.returncode == 3
    assert done.stderr.count("already held") == 2
    for level in (lock, Path(f"{lock}.1"), Path(f"{lock}.2")):
        assert level.read_text() == ""
    assert not Path(f"{lock}.3").exists()
    assert status_json(env) == free_report(lock)
