"""Single service owner, graceful drain, and restart reconciliation."""

import asyncio
import json
import logging
import os
import signal
import stat
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

from . import __version__
from .config import Config, atomic_write
from .errors import (
    AdmissionRefused,
    PublicBridgeError,
    admission_refusal_from_info,
    admission_refusal_info,
    public_bridge_error,
)
from .plugin import ModuleError, ModulePlugin
from .process import BridgeError
from .service import Bridge
from .transport import (
    FRAME_LIMIT,
    RPC_VERSION,
    check_config_owner,
    encode,
    private_directory,
    request_seconds,
    runtime_dir,
    socket_path,
    timeout_message,
)

_LOG = logging.getLogger(__name__)

# Bounded crash-loop guard. launchd (KeepAlive.SuccessfulExit=False) and systemd
# (Restart=on-failure) restart the unit only after a non-zero exit, so a config that
# fails on every startup (an incompatible upgrade) would otherwise crash-loop at the
# ten-second throttle for ever. After this many startup failures inside the window,
# cli.main() exits 0 instead, which stops both supervisors; a successful start clears
# the record and failures older than the window are forgotten.
CRASH_LOOP_FAILURE_LIMIT = 5
CRASH_LOOP_WINDOW_SECONDS = 120.0
MODULE_SHUTDOWN_TIMEOUT = 5.0
FINAL_SHUTDOWN_TIMEOUT = 1.0
# The loop-lag guard: a task sleeps LOOP_WATCH_SECONDS and measures how late it wakes. A
# wake later than LOOP_LAG_SECONDS means the loop was blocked (every request waited that
# long, and a watchdog with a 2 s budget may have called the service unreachable); it is
# counted in `status` under `loop` and logged as a `loop_lag` event. The task runs on the
# loop it watches, so it reports a block when the block ends and under-reads it by up to
# LOOP_WATCH_SECONDS.
LOOP_WATCH_SECONDS = 0.1
LOOP_LAG_SECONDS = 1.0
SERVICE_SHUTDOWN_FAILURE = 1
_STARTUP_FAILURES_FILENAME = "start-failures.json"


def startup_failures_path(config_path: Path) -> Path:
    """The guard record: beside the socket, in the bridge's own runtime directory."""
    return runtime_dir(config_path) / _STARTUP_FAILURES_FILENAME


def record_startup_failure(
    config_path: Path, limit: int | None = None, window: float | None = None
) -> bool:
    """Record one `serve` startup failure; True once the guard has tripped.

    The record is a small JSON object (count and timestamps) in the bridge's runtime
    directory, mode 0600. `limit` and `window` default to the module constants read
    at call time, so a caller that passes neither (cli.main) honours a monkeypatch.
    """
    if limit is None:
        limit = CRASH_LOOP_FAILURE_LIMIT
    if window is None:
        window = CRASH_LOOP_WINDOW_SECONDS
    marker = startup_failures_path(config_path)
    private_directory(marker.parent)
    now = time.time()
    try:
        previous = json.loads(marker.read_text()).get("timestamps", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        previous = []
    recent = [
        stamp for stamp in previous if isinstance(stamp, int | float) and now - stamp < window
    ]
    recent.append(now)
    # atomic_write creates the file through NamedTemporaryFile, which is 0600 already.
    atomic_write(marker, json.dumps({"count": len(recent), "timestamps": recent}))
    return len(recent) >= limit


def clear_startup_failures(config_path: Path) -> None:
    """Forget recorded startup failures once the service is up."""
    startup_failures_path(config_path).unlink(missing_ok=True)


class Service:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.path = socket_path(config_path)
        self.instance_id = str(uuid.uuid4())
        self.started = time.time()
        self.stopping = asyncio.Event()
        self.clients: set[asyncio.Task[Any]] = set()
        self.module_tasks: dict[str, asyncio.Task[None]] = {}
        self.module_shutdown_timed_out = False
        self.long_poll_slots: dict[str, asyncio.BoundedSemaphore] = {}
        self.slots = asyncio.Semaphore(4)
        self.rex_receiving = 0
        self.event_readers = 0
        self.loop_lag: dict[str, Any] = {
            "lag_events": 0,
            "max_lag_seconds": 0.0,
            "last_lag_seconds": None,
            "last_lag_at": None,
        }
        # The monotonic clock the loop-lag guard reads; None means the running loop's own.
        # Tests script it so the arithmetic is checked without timing a real block.
        self.clock: Callable[[], float] | None = None
        self.bridge: Bridge
        self.log = logging.getLogger(f"answerback_bridge.{self.instance_id}")
        self.log.propagate = False
        self.log.setLevel(logging.INFO)

    def event(self, event: str, **fields: Any) -> None:
        self.log.info(json.dumps({"time": time.time(), "event": event, **fields}))

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "runtime": {
                "interpreter": sys.executable,
                "package_path": str(Path(__file__).resolve().parent),
                "package_version": __version__,
            },
            "running": True,
            "accepting": not self.stopping.is_set(),
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "started": self.started,
            "socket": str(self.path),
            "tasks": self.bridge.store.counts(),
            "bus": self.bridge.module_statuses().get("bus", {}),
            "providers": self.bridge.provider_status,
            "clients": self.bridge.clients.snapshot(),
            "loop": dict(self.loop_lag),
            "herdr_session": self.bridge.config.herdr_session,
            "desktop_aliases": list(self.bridge.config.desktop_threads),
            **self.bridge.remote_results(),
        }

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        assert current is not None
        if len(self.clients) >= 32:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(current)
        method = "invalid"
        try:
            async with asyncio.timeout(request_seconds(method)) as deadline:
                raw = await reader.readline()
                request = json.loads(raw)
                if not isinstance(request, dict) or request.get("version") != RPC_VERSION:
                    raise PublicBridgeError("protocol_incompatible")
                if request.get("config_path") != str(self.config_path):
                    raise PublicBridgeError("configuration_mismatch")
                candidate = request.get("method")
                if candidate not in self.bridge.METHODS | {"status", "stop", "reset"}:
                    raise PublicBridgeError("operation_unknown")
                method = candidate
                if method in {"crew_configure", "fleet_snapshot"}:
                    deadline.reschedule(asyncio.get_running_loop().time() + request_seconds(method))
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise PublicBridgeError("parameters_invalid")
                if method in {"status", "stop", "reset", "fleet_snapshot"} and params:
                    raise PublicBridgeError("parameters_unexpected")
                if method == "status":
                    result = self.status()
                elif method == "stop":
                    self.stopping.set()
                    result = {"stopping": True}
                elif method == "fleet_snapshot":
                    result = await self.bridge.call(method, params)
                    result["health"] = {
                        "running": True,
                        "accepting": not self.stopping.is_set(),
                        "instance_id": self.instance_id,
                        "started": self.started,
                        "version": __version__,
                    }
                elif self.stopping.is_set():
                    raise PublicBridgeError("draining")
                elif method == "reset":
                    self.bridge.reset_connections()
                    result = {"reset": "connections", "receipts_retained": True}
                elif method == "rex_receive":
                    # Long polls have their own capacity, leaving submission/ack slots free.
                    if self.rex_receiving >= 8:
                        raise BridgeError("Rex receive capacity reached")
                    self.rex_receiving += 1
                    try:
                        result = await self.bridge.call(method, params)
                    finally:
                        self.rex_receiving -= 1
                elif method in getattr(self.bridge, "long_poll_verbs", {}):
                    # Modules declare a per-verb capacity for long polls, keeping their
                    # waiters outside the short-operation slots without making them unbounded.
                    capacity = self.bridge.long_poll_verbs[method]
                    slot = self.long_poll_slots.setdefault(
                        method, asyncio.BoundedSemaphore(capacity)
                    )
                    if slot.locked():
                        refusal = getattr(self.bridge, "long_poll_refusals", {}).get(method)
                        if refusal is not None:
                            raise refusal()
                        raise BridgeError("Long-poll capacity reached")
                    async with slot:
                        result = await self.bridge.call(method, params)
                elif method in {"events_read", "events_runs"}:
                    # Stream readers long-poll on their own counter, never a dispatch slot.
                    if self.event_readers >= 8:
                        raise BridgeError("Event stream reader capacity reached")
                    self.event_readers += 1
                    try:
                        result = await self.while_connected(
                            reader, self.bridge.call(method, params)
                        )
                    finally:
                        self.event_readers -= 1
                elif method in {
                    "actor_send",
                    "actor_ack",
                    "actor_reply",
                    "debug_on",
                    "debug_off",
                    "debug_status",
                    "pressure_status",
                    "pressure_clear",
                    "task_complete",
                    "desktop_list",
                    "task_abandon",
                    "pair",
                    "task_for_peer",
                    "task_permissions",
                    "permission_answer",
                    "task_cancel",
                    "client_list",
                    "client_register",
                    "client_heartbeat",
                    "client_disconnect",
                    "client_poll",
                    "client_ack",
                }:
                    result = await self.bridge.call(method, params)
                else:
                    async with self.slots:
                        result = await self.bridge.call(method, params)
                writer.write(encode({"version": RPC_VERSION, "ok": True, "result": result}))
                await writer.drain()
                if method not in (
                    "status",
                    "fleet_snapshot",
                    "client_heartbeat",
                    "client_poll",
                    "events_read",
                    "events_runs",
                ):
                    self.event("request", operation=method, outcome="ok")
        except Exception as error:
            self.event(
                "request", operation=method, outcome="error", error_type=type(error).__name__
            )
            try:
                detail = "Bridge request failed; inspect service logs and the task receipt."
                info: dict[str, Any] = {"kind": "internal"}
                if isinstance(error, TimeoutError):
                    detail, info = timeout_message(method), {"kind": "timeout"}
                elif isinstance(error, AdmissionRefused):
                    refusal_info = admission_refusal_info(error)
                    refusal = admission_refusal_from_info(refusal_info)
                    if refusal is not None:
                        detail, info = str(refusal), refusal_info
                elif public := public_bridge_error(error):
                    detail, info = str(public), {"kind": "public", "code": public.code}
                elif isinstance(error, ModuleError):
                    detail, info = str(error), {"kind": error.kind, "code": error.code}
                writer.write(
                    encode(
                        {"version": RPC_VERSION, "ok": False, "error": detail, "error_info": info}
                    )
                )
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.clients.discard(current)

    async def while_connected(self, reader: asyncio.StreamReader, call: Awaitable[Any]) -> Any:
        """Await a long poll, giving it up the moment its client leaves or the service stops.

        A client sends one request and then only listens, so anything arriving on its
        socket, end-of-file above all, means nobody is waiting for the answer. Without this
        a closed reader held its capacity slot until the poll timed out."""
        work = asyncio.ensure_future(call)
        gone = asyncio.ensure_future(reader.read(1))
        stop = asyncio.ensure_future(self.stopping.wait())
        try:
            await asyncio.wait({work, gone, stop}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            if stop.done():
                raise PublicBridgeError("draining")
            raise ConnectionResetError("The reader disconnected")
        finally:
            for task in (work, gone, stop):
                task.cancel()
            await asyncio.gather(work, gone, stop, return_exceptions=True)

    async def reconcile(self) -> None:
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), 10)
            except TimeoutError:
                for task_id in self.bridge.observation_ids(limit=4):
                    if self.stopping.is_set():
                        return
                    try:
                        await self.bridge.get_task(task_id)
                    except (BridgeError, OSError, ValueError) as error:
                        self.event("reconcile", outcome="error", error_type=type(error).__name__)

    async def watch_loop(self) -> None:
        """Count the times the event loop was blocked for longer than LOOP_LAG_SECONDS."""
        loop = asyncio.get_running_loop()
        clock = self.clock or loop.time
        writes: set[asyncio.Task[None]] = set()
        before = clock()
        try:
            while not self.stopping.is_set():
                await asyncio.sleep(LOOP_WATCH_SECONDS)
                now = clock()
                lag = now - before - LOOP_WATCH_SECONDS
                # The next window opens here, so whatever the bookkeeping below costs the
                # loop is measured by it, like any other block.
                before = now
                if lag > LOOP_LAG_SECONDS:
                    seconds = round(lag, 3)
                    self.loop_lag["lag_events"] += 1
                    self.loop_lag["max_lag_seconds"] = max(
                        self.loop_lag["max_lag_seconds"], seconds
                    )
                    self.loop_lag["last_lag_seconds"] = seconds
                    self.loop_lag["last_lag_at"] = time.time()
                    # `event` is a synchronous write to a rotating file. The guard reports
                    # on the loop, so it must not stall it: `status` carries the count at
                    # once and the log line is written from a worker thread.
                    write = loop.create_task(
                        asyncio.to_thread(self.event, "loop_lag", seconds=seconds)
                    )
                    writes.add(write)
                    write.add_done_callback(writes.discard)
        finally:
            # Let a pending line reach the file before `serve()` closes the handler.
            if writes:
                await asyncio.gather(*writes, return_exceptions=True)

    async def serve(self) -> None:
        if os.name != "posix":
            raise BridgeError("The shared service currently requires macOS or Linux")
        import fcntl

        private_directory(runtime_dir(self.config_path))
        if len(os.fsencode(self.path)) > 100:
            raise BridgeError("Socket path is too long; use --config in a shorter directory")
        descriptor = os.open(
            self.path.parent / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        handler: RotatingFileHandler | None = None
        server: asyncio.Server | None = None
        reconciler: asyncio.Task[None] | None = None
        watcher: asyncio.Task[None] | None = None
        modules_worker: asyncio.Task[None] | None = None
        observer: asyncio.Task[None] | None = None
        bound = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError("A bridge service already owns this configuration") from None
            check_config_owner(self.config_path)
            config = Config.load(self.config_path)
            atomic_write(
                self.path.parent / "config-owner.json",
                json.dumps({"config_path": str(self.config_path)}),
            )
            if self.path.exists() or self.path.is_symlink():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    raise BridgeError("Refusing to replace a non-socket at the bridge socket path")
                self.path.unlink()
            self.bridge = Bridge(config, self.config_path)
            recovered = self.bridge.recover_pending()
            log_path = self.path.parent / "bridge.log"
            handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3)
            log_path.chmod(0o600)
            self.log.addHandler(handler)
            server = await asyncio.start_unix_server(
                self.handle, path=str(self.path), limit=FRAME_LIMIT
            )
            bound = True
            self.path.chmod(0o600)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stopping.set)
            self.event("started", recovered_pending=recovered, instance_id=self.instance_id)
            stranded = self.bridge.remote_results().get("remote_unlisted_receipts")
            if stranded:
                # A machine left hc_send_machines while it owned open receipts. The bridge
                # starts anyway; `status` and `doctor` keep naming them (docs/RECIPIENTS.md).
                self.event("remote_unlisted_receipts", receipts=stranded)
            clear_startup_failures(self.config_path)
            reconciler = asyncio.create_task(self.reconcile())
            watcher = asyncio.create_task(self.watch_loop())
            modules_worker = asyncio.create_task(self.run_modules())
            observer = asyncio.create_task(self.run_events())
            await self.stopping.wait()
        finally:
            if observer:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
            if modules_worker:
                drain = asyncio.gather(modules_worker, return_exceptions=True)
                try:
                    await asyncio.wait_for(asyncio.shield(drain), timeout=MODULE_SHUTDOWN_TIMEOUT)
                except TimeoutError:
                    self.module_shutdown_timed_out = True
                    modules_worker.cancel()
                    for module in self.bridge.modules:
                        task = self.module_tasks.get(module.name)
                        if task is None or not task.done():
                            self.event(
                                "module_failed",
                                module=module.name,
                                error_type="TimeoutError",
                                shutdown_timeout=MODULE_SHUTDOWN_TIMEOUT,
                            )
            if reconciler:
                reconciler.cancel()
                await asyncio.gather(reconciler, return_exceptions=True)
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if self.clients:
                _, pending = await asyncio.wait(self.clients, timeout=5)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if server:
                server.close()
                await server.wait_closed()
            if bound:
                await self.bridge.close()
                self.bridge.recover_pending()
                self.path.unlink(missing_ok=True)
                self.event("stopped")
            if handler:
                self.log.removeHandler(handler)
                handler.close()
            os.close(descriptor)

    async def run_events(self) -> None:
        """Observe receipts and peers for the event stream. Reads only; dispatch is untouched."""
        from .events.collector import Collector, bridge_sources

        if not self.bridge.event_settings.enabled:
            return
        collector = Collector(
            self.bridge.events,
            bridge_sources(self.bridge),
            self.bridge.event_settings,
            instance_id=self.instance_id,
            settings_error=self.bridge.event_settings_error,
        )
        await collector.run(self.stopping)

    async def _run_module(self, module: ModulePlugin, *, isolate: bool) -> None:
        reporter = getattr(module, "event", None)
        if callable(reporter):
            cast(Any, module).event = self.event
        try:
            await module.run(self.bridge.kernel, self.stopping)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isolate:
                raise
            self.event("module_failed", module=module.name, error_type=type(error).__name__)

    async def run_modules(self) -> None:
        """Run installed modules independently under one cancellable service worker."""
        async with asyncio.TaskGroup() as workers:
            for module in self.bridge.modules:
                self.module_tasks[module.name] = workers.create_task(
                    self._run_module(module, isolate=True),
                    name=f"answerback-module-{module.name}",
                )

    async def run_bus(self) -> None:
        """Compatibility entry point for callers that exercised the former worker directly."""
        await self._run_module(self.bridge.module("bus"), isolate=False)


def run_service(service: Service) -> int:
    """Run one service and bound the loop teardown after an uncooperative module shutdown.

    Installed modules are trusted in-process code, like provider plugins. A module that ignores
    cancellation cannot be forced to finish, so this runner gives cancellation one final second,
    logs the leaked task, and closes the loop rather than letting ``asyncio.run`` wait forever.
    A module shutdown timeout makes the process exit with ``SERVICE_SHUTDOWN_FAILURE``.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(service.serve())
        return SERVICE_SHUTDOWN_FAILURE if service.module_shutdown_timed_out else 0
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            _, still_pending = loop.run_until_complete(
                asyncio.wait(pending, timeout=FINAL_SHUTDOWN_TIMEOUT)
            )
            if still_pending:
                service.event(
                    "shutdown_pending",
                    pending=len(still_pending),
                    timeout=FINAL_SHUTDOWN_TIMEOUT,
                )
        try:
            loop.run_until_complete(
                asyncio.wait_for(loop.shutdown_asyncgens(), timeout=FINAL_SHUTDOWN_TIMEOUT)
            )
        except TimeoutError:
            _LOG.error(
                "Service async generator shutdown timed out after %.1f seconds",
                FINAL_SHUTDOWN_TIMEOUT,
            )
        try:
            shutdown_executor = cast(Any, loop.shutdown_default_executor)
            if sys.version_info >= (3, 12):
                loop.run_until_complete(shutdown_executor(timeout=FINAL_SHUTDOWN_TIMEOUT))
            else:
                loop.run_until_complete(
                    asyncio.wait_for(shutdown_executor(), timeout=FINAL_SHUTDOWN_TIMEOUT)
                )
        except TimeoutError:
            _LOG.error(
                "Service default executor shutdown timed out after %.1f seconds",
                FINAL_SHUTDOWN_TIMEOUT,
            )
        asyncio.set_event_loop(None)
        loop.close()
