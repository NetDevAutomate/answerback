"""Bounded subprocesses, always using argument arrays."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ._waitid import darwin_only_retained_leader, peek_exit, require_waitid

MAX_OUTPUT = 4 * 1024 * 1024
# Provider connections may require the operator's proxy and certificate settings.
NETWORK_ENV_KEYS = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
CLI_ENV_KEYS = NETWORK_ENV_KEYS | frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
    }
)
CODEX_ENV_KEYS = CLI_ENV_KEYS | {
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_CA_CERTIFICATE",
}


def module_launch_argv(config_path: str | Path, *arguments: str) -> list[str]:
    """Build the bridge's own ``-m`` re-entry command, safe from the launcher's cwd.

    Every caller that tells another process to run ``python -m answerback_bridge``
    (an MCP client's registered server, a peer agent's completion callback, a
    supervisor's service command or a Kiro worker's MCP definition) must build that
    argv through this function. Without ``-P``, CPython prepends the launching
    process's current working directory to ``sys.path`` ahead of the installed
    package, so a same-named ``answerback_bridge`` directory, or a module the bridge
    imports such as ``json.py``, sitting in whatever directory the caller happens to
    be in would run in place of the installed bridge.
    """
    return [
        sys.executable,
        "-P",
        "-m",
        "answerback_bridge",
        "--config",
        str(config_path),
        *arguments,
    ]


class BridgeError(RuntimeError):
    """A runtime or transport operation could not be verified."""


class ProviderOffline(BridgeError):
    """A registered provider is not currently accepting connections."""


class CommandCleanupError(BridgeError):
    """Owned child identity or cleanup completion could not be verified."""


class CommandTimeout(BridgeError):
    """A bounded child process exceeded its allotted time."""


class CommandError(BridgeError):
    """A child exited unsuccessfully, preserving its machine-readable exit status."""

    def __init__(self, returncode: int, detail: str) -> None:
        self.returncode = returncode
        super().__init__(f"Command failed (exit {returncode}): {detail}")


@dataclass(frozen=True)
class Output:
    stdout: str
    stderr: str
    # The undecoded stdout, only when `run(..., keep_stdout_bytes=True)` asked for it.
    stdout_bytes: bytes | None = None


class _WritePipeProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop=loop)
        self.closed: asyncio.Future[None] = loop.create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self.closed.done():
            if exc is None:
                self.closed.set_result(None)
            else:
                self.closed.set_exception(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future[None]:
        return self.closed


class _ReadPipeProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__(reader)
        self.closed: asyncio.Future[Exception | None] = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self.closed.done():
            self.closed.set_result(exc)


class OwnedChild:
    """Exclusive POSIX child owner; wait observes exit, stop_child alone reaps.

    A retained waitable leader pins its PID and therefore its new process group.
    No asyncio child watcher, Popen.poll(), or independent waiter may reap it.
    Uncooperative external waitpid calls are detected as lost ownership.
    """

    def __init__(self, child: subprocess.Popen[bytes]) -> None:
        self._child = child
        self.pid = child.pid
        self.returncode: int | None = None
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None
        self._pipes: list[asyncio.BaseTransport] = []
        self._read_closed: list[asyncio.Future[Exception | None]] = []
        self._reaped = False
        self._lost = False
        self._cleanup: asyncio.Task[None] | None = None

    def _observe(self) -> int | None:
        if self._lost:
            raise CommandCleanupError("Child ownership was lost; refusing process-group signals")
        if self._reaped:
            return self.returncode
        try:
            code = peek_exit(self.pid)
        except OSError as error:
            self._lost = True
            # Prevent Popen's destructor from starting an unrelated wait attempt.
            self._child.returncode = 255
            raise CommandCleanupError(
                "Child ownership could not be verified; refusing process-group signals"
            ) from error
        if code is not None:
            self.returncode = code
        return code

    async def wait(self) -> int:
        while (code := self._observe()) is None:  # noqa: ASYNC110 — kernel waitid, no Event producer
            await asyncio.sleep(0.01)
        return code

    async def _signal_group(self, sig: signal.Signals, *, deadline: float | None = None) -> None:
        if self._reaped:
            raise CommandCleanupError("Child ownership released; refusing process-group signals")
        self._observe()  # ECHILD/unknown status refuses before signalling.
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            # The still-waitable leader pins the ID, but its group may be empty.
            pass
        except PermissionError as error:
            code = self._observe()
            if code is None and sys.platform == "darwin":
                report_deadline = asyncio.get_running_loop().time() + 1
                if deadline is not None:
                    report_deadline = min(report_deadline, deadline)
                try:
                    async with asyncio.timeout_at(report_deadline):
                        await self.wait()
                except TimeoutError:
                    pass
                else:
                    if self._reaped:
                        raise CommandCleanupError(
                            "Child ownership released; refusing process-group signals"
                        ) from error
                    code = self._observe()
            if code is not None and darwin_only_retained_leader(self.pid):
                return
            raise CommandCleanupError(
                "Command cleanup cannot confirm process-group termination"
            ) from error

    def _reap_ready(self) -> bool:
        if self._reaped:
            return True
        if self._observe() is None:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError as error:
            self._lost = True
            self._child.returncode = 255
            raise CommandCleanupError("Child ownership lost before reaping") from error
        if pid != self.pid:
            raise CommandCleanupError("Child reaping could not be verified")
        self.returncode = os.waitstatus_to_exitcode(status)
        self._child.returncode = self.returncode
        self._reaped = True
        return True

    def _close_pipes(self) -> None:
        for pipe in self._pipes:
            pipe.close()
        for file in (self._child.stdin, self._child.stdout, self._child.stderr):
            if file is not None:
                file.close()

    async def _connect(self, limit: int) -> None:
        loop = asyncio.get_running_loop()

        async def reader(file: IO[bytes]) -> asyncio.StreamReader:
            stream = asyncio.StreamReader(limit=limit)
            protocol = _ReadPipeProtocol(stream)
            self._read_closed.append(protocol.closed)
            transport, _ = await loop.connect_read_pipe(lambda: protocol, file)
            self._pipes.append(transport)
            return stream

        if self._child.stdout is not None:
            self.stdout = await reader(self._child.stdout)
        if self._child.stderr is not None:
            self.stderr = await reader(self._child.stderr)
        if self._child.stdin is not None:
            protocol = _WritePipeProtocol(loop)
            transport, _ = await loop.connect_write_pipe(lambda: protocol, self._child.stdin)
            self._pipes.append(transport)
            self.stdin = asyncio.StreamWriter(transport, protocol, None, loop)


ChildProcess = OwnedChild | asyncio.subprocess.Process
_pending_reaps: set[asyncio.Task[None]] = set()


async def settle(task: asyncio.Task[Any]) -> Any:
    """Repeated cancellation cannot abandon one already-owned cleanup."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def spawn_owned(
    *args: str,
    limit: int = 65536,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
    start_new_session: bool = False,
) -> ChildProcess:
    """Create only an explicitly isolated child; refuse unsupported POSIX before spawn."""
    if os.name != "posix":
        return await asyncio.create_subprocess_exec(
            *args, limit=limit, env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr
        )
    if not start_new_session:
        raise ValueError("Owned children require start_new_session=True")
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise CommandCleanupError("SIGCHLD auto-reaping prevents retained child ownership")
    require_waitid()

    def start() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            args,
            bufsize=0,
            env=env,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    async def create() -> OwnedChild:
        child = OwnedChild(await asyncio.to_thread(start))
        try:
            await child._connect(limit)
        except BaseException:
            await stop_child(child, cleanup_seconds=2)
            raise
        return child

    creating = asyncio.create_task(create())
    try:
        return await settle(creating)
    except BaseException:
        if creating.done() and not creating.cancelled() and creating.exception() is None:
            await stop_child(creating.result(), cleanup_seconds=2)
        raise


async def _reap_later(proc: OwnedChild) -> None:
    # A killed process can remain uninterruptible beyond the cleanup budget.
    # Retain ownership solely to reap it later; this task never sends a signal.
    try:
        while not proc._reap_ready():  # noqa: ASYNC110 — kernel waitid, no Event producer
            await asyncio.sleep(0.05)
    except CommandCleanupError:
        pass  # The caller already received the failed-cleanup result.


async def _stop_owned(proc: OwnedChild, cleanup_seconds: float | None) -> None:
    """macOS EPERM waits at most 1 s, sharing any cleanup_seconds deadline with completion."""
    deadline = (
        None if cleanup_seconds is None else asyncio.get_running_loop().time() + cleanup_seconds
    )
    try:
        if cleanup_seconds is None:
            await proc._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 0.2)
            except TimeoutError:
                pass
        await proc._signal_group(signal.SIGKILL, deadline=deadline)
        try:
            async with asyncio.timeout_at(
                asyncio.get_running_loop().time() + 3 if deadline is None else deadline
            ):
                await proc.wait()
                errors = await asyncio.gather(
                    *(asyncio.shield(closed) for closed in proc._read_closed)
                )
                if any(errors):
                    raise CommandCleanupError("Command cleanup observed a failed output pipe")
        except TimeoutError as error:
            raise CommandCleanupError(
                "Command cleanup failed to confirm owned child reaping"
            ) from error
    finally:
        proc._close_pipes()
        if not proc._lost and not proc._reap_ready():
            reaper = asyncio.create_task(_reap_later(proc))
            _pending_reaps.add(reaper)
            reaper.add_done_callback(_pending_reaps.discard)
        await asyncio.sleep(0)  # Publish pipe closure to readers.


async def stop_child(proc: ChildProcess, *, cleanup_seconds: float | None = None) -> None:
    if cleanup_seconds is not None and cleanup_seconds <= 0:
        raise ValueError("cleanup_seconds must be positive")
    if isinstance(proc, OwnedChild):
        if proc._cleanup is None:
            proc._cleanup = asyncio.create_task(_stop_owned(proc, cleanup_seconds))
        await settle(proc._cleanup)
        return
    if os.name == "posix":
        raise CommandCleanupError(
            "POSIX child ownership is unverified; create it with spawn_owned before cleanup"
        )
    # Windows asyncio retains the native process handle rather than a POSIX group ID.
    if proc.returncode is not None:
        return
    try:
        proc.kill() if cleanup_seconds is not None else proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 3 if cleanup_seconds is None else cleanup_seconds)
    except TimeoutError as error:
        if cleanup_seconds is not None:
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
            raise CommandCleanupError("Command cleanup failed to confirm child reaping") from error
        proc.kill()
        await proc.wait()


async def run(
    args: list[str],
    *,
    max_seconds: float = 30,
    env: dict[str, str] | None = None,
    cleanup_seconds: float | None = None,
    keep_stdout_bytes: bool = False,
) -> Output:
    """Run owned argv; opt-in cleanup kills immediately and bounds wait completion.

    Cleanup failure is explicit. Caller cancellation cannot abandon the cleanup
    task; the default TERM/grace behavior remains unchanged for other providers.
    Output text is always decoded with replacement; `keep_stdout_bytes` also
    returns the undecoded stdout, for a caller that must decode it strictly.
    """
    if cleanup_seconds is not None and cleanup_seconds <= 0:
        raise ValueError("cleanup_seconds must be positive")
    proc = await spawn_owned(
        *args,
        env=env,
        start_new_session=os.name == "posix",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        assert stream is not None
        chunks = bytearray()
        while chunk := await stream.read(65536):
            chunks.extend(chunk)
            if len(chunks) > MAX_OUTPUT:
                raise BridgeError("Command output exceeded the 4 MiB bound")
        return bytes(chunks)

    readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        async with asyncio.timeout(max_seconds):
            stdout, stderr = await asyncio.gather(*readers)
            await proc.wait()
        if proc.returncode:
            detail = stderr.decode(errors="replace")[-2000:].strip()
            raise CommandError(proc.returncode, detail)
        return Output(
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            stdout if keep_stdout_bytes else None,
        )
    except TimeoutError as error:
        raise CommandTimeout(
            "Command timed out; delivery may be uncertain. Do not resubmit blindly."
        ) from error
    finally:
        for task in readers:
            task.cancel()

        async def cleanup() -> None:
            try:
                await stop_child(proc, cleanup_seconds=cleanup_seconds)
            finally:
                await asyncio.gather(*readers, return_exceptions=True)

        await settle(asyncio.create_task(cleanup()))


def herdr_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "HERDR_SOCKET_PATH",
        "HERDR_CLIENT_SOCKET_PATH",
        "HERDR_SESSION",
        "HERDR_WORKSPACE_ID",
        "HERDR_TAB_ID",
        "HERDR_PANE_ID",
    ):
        env.pop(key, None)
    return env


def codex_env(home: str | None) -> dict[str, str]:
    """Forward only Codex authentication, profile and CLI startup variables."""
    env = {key: os.environ[key] for key in CODEX_ENV_KEYS if key in os.environ}
    if home is not None:
        env["CODEX_HOME"] = home
    return env
