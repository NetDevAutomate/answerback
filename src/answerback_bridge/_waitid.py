"""Non-reaping child observation, including CPython <3.13 on 64-bit Darwin."""

import ctypes
import errno
import os
import sys


class _Sigval(ctypes.Union):
    _fields_ = [("integer", ctypes.c_int), ("pointer", ctypes.c_void_p)]


class _DarwinSiginfo(ctypes.Structure):
    # Darwin SDK sys/signal.h siginfo_t; both supported 64-bit Darwin ABIs.
    _fields_ = [
        ("signo", ctypes.c_int),
        ("errno", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("addr", ctypes.c_void_p),
        ("value", _Sigval),
        ("band", ctypes.c_long),
        ("pad", ctypes.c_ulong * 7),
    ]


def require_waitid() -> None:
    if hasattr(os, "waitid"):
        return
    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Owned POSIX children require non-reaping waitid support")


def _darwin_peek(pid: int) -> tuple[int, int, int]:
    # Public libc ABI from sys/wait.h: P_PID=1, WEXITED=4, WNOHANG=1,
    # WNOWAIT=32. No signal handlers or global child watchers are installed.
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    waitid = library.waitid
    waitid.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(_DarwinSiginfo),
        ctypes.c_int,
    ]
    waitid.restype = ctypes.c_int
    info = _DarwinSiginfo()
    if waitid(1, pid, ctypes.byref(info), 4 | 1 | 32) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return info.pid, info.code, info.status


def peek_exit(pid: int) -> int | None:
    """Observe this waitable child without releasing its numeric identity."""
    if hasattr(os, "waitid"):
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if info is None:
            return None
        found, code, status = info.si_pid, info.si_code, info.si_status
    else:
        require_waitid()
        found, code, status = _darwin_peek(pid)
    if found == 0:
        return None
    if found != pid or code not in (1, 2, 3):  # CLD_EXITED/KILLED/DUMPED
        raise OSError(errno.EPROTO, "Unexpected waitid child status")
    return status if code == 1 else -status


def darwin_only_retained_leader(pid: int) -> bool:
    """Darwin killpg returns EPERM for a group containing only its zombie leader.

    Call only while waitid still owns that exited leader. A complete bounded
    PROC_PGRP_ONLY snapshot must contain no other PID; errors/truncation refuse.
    """
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    listpids = library.proc_listpids
    listpids.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    pids = (ctypes.c_int * 4096)()
    ctypes.set_errno(0)
    size = listpids(2, pid, pids, ctypes.sizeof(pids))  # sys/proc_info.h PROC_PGRP_ONLY
    if ctypes.get_errno() or size < 0 or size >= ctypes.sizeof(pids) or size % 4:
        return False
    return set(pids[: size // 4]) <= {pid}
