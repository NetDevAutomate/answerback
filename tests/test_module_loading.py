"""Installed modules contribute surfaces, lifecycle workers, and maintenance policy."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from textwrap import dedent

import pytest

import answerback_bridge.cli as cli
import answerback_bridge.runtime as runtime
from answerback_bridge.bus.extra import MISSING_BUS_EXTRA
from answerback_bridge.bus.maintenance import BusMaintenance
from answerback_bridge.config import Config
from answerback_bridge.maintenance_policy import Policy
from answerback_bridge.mcp_server import TOOL_ORDER
from answerback_bridge.plugin import MaintenanceContribution, PluginLoadError, SurfaceCollision
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import StoreHookRegistry


@pytest.fixture(autouse=True)
def reset_module_discovery_cache() -> None:
    """Synthetic entry-point modules must not reuse a prior test's discovery."""
    from answerback_bridge.plugin import reset_plugin_cache

    reset_plugin_cache()
    yield
    reset_plugin_cache()


def _write_module_distribution(
    root: Path,
    *,
    verb: str = "fixture_echo",
    long_poll_verbs: str = "{}",
    module_name: str | None = None,
    plugin_name: str = "fixture",
    distribution_name: str = "answerback_fixture_module",
) -> None:
    module_name = module_name or f"fixture_module_{verb}"
    (root / f"{module_name}.py").write_text(
        f"""from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from answerback_bridge.plugin import (
    PLUGIN_API,
    CliRunner,
    KernelContext,
    RpcHandler,
)


class Plugin:
    api_version = PLUGIN_API
    name = {plugin_name!r}
    extra = None
    long_poll_verbs = {long_poll_verbs}

    async def echo(self, text: str = "") -> dict[str, str]:
        return {{"echo": text}}

    def rpc(self) -> Mapping[str, RpcHandler]:
        return {{{verb!r}: self.echo}}

    def cli(self, commands: Any, run: CliRunner) -> None:
        command = commands.add_parser("fixture-module", help="Exercise a loaded module")
        command.add_argument("text")

        async def handler(args: Any, bridge: Any) -> dict[str, str]:
            return await bridge.call({verb!r}, {{"text": args.text}})

        run(command, handler)

    async def run(self, ctx: KernelContext, stopping: asyncio.Event) -> None:
        marker = ctx.config_path.parent / "fixture-module-lifecycle"
        marker.write_text("started")
        try:
            await stopping.wait()
        finally:
            marker.write_text("stopped")


def make_plugin() -> Plugin:
    return Plugin()
"""
    )
    metadata = root / f"{distribution_name}-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name.replace('_', '-')}\nVersion: 1.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        f"[answerback.modules]\n{plugin_name} = {module_name}:make_plugin\n"
    )


@pytest.fixture
def module_distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_module_distribution(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.metadata.MetadataPathFinder.invalidate_caches()
    return tmp_path


async def test_installed_module_contributes_rpc_cli_and_lifecycle(
    module_distribution: Path,
) -> None:
    args = cli.parser().parse_args(["fixture-module", "hello"])
    assert args.command == "fixture-module"

    with tempfile.TemporaryDirectory(prefix="ab-module-", dir="/tmp") as folder:
        config_path = await asyncio.to_thread(Path(folder).resolve) / "config.json"
        Config(provider_selection=[]).save(config_path)
        service = Service(config_path)
        worker = asyncio.create_task(service.serve())
        marker = config_path.parent / "fixture-module-lifecycle"
        try:
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            assert marker.read_text() == "started"
            assert await service.bridge.call("fixture_echo", {"text": "hello"}) == {"echo": "hello"}
            service.stopping.set()
            await worker
            assert marker.read_text() == "stopped"
        finally:
            service.stopping.set()
            await asyncio.gather(worker, return_exceptions=True)


async def test_one_module_failure_does_not_cancel_sibling_workers(tmp_path: Path) -> None:
    started = asyncio.Event()

    class FailingModule:
        name = "failing"

        async def run(self, ctx: object, stopping: asyncio.Event) -> None:
            raise RuntimeError("fixture failure")

    class WaitingModule:
        name = "waiting"

        async def run(self, ctx: object, stopping: asyncio.Event) -> None:
            started.set()
            await stopping.wait()

    config_path = tmp_path / "config.json"
    bridge = Bridge(Config(provider_selection=[]), config_path)
    service = Service(config_path)
    service.bridge = bridge
    bridge.modules = (FailingModule(), WaitingModule())  # type: ignore[assignment]
    events: list[tuple[str, dict[str, object]]] = []
    service.event = lambda name, **fields: events.append((name, fields))
    worker = asyncio.create_task(service.run_modules())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0)
        assert not worker.done()
        assert events == [("module_failed", {"module": "failing", "error_type": "RuntimeError"})]
    finally:
        service.stopping.set()
        await worker
        await bridge.close()


async def test_module_shutdown_timeout_does_not_hang_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    class StubbornModule:
        name = "stubborn"

        async def run(self, ctx: object, stopping: asyncio.Event) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()

    with tempfile.TemporaryDirectory(prefix="ab-module-", dir="/tmp") as folder:
        config_path = await asyncio.to_thread(Path(folder).resolve) / "config.json"
        config = Config(provider_selection=[])
        config.save(config_path)
        bridge = Bridge(config, config_path)
        bridge.modules = (StubbornModule(),)  # type: ignore[assignment]
        service = Service(config_path)
        events: list[tuple[str, dict[str, object]]] = []
        service.event = lambda name, **fields: events.append((name, fields))
        monkeypatch.setattr(runtime, "Bridge", lambda *_args: bridge)
        monkeypatch.setattr(runtime, "MODULE_SHUTDOWN_TIMEOUT", 0.05, raising=False)
        worker = asyncio.create_task(service.serve())
        completed_in_time = True
        try:
            await asyncio.wait_for(started.wait(), 1)
            service.stopping.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker), 0.2)
            except TimeoutError:
                completed_in_time = False
        finally:
            release.set()
            await asyncio.wait_for(worker, 1)
            await bridge.close()

    assert cancelled.is_set()
    assert completed_in_time, "A module that swallowed cancellation hung service shutdown"
    assert (
        "module_failed",
        {
            "module": "stubborn",
            "error_type": "TimeoutError",
            "shutdown_timeout": 0.05,
        },
    ) in events


async def test_normal_bus_close_finishes_within_module_shutdown_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowCloseTransport:
        async def __aenter__(self) -> SlowCloseTransport:
            return self

        async def __aexit__(self, *_: object) -> None:
            await asyncio.sleep(2.5)
            closed.set()

    class BusModule:
        name = "bus"

        async def run(self, ctx: object, stopping: asyncio.Event) -> None:
            async with SlowCloseTransport():
                entered.set()
                await stopping.wait()

    with tempfile.TemporaryDirectory(prefix="ab-normal-close-", dir="/tmp") as folder:
        config_path = Path(folder) / "config.json"
        config = Config(provider_selection=[])
        config.save(config_path)
        bridge = Bridge(config, config_path)
        bridge.modules = (BusModule(),)  # type: ignore[assignment]
        service = Service(config_path)
        events: list[tuple[str, dict[str, object]]] = []
        service.event = lambda name, **fields: events.append((name, fields))
        monkeypatch.setattr(runtime, "Bridge", lambda *_args: bridge)
        worker = asyncio.create_task(service.serve())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            started = time.monotonic()
            service.stopping.set()
            await asyncio.wait_for(worker, runtime.MODULE_SHUTDOWN_TIMEOUT)
            elapsed = time.monotonic() - started
        finally:
            service.stopping.set()
            await asyncio.gather(worker, return_exceptions=True)
            await bridge.close()

    assert closed.is_set()
    assert 2.5 <= elapsed < runtime.MODULE_SHUTDOWN_TIMEOUT
    assert not [event for event in events if event[0] == "module_failed"]


def test_run_service_logs_pending_tasks_to_the_bridge_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapturingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    class PendingTask:
        def cancel(self) -> None:
            return None

    class PendingService:
        module_shutdown_timed_out = False

        def __init__(self, log: logging.Logger) -> None:
            self.log = log

        def event(self, event: str, **fields: object) -> None:
            self.log.info(json.dumps({"event": event, **fields}))

        async def serve(self) -> None:
            return None

    bridge_handler = CapturingHandler()
    last_resort = CapturingHandler()
    bridge_log = logging.getLogger("answerback_bridge.test-pending-teardown")
    original_level = bridge_log.level
    bridge_log.setLevel(logging.INFO)
    bridge_log.handlers = [bridge_handler]
    bridge_log.propagate = False
    root_handlers = logging.root.handlers[:]
    logging.root.handlers.clear()
    pending = PendingTask()

    async def wait(
        tasks: set[PendingTask], **kwargs: float
    ) -> tuple[set[PendingTask], set[PendingTask]]:
        assert tasks == {pending}
        assert kwargs == {"timeout": runtime.FINAL_SHUTDOWN_TIMEOUT}
        return set(), set(tasks)

    monkeypatch.setattr(runtime.asyncio, "all_tasks", lambda _loop: {pending})
    monkeypatch.setattr(runtime.asyncio, "wait", wait)
    monkeypatch.setattr(logging, "lastResort", last_resort)
    try:
        assert runtime.run_service(PendingService(bridge_log)) == 0
    finally:
        logging.root.handlers[:] = root_handlers
        bridge_log.handlers.clear()
        bridge_log.setLevel(original_level)

    record = json.loads(bridge_handler.records[0].getMessage())
    assert record == {
        "event": "shutdown_pending",
        "pending": 1,
        "timeout": runtime.FINAL_SHUTDOWN_TIMEOUT,
    }
    assert last_resort.records == []


async def test_lifecycle_logs_parses_shutdown_pending_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import lifecycle

    record = {
        "time": 1.0,
        "event": "shutdown_pending",
        "pending": 1,
        "timeout": runtime.FINAL_SHUTDOWN_TIMEOUT,
    }
    (tmp_path / "bridge.log").write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(lifecycle, "runtime_dir", lambda _config_path: tmp_path)

    assert await lifecycle.logs(tmp_path / "config.json", follow=False) == {"entries": [record]}


async def test_lifecycle_logs_reads_service_shutdown_pending_event(tmp_path: Path) -> None:
    from answerback_bridge import lifecycle

    config_path = tmp_path / "config.json"
    log_path = runtime.runtime_dir(config_path) / "bridge.log"
    log_path.parent.mkdir()
    service = Service(config_path)
    handler = runtime.RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3)
    service.log.addHandler(handler)
    try:
        service.event(
            "shutdown_pending",
            pending=1,
            timeout=runtime.FINAL_SHUTDOWN_TIMEOUT,
        )
        handler.flush()
        entries = (await lifecycle.logs(config_path, follow=False))["entries"]
    finally:
        service.log.removeHandler(handler)
        handler.close()

    assert len(entries) == 1
    assert entries[0]["event"] == "shutdown_pending"
    assert entries[0]["pending"] == 1
    assert entries[0]["timeout"] == runtime.FINAL_SHUTDOWN_TIMEOUT
    assert isinstance(entries[0]["time"], float)


def test_run_service_finalizes_suspended_async_generators() -> None:
    class AsyncGeneratorService:
        module_shutdown_timed_out = False

        def __init__(self) -> None:
            self.finalized = False
            self.generator: object | None = None

        async def serve(self) -> None:
            async def suspended():
                try:
                    yield
                finally:
                    self.finalized = True

            self.generator = suspended()
            await anext(self.generator)

    service = AsyncGeneratorService()

    assert runtime.run_service(service) == 0
    assert service.finalized


def test_serve_entrypoint_exits_after_stubborn_module_timeout() -> None:
    script = dedent(
        """
        import asyncio
        import json
        import os
        import signal
        import sys
        from pathlib import Path

        import answerback_bridge.cli as cli
        import answerback_bridge.runtime as runtime
        from answerback_bridge.config import Config
        from answerback_bridge.service import Bridge

        class StubbornModule:
            name = "stubborn"

            async def run(self, ctx, stopping):
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        pass

        root = Path(sys.argv[1])
        config_path = root / "config.json"
        config = Config(provider_selection=[])
        config.save(config_path)
        bridge = Bridge(config, config_path)
        bridge.modules = (StubbornModule(),)
        runtime.Bridge = lambda *_args: bridge
        original_event = runtime.Service.event

        def signal_after_started(self, event, **fields):
            original_event(self, event, **fields)
            if event == "started":
                os.kill(os.getpid(), signal.SIGTERM)

        runtime.Service.event = signal_after_started
        sys.argv = ["answerback", "serve", "--config", str(config_path)]
        try:
            cli.main()
        except SystemExit as error:
            log = (root / "runtime" / "bridge.log").read_text()
            print(json.dumps({"code": error.code, "log": log}))
        """
    )
    with tempfile.TemporaryDirectory(prefix="ab-stubborn-", dir="/tmp") as folder:
        started = time.monotonic()
        completed = subprocess.run(  # noqa: ASYNC221, S603 -- test executes the current interpreter only
            ["timeout", "10s", sys.executable, "-c", script, folder],
            capture_output=True,
            text=True,
            timeout=11,
        )
        elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["code"] == runtime.SERVICE_SHUTDOWN_FAILURE
    assert elapsed < 10
    assert '"event": "module_failed"' in result["log"]
    assert f'"shutdown_timeout": {runtime.MODULE_SHUTDOWN_TIMEOUT}' in result["log"]


def test_module_verb_collision_with_kernel_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module_distribution(tmp_path, verb="peer_submit")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    with pytest.raises(SurfaceCollision, match=r"verb 'peer_submit'.*plugin fixture.*kernel"):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


def test_module_unknown_long_poll_verb_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module_distribution(
        tmp_path,
        verb="unknown_candidate",
        long_poll_verbs="{'unknown_poll': 1}",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    with pytest.raises(
        PluginLoadError,
        match=r"Module 'fixture' declares unknown long-poll verbs \['unknown_poll'\]",
    ):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


@pytest.mark.parametrize(
    ("case", "capacity"),
    (
        ("zero", 0),
        ("negative", -1),
        ("string", "3"),
        ("float", 2.5),
        ("boolean", True),
        ("none", None),
    ),
)
def test_module_long_poll_capacity_requires_positive_integer(
    case: str, capacity: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verb = f"fixture_capacity_{case}"
    _write_module_distribution(tmp_path, verb=verb, long_poll_verbs=repr({verb: capacity}))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    with pytest.raises(PluginLoadError, match=r"fixture.*invalid long-poll capacity"):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


def test_module_long_poll_verbs_requires_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module_distribution(
        tmp_path,
        verb="fixture_mapping_required",
        long_poll_verbs="['fixture_mapping_required']",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    with pytest.raises(PluginLoadError, match="must be a verb-to-capacity mapping"):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


def test_module_long_poll_verbs_must_be_unique_across_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_module_distribution(
        first,
        verb="shared_long_poll",
        long_poll_verbs="{'shared_long_poll': 1}",
        module_name="first_module",
        plugin_name="first",
        distribution_name="answerback_first_module",
    )
    _write_module_distribution(
        second,
        verb="shared_long_poll",
        long_poll_verbs="{'shared_long_poll': 1}",
        module_name="second_module",
        plugin_name="second",
        distribution_name="answerback_second_module",
    )
    monkeypatch.syspath_prepend(str(first))
    monkeypatch.syspath_prepend(str(second))
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    with pytest.raises(PluginLoadError, match=r"duplicate long-poll verb 'shared_long_poll'"):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


class _RequestReader:
    def __init__(self, request: dict[str, object]) -> None:
        self.request = (json.dumps(request) + "\n").encode()

    async def readline(self) -> bytes:
        return self.request


class _ResponseWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def test_bus_actor_receive_capacity_preserves_actor_wire_refusal(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    service = Service(tmp_path / "config.json")
    service.bridge = bridge

    async def call(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal active
        assert (method, params) == ("actor_receive", {})
        active += 1
        if active == 16:
            entered.set()
        try:
            await release.wait()
            return {"messages": []}
        finally:
            active -= 1

    bridge.call = call

    async def request() -> _ResponseWriter:
        writer = _ResponseWriter()
        await service.handle(
            _RequestReader(
                {
                    "version": 1,
                    "config_path": str(service.config_path),
                    "method": "actor_receive",
                    "params": {},
                }
            ),  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
        )
        return writer

    waiters = [asyncio.create_task(request()) for _ in range(16)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        refused = await asyncio.wait_for(request(), 0.1)
        assert json.loads(refused.frames[0]) == {
            "version": 1,
            "ok": False,
            "error": "Actor capacity limit reached",
            "error_info": {"kind": "actor", "code": "capacity"},
        }
    finally:
        release.set()
        await asyncio.gather(*waiters)
        await bridge.close()


async def test_module_long_poll_capacity_is_kernel_enforced(tmp_path: Path) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    active = 0

    class LongPollModule:
        METHODS = frozenset({"fast_submit"})
        long_poll_verbs = {"fast_submit": 20}

        async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal active
            assert (method, params) == ("fast_submit", {})
            active += 1
            if active == 20:
                started.set()
            try:
                await release.wait()
                return {"ok": True}
            finally:
                active -= 1

    service = Service(tmp_path / "config.json")
    service.bridge = LongPollModule()  # type: ignore[assignment]

    async def request() -> _ResponseWriter:
        writer = _ResponseWriter()
        await service.handle(
            _RequestReader(
                {
                    "version": 1,
                    "config_path": str(service.config_path),
                    "method": "fast_submit",
                    "params": {},
                }
            ),  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
        )
        return writer

    waiters = [asyncio.create_task(request()) for _ in range(20)]
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert active == 20
        assert not service.slots.locked()
        refused = await asyncio.wait_for(request(), 0.1)
        assert json.loads(refused.frames[0]) == {
            "version": 1,
            "ok": False,
            "error": "Bridge request failed; inspect service logs and the task receipt.",
            "error_info": {"kind": "internal"},
        }
        assert active == 20
        assert not service.slots.locked()
    finally:
        release.set()
        await asyncio.gather(*waiters)


def test_bus_rpc_behavior_and_mcp_names_match_main_when_extra_is_absent(tmp_path: Path) -> None:
    script = dedent(
        """
        import asyncio
        import json
        import sys
        from pathlib import Path

        sys.modules["nats"] = None
        sys.modules["psutil"] = None

        from mcp import Client

        from answerback_bridge.bus.config import BusConfig
        from answerback_bridge.config import Config
        from answerback_bridge.mcp_server import create_server
        from answerback_bridge.runtime import Service
        from answerback_bridge.service import Bridge

        async def main() -> None:
            root = Path(sys.argv[1])
            config_path = root / "config.json"
            config = Config(
                provider_selection=["kiro"],
                kiro_workspaces={"worker": str(root)},
            )
            config.save(config_path)
            BusConfig.create(config_path, server_binary=sys.executable)
            bridge = Bridge(config, config_path)
            service = Service(config_path)
            service.bridge = bridge
            try:
                await service.run_bus()
                submit = await bridge.call(
                    "bus_submit",
                    {
                        "peer_id": "kiro:worker",
                        "project": str(root),
                        "message": "Review",
                        "request_id": "extra-absent",
                    },
                )
                result = await bridge.call(
                    "bus_result", {"command_id": submit["command_id"]}
                )
                events = await bridge.call("bus_events", {})
                cancelled = await bridge.call(
                    "bus_cancel", {"command_id": submit["command_id"]}
                )
                status = await bridge.call("bus_status", {})
                calls = {
                    "bus_submit": {
                        "peer_id": "kiro:worker",
                        "project": str(root),
                        "message": "Review",
                        "request_id": "extra-absent-mcp",
                    },
                    "bus_result": {"command_id": submit["command_id"]},
                    "bus_events": {},
                    "bus_cancel": {"command_id": submit["command_id"]},
                    "bus_status": {},
                }
                async with Client(create_server(bridge)) as client:
                    tools = await client.list_tools()
                    mcp_errors = {
                        verb: (await client.call_tool(verb, params)).is_error
                        for verb, params in calls.items()
                    }
                print(
                    json.dumps(
                        {
                            "bus_submit": {
                                key: submit[key] for key in ("delivery", "task_id")
                            },
                            "bus_result": {
                                key: result[key] for key in ("delivery", "task_id")
                            },
                            "bus_events": [
                                event["type"] for event in events["events"]
                            ],
                            "bus_cancel": {
                                key: cancelled[key]
                                for key in ("delivery", "task_id")
                            },
                            "bus_status": {
                                "configured": status["configured"],
                                "connected": status["coordinator"]["connected"],
                                "last_error": status["coordinator"]["last_error"],
                            },
                            "mcp_names": [tool.name for tool in tools.tools],
                            "mcp_errors": mcp_errors,
                        }
                    )
                )
            finally:
                await bridge.close()

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    behavior = json.loads(completed.stdout)
    assert behavior == {
        "bus_submit": {"delivery": "queued", "task_id": None},
        "bus_result": {"delivery": "queued", "task_id": None},
        "bus_events": ["command.accepted"],
        "bus_cancel": {"delivery": "cancelled", "task_id": None},
        "bus_status": {
            "configured": True,
            "connected": False,
            "last_error": MISSING_BUS_EXTRA,
        },
        "mcp_names": list(TOOL_ORDER),
        "mcp_errors": dict.fromkeys(
            ("bus_submit", "bus_result", "bus_events", "bus_cancel", "bus_status"),
            False,
        ),
    }


class FixtureMaintenance:
    def __init__(self, *tables: tuple[str, str]) -> None:
        self.tables = frozenset(tables)

    def inspect(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {}


def _maintenance_policy(*contributors: MaintenanceContribution) -> Policy:
    return Policy(
        sqlite3.connect(":memory:"),
        {},
        0,
        None,
        0,
        StoreHookRegistry(),
        contributors,
    )


def test_maintenance_claims_stay_inside_the_module_store_namespace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    third_party = MaintenanceContribution("rogue", False, FixtureMaintenance(("tasks", "tasks")))
    caplog.set_level(logging.WARNING, logger="answerback_bridge.maintenance_policy")

    policy = _maintenance_policy(third_party)

    assert policy.contributors == {}
    assert "outside its store namespace" in caplog.text
    built_in = MaintenanceContribution("rogue", True, FixtureMaintenance(("tasks", "tasks")))
    with pytest.raises(ValueError, match="outside its store namespace"):
        _maintenance_policy(built_in)


def test_third_party_duplicate_maintenance_claim_is_warned_and_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = MaintenanceContribution("bus", True, BusMaintenance())
    third_party = MaintenanceContribution("bus", False, FixtureMaintenance(("bus", "commands")))
    caplog.set_level(logging.WARNING, logger="answerback_bridge.maintenance_policy")

    policy = _maintenance_policy(bus, third_party)

    assert policy.contributors[("bus", "commands")] is bus
    assert "Skipping third-party module 'bus'" in caplog.text
    with pytest.raises(ValueError, match="two built-in contributors"):
        _maintenance_policy(
            bus,
            MaintenanceContribution("bus", True, FixtureMaintenance(("bus", "commands"))),
        )


def test_module_long_poll_capacity_total_cannot_exceed_connection_cap() -> None:
    class ExactCapacity:
        name = "exact"
        long_poll_verbs = {"exact_poll": 32}

        def rpc(self) -> dict[str, object]:
            return {"exact_poll": object()}

    class ExcessCapacity:
        name = "excess"
        long_poll_verbs = {"excess_poll": 33}

        def rpc(self) -> dict[str, object]:
            return {"excess_poll": object()}

    from answerback_bridge.plugin import module_long_poll_verbs

    assert module_long_poll_verbs((ExactCapacity(),)) == {"exact_poll": 32}  # type: ignore[arg-type]
    with pytest.raises(PluginLoadError, match=r"Module 'excess'.*connection cap 32"):
        module_long_poll_verbs((ExcessCapacity(),))  # type: ignore[arg-type]
