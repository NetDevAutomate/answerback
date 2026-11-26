"""Publication of ``fleet.json`` by rename from a staging folder (spec section 9)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
from pathlib import Path

from . import document

NAME = "fleet.json"
LOCK = "writer.lock"
STAGED = "fleet.json.new"
ALLOWED = frozenset({NAME})
STAGE_ALLOWED = frozenset({LOCK, STAGED})


class PublishError(Exception):
    """A document was not published; the previous file stays."""


class PublishRefused(Exception):
    """The folders cannot be used; the writer does not start."""


class LockHeld(Exception):
    """Another writer holds the folder."""


def default_folder(config_path: Path) -> Path:
    """``fleet`` beside the bridge's runtime folder: the config file's folder, then fleet/."""
    return config_path.expanduser().resolve().parent / "fleet"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC


def _device(fd: int) -> int:
    return os.fstat(fd).st_dev


def _open_folder(path: Path) -> int | None:
    """Open ``path`` as a folder without following a link; None when it does not exist."""
    try:
        return os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise PublishRefused(f"{path} is a symbolic link or not a folder") from None
        raise PublishRefused(f"{path} cannot be opened: {error.strerror or error}") from None


def _inspect(fd: int, path: Path, allowed: frozenset[str]) -> None:
    """Check an open folder: ours, and holding only regular files of ours under ``allowed``."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PublishRefused(f"{path} must be a folder owned by the current user")
    names = os.listdir(fd)
    others = sorted(set(names) - allowed)
    if others:
        shown = ", ".join(others[:3]) + (", ..." if len(others) > 3 else "")
        raise PublishRefused(f"{path} holds files the fleet writer does not own: {shown}")
    for name in sorted(set(names) & allowed):
        _one_file(os.stat(name, dir_fd=fd, follow_symlinks=False), path / name)


def _one_file(info: os.stat_result, path: Path) -> None:
    """Refuse an entry that is not a regular file of ours with one name only."""
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise PublishRefused(f"{path} must be a regular file of the current user, not a link")
    if info.st_nlink != 1:
        raise PublishRefused(f"{path} has a second name (a hard link); the writer refuses it")


def _close_each(held: list[int]) -> None:
    """Give every descriptor to ``os.close`` once, the last opened first, whatever a close does.

    An ``OSError`` of a close is dropped. Any other exception of a close (an interrupt) is
    kept until every descriptor was tried; then the first of them is raised.
    """
    stopped: BaseException | None = None
    for fd in reversed(held):
        try:
            os.close(fd)
        except OSError:
            pass
        except BaseException as error:
            if stopped is None:
                stopped = error
    if stopped is not None:
        raise stopped


class Publisher:
    """Owns one published folder and its staging folder ``<folder>.stage``.

    Both folders are opened once, without following a link, and held by descriptor until
    ``close``. Every later step goes through these descriptors, so a path that is replaced
    after the check cannot make the writer change or write into another folder.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder.expanduser().absolute()
        self.stage = self.folder.with_name(self.folder.name + ".stage")
        self._folder_fd: int | None = None
        self._stage_fd: int | None = None
        self._lock_fd: int | None = None

    def open(self) -> None:
        """Check both folders, then make and hold them, and take the writer lock.

        Both existing folders are checked before either is changed. A start that fails
        sets no mode of a folder that existed before it holds the lock, and leaves a staged
        file an earlier writer left. It removes nothing: what it made (folders, an empty lock
        file) stays and the next start uses it, since a start cannot tell whether another
        start already uses what it made.

        A publisher that still holds a folder or the lock (it is open, or an interrupt ended
        its ``close`` early) is refused before any step, and keeps what it holds.
        """
        if self._lock_fd is not None or self._stage_fd is not None or self._folder_fd is not None:
            raise PublishRefused(f"this publisher still holds {self.folder}: close it first")
        held: list[int] = []
        try:
            fds: dict[Path, int | None] = {}
            for path, allowed in ((self.folder, ALLOWED), (self.stage, STAGE_ALLOWED)):
                fd = _open_folder(path)
                if fd is not None:
                    held.append(fd)
                    _inspect(fd, path, allowed)
                fds[path] = fd
            for path in (self.folder, self.stage):
                if fds[path] is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.mkdir(path, 0o700)
                    fd = _open_folder(path)
                    if fd is None:
                        raise PublishRefused(f"{path} vanished while it was made")
                    held.append(fd)
                    _inspect(fd, path, frozenset())
                    os.fchmod(fd, 0o700)  # a folder this start made gets its mode now
                    fds[path] = fd
            folder_fd, stage_fd = fds[self.folder], fds[self.stage]
            assert folder_fd is not None and stage_fd is not None
            if _device(folder_fd) != _device(stage_fd):
                raise PublishRefused("the published and staging folders must be on one file system")
            lock_fd = self._open_lock(stage_fd)
            held.append(lock_fd)
            self._take_lock(lock_fd)
            self._lock_fd = lock_fd
            # Only now, with the lock taken and its mode set: a refused start sets no mode
            # of a folder that existed.
            os.fchmod(folder_fd, 0o700)
            os.fchmod(stage_fd, 0o700)
            # The very last step: a file of an earlier writer that died between write and
            # rename.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(STAGED, dir_fd=stage_fd)
        except BaseException:
            # The lock's number is forgotten before any descriptor is closed, so that no
            # later close() can give it to os.close again (each descriptor is closed once).
            self._lock_fd = None
            # No field holds the other numbers, so every held descriptor is tried here, also
            # after a close raised. Without an interrupt of a close the error of the start
            # comes out, the same object; with one, that interrupt comes out.
            _close_each(held)
            raise
        self._folder_fd, self._stage_fd = folder_fd, stage_fd

    def _open_lock(self, stage_fd: int) -> int:
        """Open or make the lock file: never truncated, never through a link."""
        try:
            return os.open(LOCK, os.O_RDWR | os.O_CREAT | _FILE_FLAGS, 0o600, dir_fd=stage_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise PublishRefused(f"{self.stage / LOCK} is a symbolic link") from None
            raise

    def _take_lock(self, fd: int) -> None:
        # Type, owner and one name, on the open descriptor, before flock and fchmod.
        _one_file(os.fstat(fd), self.stage / LOCK)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld(f"another fleet writer already publishes to {self.folder}") from None
        os.fchmod(fd, 0o600)

    def close(self) -> None:
        """Release the lock and the folders; the last published file stays."""
        # The lock first, then the staging folder, then the published folder. Each field is
        # cleared before its descriptor is closed, so no descriptor is closed twice. Every
        # one is tried, also after a close failed; then the first OSError comes out, the same
        # object. Any other exception of a close (an interrupt) goes out at once: the fields
        # not yet reached stay stored, and a later close() closes them.
        first: OSError | None = None
        for name in ("_lock_fd", "_stage_fd", "_folder_fd"):
            fd = getattr(self, name)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError as error:
                    if first is None:
                        first = error
        if first is not None:
            raise first

    def publish(self, data: bytes) -> None:
        """Check, write and sync in the staging folder, then rename into the folder."""
        problems = document.validate(data)
        if problems:
            raise PublishError("document refused: " + ", ".join(problems[:5]))
        folder_fd, stage_fd = self._folder_fd, self._stage_fd
        if folder_fd is None or stage_fd is None:
            raise PublishError("publication failed: the folders are not open")
        if self._lock_fd is None:
            # An interrupt ended close() after the lock: the folders are still held, the
            # lock is not, and another writer may hold it.
            raise PublishError("publication failed: the writer lock is not held")
        try:
            # Always a new file: never one that stood there, whatever it is or links to.
            fd = os.open(
                STAGED, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=stage_fd
            )
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(STAGED, NAME, src_dir_fd=stage_fd, dst_dir_fd=folder_fd)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(STAGED, dir_fd=stage_fd)
            raise PublishError(f"publication failed: {error.strerror or error}") from None
        with contextlib.suppress(OSError):
            os.fsync(folder_fd)
