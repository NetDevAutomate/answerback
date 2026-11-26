#!/usr/bin/env python3
"""One whole test suite per machine and OS user, kept by a lock the kernel releases.

    python3 tools/suite_lock.py run [--wait SECONDS | --no-wait] -- <command...>
    python3 tools/suite_lock.py status [--json]
    python3 tools/suite_lock.py require

`run` takes the lock, runs the command and exits with the command's exit code. `status`
says who holds the lock, or that it is free, and whether it found a problem with the
folder of the lock file (a folder that is not there is not checked); it never waits for
the lock. `require` exits 0 only below a `run` that holds the lock: a recipe that must
not start on its own asks it first.

The lock is `flock` on an open file, and the kernel releases it: no stale lock exists.

The contract, and all of it. The suite lock is cooperative exclusion inside one OS
account on one machine. It is not a security boundary: a process of the same account
can close the descriptor, lock the same file or signal another run.

A run holds the lock for as long as some process still has the open file description
that `run` locked: the wrapper, its command, and every process that inherited the
descriptor and kept it. Nothing else is claimed. A process that closed the descriptor
(the default of Python's `subprocess`) is not covered, and may still run when the next
suite starts. A process that kept the descriptor keeps the lock after the command has
ended; a run that waits names the holder and waits, or gives up after `--wait`.

`SIGINT`, `SIGTERM`, `SIGHUP` or `SIGQUIT` that reaches the wrapper is passed on to the
process group that the wrapper created, unless the wrapper inherited that signal as
ignored: then it stays ignored, in the wrapper and in the command, and nothing follows
it. The group is signalled (the signal itself, then SIGTERM, then SIGKILL) only until
the wrapper has waited for its direct child, the leader of the group. Between the end
of the leader and that wait, a process that is still in the group can receive a signal.
After the wait nothing is signalled. A process that left the group is never signalled.

The check of the lock's folder is placement hygiene, by mode only, and not a defence;
the folders above it are trusted.

The holder writes a record of itself into the same file. The record is for people and
for probes: it is never the lock, nothing in it is verified, and a record that an ended
holder left behind blocks nobody.

Standard library only. It runs on Python 3.9 (the system Python of macOS) and later.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pwd
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from typing import NamedTuple

# Exit codes of the wrapper itself. A command can end with the same numbers, and a run
# that the command starts writes "suite lock:" lines to the same standard error. So
# neither the number nor that text says that this run started nothing.
BUSY = 75  # EX_TEMPFAIL: the wait ended without the lock and nothing was run.
USAGE = 64  # EX_USAGE
NO_LOCK_FILE = 73  # EX_CANTCREAT: the lock file cannot be opened, made or trusted.
# EX_PROTOCOL: the lock is held, the environment names a run as a holder of this lock
# file, and that run's lock descriptor is not here. Nothing was run.
NO_DESCRIPTOR = 76
NOT_HELD = 77  # EX_NOPERM: `require` was not started below a run that holds the lock.
NOT_FOUND = 127
NOT_RUNNABLE = 126
INTERRUPTED = 130

# Names another lock file. For tests, and for an account whose home cannot be written.
PATH_ENV = "ANSWERBACK_SUITE_LOCK"
# Given to the command by the holder: "<pid>/<lock descriptor>/<device>/<inode>", one
# entry for each run above the command, the outermost first, with commas between. The
# lock descriptor is the proof. The process ID is only a name for
# messages. Device and inode say which lock file the entry is about, so that a run can
# tell "the run above me holds this file" from "it holds another one" when the
# descriptor did not arrive.
HELD_ENV = "ANSWERBACK_SUITE_LOCK_HELD"

POLL_SECONDS = 0.2
# A run that waits for the lock says so at most this often.
REPORT_SECONDS = 60.0
# After a signal that the wrapper passed on: a command that still runs after
# TERM_AFTER_SECONDS gets SIGTERM, and one that still runs KILL_AFTER_SECONDS after that
# gets SIGKILL, each sent to its process group. Nothing is sent once the command has
# been waited for.
TERM_AFTER_SECONDS = 5.0
KILL_AFTER_SECONDS = 10.0
# A `status` probe holds a shared lock for an instant. One more look after this pause
# keeps that instant from reading as "busy".
SECOND_LOOK_SECONDS = 0.05
RECORD_RETRY_SECONDS = 0.02
RECORD_BYTES = 65536
# How often a run follows a lock file that is replaced under it before it gives up.
REOPEN_LIMIT = 5
PID_LIMIT = 2**31
EPOCH_LIMIT = 1e11
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


class Refusal(Exception):
    """The run ends here with `code`. `text` is said first, unless it was said already."""

    def __init__(self, code: int, text: str | None = None) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


def lock_path(environ: dict[str, str]) -> str:
    """The lock file of this OS user on this machine.

    The home comes from the account database, not from HOME or XDG_STATE_HOME: tests and
    sandboxes change those, and then two runs would not meet at one file.
    """
    chosen = environ.get(PATH_ENV)
    if chosen:
        return os.path.abspath(chosen)
    home = pwd.getpwuid(os.getuid()).pw_dir
    return os.path.join(home, ".local", "state", "answerback", "suite.lock")


def say(text: str) -> None:
    sys.stderr.write(f"suite lock: {text}\n")
    sys.stderr.flush()


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def plain(text: object, limit: int = 300) -> str:
    """Text from a record, safe to print: one line, no control character, bounded."""
    cleaned = "".join(char if char.isprintable() else "?" for char in str(text)[: limit + 1])
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def try_lock(fd: int, kind: int) -> bool:
    try:
        fcntl.flock(fd, kind | fcntl.LOCK_NB)
    except OSError:
        # Held by another open file, or a descriptor that cannot be locked at all.
        return False
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def process_id(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < PID_LIMIT:
        return value
    return None


def parse_record(data: bytes) -> dict[str, object] | None:
    """The holder record in `data`, or None when the text is not one.

    The file is not trusted: any process of this user can write it. Every field is
    checked for its type and its range, and nothing here raises for any content.
    """
    try:
        raw = json.loads(data.decode())
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = process_id(raw.get("pid"))
    if pid is None:
        return None
    started = raw.get("started")
    epoch = raw.get("started_epoch")
    command = raw.get("command")
    cwd = raw.get("cwd")
    command_ok = isinstance(command, list) and all(isinstance(part, str) for part in command)
    # The comparison is exact for an integer of any size, and false for NaN.
    epoch_ok = (
        isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and 0 <= epoch < EPOCH_LIMIT
    )
    return {
        "pid": pid,
        "command_pid": process_id(raw.get("command_pid")),
        "started": started if isinstance(started, str) else None,
        "started_epoch": float(epoch) if epoch_ok else None,
        "command": command if command_ok else None,
        "cwd": cwd if isinstance(cwd, str) else None,
    }


def read_record(fd: int, expected: bool) -> dict[str, object] | None:
    """Read the record. A holder may be writing it right now: look twice."""
    for attempt in (0, 1):
        data = os.pread(fd, RECORD_BYTES, 0)
        record = parse_record(data) if data.strip() else None
        if record is not None or attempt or not (data.strip() or expected):
            return record
        time.sleep(RECORD_RETRY_SECONDS)
    return None


def record_pid(record: dict[str, object]) -> int:
    pid = record["pid"]
    return pid if isinstance(pid, int) else 0


def command_text(record: dict[str, object]) -> str:
    command = record["command"]
    return plain(shlex.join(command)) if isinstance(command, list) else "?"


def describe(record: dict[str, object] | None) -> str:
    if record is None:
        return "a holder that has no record in the lock file"
    started = plain(record["started"] or "?", 40)
    cwd = plain(record["cwd"] or "?")
    run = f"pid {record_pid(record)} (started {started}, in {cwd}): {command_text(record)}"
    if alive(record_pid(record)):
        return run
    return f"processes of a run that has ended; the record in the lock file is of {run}"


def holder_text(fd: int) -> str:
    """Who holds the lock, for a message. What the file says never decides anything, so
    no content of it may end the run in another way than the lock does."""
    try:
        return describe(read_record(fd, expected=True))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return "a holder whose record in the lock file cannot be read"


def kind_of(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISDIR(mode):
        return "a folder"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    return "a special file"


def open_lock(path: str, create: bool, folder: int | None = None) -> tuple[int, bool]:
    """Open the lock file and return the descriptor and whether it can be written.

    The path itself is opened, never what a symbolic link there points to, and only a
    regular file of this user is accepted: the record is written through the descriptor,
    and it must not land in a file that somebody else chose. Nothing here waits: a FIFO
    at the path is refused, not opened for reading. A missing file is made when `create`
    is set, and is a FileNotFoundError when it is not. With `folder`, the descriptor of
    the folder of the path, the file is opened by its name in that folder, whatever is
    at the path of the folder by then.
    """
    name = path if folder is None else os.path.basename(path)
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    writable = create
    try:
        try:
            before = os.lstat(name, dir_fd=folder)
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise Refusal(NO_LOCK_FILE, f"{path} is {kind_of(before.st_mode)}, not a lock file")
        if create:
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | flags, 0o600, dir_fd=folder)
            except PermissionError:
                # The lock needs an open file, not a writable one.
                writable = False
                fd = os.open(name, os.O_RDONLY | flags, dir_fd=folder)
        else:
            fd = os.open(name, os.O_RDONLY | flags, dir_fd=folder)
    except FileNotFoundError:
        if create:
            raise Refusal(NO_LOCK_FILE, f"cannot make the lock file {path}") from None
        raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise Refusal(NO_LOCK_FILE, f"{path} is a symbolic link, not a lock file") from None
        raise Refusal(NO_LOCK_FILE, f"cannot open the lock file {path}: {error}") from None
    found = os.fstat(fd)
    problem = None
    if not stat.S_ISREG(found.st_mode):
        problem = f"{path} is {kind_of(found.st_mode)}, not a lock file"
    elif found.st_uid != os.getuid():
        problem = f"{path} belongs to another user (uid {found.st_uid})"
    elif found.st_nlink > 1:
        # The file has another name. It can be somebody's file with other content, which
        # the record would replace, or the lock of a running holder that a backup gave a
        # second name: the tool cannot tell. A name added after this look is not seen.
        problem = (
            f"{path} has {found.st_nlink} names (a hard link): the holder record would "
            "be written into the file under its other name too. Do not delete or replace "
            "the lock path: a run may hold it, and a run that starts after that would "
            "not see it. Either remove the other name when no run is active, or set "
            f"{PATH_ENV} to another path for every run"
        )
    if problem is not None:
        os.close(fd)
        raise Refusal(NO_LOCK_FILE, problem)
    return fd, writable


def open_for_run(path: str) -> tuple[int, bool]:
    """Open the lock file for a run, making it and its folder when needed."""
    folder = os.path.dirname(path)
    try:
        try:
            where = open_folder(folder)
        except FileNotFoundError:
            # Every folder that is made here is for this user alone, whatever the umask is.
            umask = os.umask(0o077)
            try:
                os.makedirs(folder, mode=0o700, exist_ok=True)
            finally:
                os.umask(umask)
            where = open_folder(folder)
    except OSError as error:
        raise Refusal(NO_LOCK_FILE, f"cannot use the folder {folder}: {error}") from None
    try:
        return open_lock(path, create=True, folder=where)
    finally:
        os.close(where)


def open_folder(folder: str) -> int:
    """Open the folder of the lock file and return its descriptor, or raise Refusal. A
    folder that is not there is a FileNotFoundError.

    The folder itself is opened, never what a symbolic link at its name points to. It
    must belong to this user, and its mode must not let its group or everybody write.
    That is asked of the open folder, and the lock file is then opened relative to the
    same descriptor: the folder that was checked is the folder that is used.

    This is placement hygiene and no defence. Only the mode is read, no access control
    list. The folders above this one are trusted, and symbolic links among them are
    followed. The rule is the same for every lock path.
    """
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as error:
        try:
            # Only for the wording: the open above has refused the folder already.
            link = stat.S_ISLNK(os.lstat(folder).st_mode)
        except OSError:
            link = False
        if not link:
            raise Refusal(NO_LOCK_FILE, f"cannot use the folder {folder}: {error}") from None
        raise Refusal(
            NO_LOCK_FILE,
            f"the folder {folder} is a symbolic link, and the lock file needs a folder "
            "that cannot be pointed somewhere else. Name the folder itself",
        ) from None
    found = os.fstat(fd)
    problem = None
    if found.st_uid != os.getuid():
        problem = (
            f"the folder {folder} belongs to another user (uid {found.st_uid}). The "
            "lock file needs a folder of this account"
        )
    elif found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        problem = (
            f"the folder {folder} can be written by its group or by everybody (mode "
            f"{stat.S_IMODE(found.st_mode):o}). The lock file needs a folder whose mode "
            f"lets this account alone write. Run `chmod go-w {shlex.quote(folder)}`"
        )
    if problem is not None:
        os.close(fd)
        raise Refusal(NO_LOCK_FILE, problem)
    return fd


def same_file(fd: int, path: str) -> bool:
    """Whether the open file is still the file at the path."""
    held = os.fstat(fd)
    try:
        there = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return held.st_nlink > 0 and (held.st_dev, held.st_ino) == (there.st_dev, there.st_ino)


def acquire(fd: int, wait: float | None, asked: float | None = None) -> bool:
    """Take the lock. `wait` is None for no bound, else the seconds to wait. `asked` is
    the bound to name in the message when `wait` is only what is left of it."""
    began = time.monotonic()
    deadline = None if wait is None else began + wait
    if try_lock(fd, fcntl.LOCK_EX):
        return True
    time.sleep(SECOND_LOOK_SECONDS)
    reported: float | None = None
    while True:
        if try_lock(fd, fcntl.LOCK_EX):
            if reported is not None:
                say(f"acquired after {time.monotonic() - began:.0f} s")
            return True
        now = time.monotonic()
        # The report comes before the look at the deadline: a wait with a bound names
        # the holder once, however late this process got here. `--no-wait` names it in
        # the one line that says "busy".
        if wait != 0 and (reported is None or now - reported >= REPORT_SECONDS):
            say(f"waiting for {holder_text(fd)}")
            reported = now
        if deadline is not None and now >= deadline:
            bound = wait if asked is None else asked
            waited = f" after {bound:g} s" if bound else ""
            say(f"busy{waited}, held by {holder_text(fd)}; nothing was run")
            return False
        pause = POLL_SECONDS if deadline is None else min(POLL_SECONDS, deadline - now)
        time.sleep(max(pause, 0.0))


class Claim(NamedTuple):
    """What a run above this process says it holds. It is a claim. `proves` tests it."""

    pid: int
    fd: int
    file: tuple[int, int]


def inherited(environ: dict[str, str]) -> list[Claim]:
    """The claims of the runs above this process, the outermost first."""
    entries: list[Claim] = []
    for part in environ.get(HELD_ENV, "").split(","):
        fields = part.split("/")
        if len(fields) != 4:
            break
        if not all(field.isascii() and field.isdigit() for field in fields):
            break
        pid, fd, device, inode = fields
        if max(len(pid), len(fd)) > 10 or max(len(device), len(inode)) > 20:
            break
        entries.append(Claim(int(pid), int(fd), (int(device), int(inode))))
    return entries


def proves(number: int, own: int) -> bool:
    """Whether descriptor `number` of this process holds the lock of the file open at
    `own`, which another open file holds now.

    An environment can be copied and a holder record can be written by anybody, so
    neither proves anything. A descriptor does: `flock` belongs to the open file, and
    the only open file that can take the lock again while it is held is the one that
    holds it. The holder hands that open file to its command on purpose. The file is
    compared by device and inode of the two descriptors, never of the path.
    """
    if number <= 2 or number == own:
        return False
    try:
        theirs = os.fstat(number)
    except (OSError, OverflowError):
        return False
    mine = os.fstat(own)
    if (theirs.st_dev, theirs.st_ino) != (mine.st_dev, mine.st_ino):
        return False
    return try_lock(number, fcntl.LOCK_EX)


def take(path: str, wait: float | None, claim: Claim | None) -> tuple[int, bool, bool]:
    """Hold the lock of the file at `path`, or raise Refusal.

    Returns the descriptor, whether the record can be written through it, and whether
    this run took the lock itself. The last is False when the descriptor is the one a
    run above handed down, which holds the lock already.

    When the lock is held, the environment names a run as a holder of this very file,
    and the descriptor does not prove it, the run is refused: if this process is below
    that run and the holder's command waits for it, the two waits form a cycle. Whether
    it does is not asked, and the run that is named can have ended, with another run as
    the holder: that is not asked either. The rule reads the environment and the lock,
    never the holder record.
    """
    began = time.monotonic()
    for _ in range(REOPEN_LIMIT):
        fd, writable = open_for_run(path)
        if not try_lock(fd, fcntl.LOCK_EX):
            if claim is not None and proves(claim.fd, fd):
                os.close(fd)
                return claim.fd, False, False
            found = os.fstat(fd)
            if claim is not None and claim.file == (found.st_dev, found.st_ino):
                os.close(fd)
                raise Refusal(
                    NO_DESCRIPTOR,
                    f"{path} is held. {HELD_ENV} names pid {claim.pid}, and this process "
                    "does not have that run's lock descriptor. If this process is below "
                    "that run, a program in between closed the descriptor (Python's "
                    "subprocess does unless it is told close_fds=False or pass_fds), and "
                    "this run and the holder can wait for each other without end: pass "
                    "the descriptor on. If this process is not below that run (the run "
                    f"can have ended), unset {HELD_ENV}",
                )
            left = None if wait is None else max(wait - (time.monotonic() - began), 0.0)
            if not acquire(fd, left, wait):
                os.close(fd)
                raise Refusal(BUSY)
        if same_file(fd, path):
            return fd, writable, True
        say(
            f"{path} was deleted or replaced while this run waited for its lock; "
            "taking the lock of the file that is at the path now"
        )
        os.close(fd)
    raise Refusal(NO_LOCK_FILE, f"{path} is replaced again and again")


def let_go(fd: int, path: str, clear: bool) -> bool:
    """Close `fd`, the lock descriptor of this run, and look once whether the lock is
    held after that. True when it is.

    The look is one attempt to take the lock through a second open file of the same
    file. When it succeeds, nobody held the lock, and the record of this run is cleared
    under that lock (`clear` says that the file can be written). When it fails, some
    process holds it: one that kept the descriptor of this run, or a run that started
    in this moment. The record is theirs then, and stays as it is.
    """
    flags = (os.O_RDWR if clear else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        second = os.open(path, flags)
    except OSError:
        os.close(fd)
        return False
    try:
        ours, theirs = os.fstat(fd), os.fstat(second)
        os.close(fd)
        if (ours.st_dev, ours.st_ino) != (theirs.st_dev, theirs.st_ino):
            # Another file is at the path now; `run` has said so.
            return False
        if not try_lock(second, fcntl.LOCK_EX):
            return True
        if clear:
            try:
                write_record(second, None)
            except OSError as error:
                say(f"holder record not cleared ({error}); it blocks nobody")
        return False
    finally:
        os.close(second)


def run_command(
    command: list[str],
    environ: dict[str, str],
    keep: list[int],
    started: Callable[[int], None],
) -> int:
    """Run the command, wait for it and for nothing else, and return the exit code to
    pass on. `keep` are the lock descriptors that the command gets.

    The command leads a process group that is made here. A signal to the wrapper that
    the wrapper did not inherit as ignored is passed on to that group, and a signal from
    the terminal (Ctrl-C) reaches the wrapper alone, which passes it on as well. A
    command that still runs TERM_AFTER_SECONDS later gets SIGTERM, and after
    KILL_AFTER_SECONDS more SIGKILL, sent to the same group.

    Every signal is sent from the loop below and only while the command has not been
    waited for: until then the kernel keeps its process ID, and so the ID of its group,
    from any other process. A handler only writes the signal down.
    """
    arrived: list[int] = []
    # A handler, never SIG_IGN: an ignored signal would stay ignored in the command.
    for signum in FORWARDED:
        if signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, lambda number, _frame: arrived.append(number))
    # The end of the command and every signal wake the loop through this pipe.
    signal.signal(signal.SIGCHLD, lambda _number, _frame: None)
    wake, wake_in = os.pipe()
    os.set_blocking(wake, False)
    os.set_blocking(wake_in, False)
    signal.set_wakeup_fd(wake_in, warn_on_full_buffer=False)
    try:
        try:
            child = subprocess.Popen(command, env=environ, pass_fds=keep, start_new_session=True)
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                say(f"command not found: {command[0]}")
                return NOT_FOUND
            say(f"cannot run {command[0]}: {error}")
            return NOT_RUNNABLE
        started(child.pid)
        group = child.pid

        def send(signum: int) -> None:
            try:
                os.killpg(group, signum)
            except OSError:
                pass  # No process of the group runs any more; the wait below says so.

        force, force_at, first = signal.SIGTERM, None, 0
        while child.poll() is None:
            # Not waited for yet, so `group` is still the group that was made here.
            # This loop is the only place that calls `send`.
            while arrived:
                signum = arrived.pop(0)
                if force_at is None:
                    force_at, first = time.monotonic() + TERM_AFTER_SECONDS, signum
                send(signum)
            if force_at is not None and time.monotonic() >= force_at:
                say(
                    f"signal {first} was passed on and the command had not ended at the last "
                    f"check; sending {force.name} to its process group"
                )
                send(force)
                force, force_at = signal.SIGKILL, time.monotonic() + KILL_AFTER_SECONDS
            pause = None if force_at is None else max(force_at - time.monotonic(), 0.0)
            if select.select([wake], [], [], pause)[0]:
                os.read(wake, 4096)
        status = child.returncode
    finally:
        signal.set_wakeup_fd(-1)
        os.close(wake)
        os.close(wake_in)
    if status < 0:
        say(f"the command was ended by signal {-status}")
        return 128 - status
    return status


def brief(command: list[str]) -> list[str]:
    """The command for the record, cut so that the record stays far below RECORD_BYTES."""
    parts = [part if len(part) <= 1024 else part[:1024] + "..." for part in command[:24]]
    return parts if len(command) <= 24 else [*parts, "..."]


def write_record(fd: int, record: dict[str, object] | None) -> None:
    """Write the record through a descriptor that holds the lock, or clear it."""
    data = b"" if record is None else (json.dumps(record) + "\n").encode()
    os.pwrite(fd, data, 0)
    os.ftruncate(fd, len(data))


def run(command: list[str], wait: float | None, environ: dict[str, str]) -> int:
    path = lock_path(environ)
    if environ.get(PATH_ENV):
        say(f"using {path} (from {PATH_ENV}), not the lock of this machine")

    # Level 0 is the lock file. A run below a holder proves that with the descriptor it
    # got, and then takes the lock of the next level, "<lock file>.1", so that two runs
    # below one holder still do not run at once.
    claims = inherited(environ)
    above: list[Claim] = []
    try:
        while True:
            level_path = path if not above else f"{path}.{len(above)}"
            claim = claims[len(above)] if len(above) < len(claims) else None
            fd, writable, own = take(level_path, wait, claim)
            if own or claim is None:
                break
            # The descriptor of `claim` holds this level: go on to the next one.
            above.append(claim)
    except Refusal as refusal:
        if refusal.text is not None:
            say(f"{refusal.text}; nothing was run")
        if refusal.code == NO_LOCK_FILE:
            say(
                f"set {PATH_ENV} to a file this account can make, or repair the path. "
                "A lock file that a run may hold is never deleted or replaced"
            )
        return refusal.code
    if above:
        say(
            f"already held by the run above this one (pid {above[-1].pid}, named by "
            f"{HELD_ENV} and proved by its lock descriptor); running under its lock, and "
            f"under {level_path} beside the other runs below it"
        )

    now = time.time()
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "?"
    record: dict[str, object] = {
        "version": 1,
        "pid": os.getpid(),
        "command_pid": None,
        "started": stamp(now),
        "started_epoch": round(now, 3),
        "command": brief(command),
        "cwd": cwd,
    }

    def note(change: dict[str, object] | None) -> None:
        if not writable:
            return
        try:
            write_record(fd, None if change is None else {**record, **change})
        except (OSError, ValueError) as error:
            say(f"holder record not written ({error}); the lock is held all the same")

    if not writable:
        say(f"{level_path} cannot be written: no holder record; the lock is held all the same")
    note({})

    # The command gets the lock descriptor of this run and of every run above, and the
    # names of them, so that a run below it can prove what it holds.
    level = os.fstat(fd)
    chain = [*above, Claim(os.getpid(), fd, (level.st_dev, level.st_ino))]
    marked = dict(environ)
    marked[HELD_ENV] = ",".join(
        f"{claim.pid}/{claim.fd}/{claim.file[0]}/{claim.file[1]}" for claim in chain
    )
    keep = [fd, *(claim.fd for claim in above)]
    code = run_command(command, marked, keep, lambda pid: note({"command_pid": pid}))
    if not same_file(fd, level_path):
        say(
            f"{level_path} was deleted or replaced during this run: a run that started "
            "after that did not see this one, and may have run at the same time"
        )
    if let_go(fd, level_path, writable):
        say(
            "the command has ended and the lock is still held: by a process that kept "
            "the lock descriptor of this run, or by a run that started in this moment"
        )
    return code


def probe(path: str) -> dict[str, object]:
    """Who holds the lock and whether a problem was found with the folder of the lock
    file, without waiting for the lock and without creating anything.

    `usable` is false when `run` would refuse the folder now, for the reason in
    `unusable_because`. It is true when no problem was found with a folder that exists,
    and also when the folder does not exist: that one is not checked at all, and `run`
    can still fail to make it. So true is no promise that `run` works.

    `state` is about the lock, and each of the two is what was found in this moment. A
    held lock is `held` whatever `usable` says. The lock file is read in the folder
    that was checked, through the same descriptor; a folder that was refused has no
    such descriptor, and its lock file is read at the path.
    """
    report: dict[str, object] = {"state": "free", "lock": path, "holder": None, "usable": True}
    where = None
    try:
        where = open_folder(os.path.dirname(path))
    except FileNotFoundError:
        return report  # No folder, so no lock file in it; nothing was checked.
    except Refusal as refusal:
        report["usable"] = False
        report["unusable_because"] = str(refusal.text)
    try:
        fd, _ = open_lock(path, create=False, folder=where)
    except FileNotFoundError:
        return report
    finally:
        if where is not None:
            os.close(where)
    try:
        if try_lock(fd, fcntl.LOCK_SH):
            fcntl.flock(fd, fcntl.LOCK_UN)
            stale = read_record(fd, expected=False)
        else:
            report["state"] = "held"
            stale = read_record(fd, expected=True)
            if stale is not None and alive(record_pid(stale)):
                report["holder"], stale = stale, None
        if stale is not None:
            report["stale_record"] = stale
    finally:
        os.close(fd)
    return report


def status(as_json: bool, environ: dict[str, str]) -> int:
    path = lock_path(environ)
    try:
        report = probe(path)
    except Refusal as refusal:
        say(str(refusal.text))
        return refusal.code
    except OSError as error:
        say(f"cannot read the lock file {path}: {error}")
        return NO_LOCK_FILE
    if as_json:
        sys.stdout.write(json.dumps(report, allow_nan=False) + "\n")
        return 0
    held = report["state"] == "held"
    lines = [str(report["state"])]
    holder = report["holder"]
    stale = report.get("stale_record")
    if isinstance(holder, dict):
        epoch = holder["started_epoch"]
        age = f" ({max(time.time() - epoch, 0):.0f} s ago)" if isinstance(epoch, float) else ""
        lines.append(f"pid: {record_pid(holder)}")
        if holder["command_pid"] is not None:
            lines.append(f"command pid: {holder['command_pid']}")
        lines += [
            f"started: {plain(holder['started'] or '?', 40)}{age}",
            f"command: {command_text(holder)}",
            f"cwd: {plain(holder['cwd'] or '?')}",
        ]
    elif held and isinstance(stale, dict):
        lines += [
            f"holder: unknown. The record in the lock file is of pid {record_pid(stale)}, "
            "which has ended. Some process has the descriptor that holds the lock",
            f"started: {plain(stale['started'] or '?', 40)}",
            f"command: {command_text(stale)}",
        ]
    elif held:
        lines.append(
            "holder: unknown (no record in the lock file; some process has the "
            "descriptor that holds the lock)"
        )
    lines.append(f"lock: {path}")
    because = report.get("unusable_because")
    lines.append("usable: yes" if because is None else f"usable: no. {because}")
    if not held and isinstance(stale, dict):
        lines.append(
            f"note: the file holds a record of pid {record_pid(stale)} "
            f"(started {plain(stale['started'] or '?', 40)}) that is not the holder; "
            "it blocks nothing"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def require(environ: dict[str, str]) -> int:
    """Exit 0 when a run above this process holds the lock, proved by its descriptor."""
    claims = inherited(environ)
    if claims:
        try:
            fd, _ = open_lock(lock_path(environ), create=False)
        except (OSError, Refusal):
            fd = -1
        if fd >= 0:
            try:
                # A shared lock that can be taken means that nobody holds the lock.
                if try_lock(fd, fcntl.LOCK_SH):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif proves(claims[0].fd, fd):
                    return 0
            finally:
                os.close(fd)
    say(
        "this command is not below a run that holds the suite lock, and it must be. "
        "Start it with `just check`, or under `python3 tools/suite_lock.py run -- ...`"
    )
    return NOT_HELD


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(USAGE, f"{self.prog}: error: {message}\n")


def seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not 0 <= value < float("inf"):
        raise argparse.ArgumentTypeError(f"not a wait of zero or more seconds: {text!r}")
    return value


def main(argv: list[str]) -> int:
    parser = Parser(
        prog="suite_lock.py",
        description="One whole test suite per machine and OS user.",
        epilog=(
            f"Exit codes of the wrapper itself: {BUSY} the wait ended without the lock and "
            f"nothing was run; {NO_LOCK_FILE} the lock file cannot be used; {NO_DESCRIPTOR} "
            "the lock is held and the descriptor of the run that the environment names "
            f"is not here, nothing was run; {NOT_HELD} `require` is not below a run that "
            f"holds the lock; {USAGE} wrong usage; "
            f"{NOT_FOUND} and {NOT_RUNNABLE} the command cannot be started; 128 + N the "
            f"command was ended by signal N; {INTERRUPTED} interrupted while waiting. A "
            "command can end with the same numbers."
        ),
    )
    actions = parser.add_subparsers(dest="action", metavar="{run,status,require}")
    run_parser = actions.add_parser(
        "run",
        usage="suite_lock.py run [--wait SECONDS | --no-wait] -- command [argument ...]",
        help="take the lock, run the command, exit with its exit code",
    )
    bound = run_parser.add_mutually_exclusive_group()
    bound.add_argument("--wait", type=seconds, metavar="SECONDS", help="wait at most this long")
    bound.add_argument("--no-wait", action="store_true", help="give up at once when it is held")
    status_parser = actions.add_parser(
        "status", help="print who holds the lock; never waits for it"
    )
    status_parser.add_argument("--json", action="store_true", help="one JSON object")
    require_parser = actions.add_parser(
        "require", help=f"exit 0 below a run that holds the lock, else {NOT_HELD}"
    )

    # The command is everything after the first "--": none of it is the wrapper's.
    cut = argv.index("--") if "--" in argv else len(argv)
    options = parser.parse_args(argv[:cut])
    command = argv[cut + 1 :]
    environ = dict(os.environ)
    if options.action == "status":
        if cut != len(argv):
            status_parser.error("status takes no command")
        return status(options.json, environ)
    if options.action == "require":
        if cut != len(argv):
            require_parser.error("require takes no command")
        return require(environ)
    if options.action != "run":
        parser.error("say run, status or require")
    if not command:
        run_parser.error('a command is needed after "--"')
    wait = 0.0 if options.no_wait else options.wait
    try:
        return run(command, wait, environ)
    except KeyboardInterrupt:
        say("interrupted")
        return INTERRUPTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
