"""Bridge-only lifecycle; never signals a PID read from a file."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .process import BridgeError, module_launch_argv
from .transport import RemoteBridge, TransportUnavailable, private_directory, runtime_dir

FLEET_NOT_CONFIGURED = "fleet writer not configured; run `answerback fleet configure`"
FLEET_SETTINGS_INVALID = "fleet writer settings invalid; run `answerback fleet configure`"


def service_command(config_path: Path) -> list[str]:
    return module_launch_argv(config_path.expanduser().resolve(), "serve")


async def _start_fleet_writer(
    config_path: Path, status: dict[str, Any], *, standalone: bool
) -> dict[str, Any]:
    """Start the configured writer only after the bridge reports readiness."""
    from .fleet.settings import SettingsError
    from .fleet.settings import read as read_settings
    from .fleet.supervision import FleetWriterSupervisor

    supervisor = FleetWriterSupervisor(config_path)
    if not supervisor.settings_path.is_file():
        writer_status = await supervisor.status()
        writer_status["message"] = FLEET_NOT_CONFIGURED
    elif not await asyncio.to_thread(
        _settings_usable, read_settings, supervisor.settings_path, SettingsError
    ):
        # A present but unusable file would make the supervised writer exit 2 and launchd
        # relaunch it every ten seconds for good (T-186 review R3): refuse to start it.
        writer_status = await supervisor.status()
        writer_status["message"] = FLEET_SETTINGS_INVALID
    elif standalone:
        writer_status = await supervisor.status()
        writer_status["message"] = "fleet writer not started with --standalone"
    else:
        await supervisor.start()
        writer_status = await supervisor.status()
    return {**status, "fleet_writer": writer_status}


def _settings_usable(read: Any, path: Path, error: type[Exception]) -> bool:
    """True when the writer's settings file reads as valid settings."""
    try:
        read(path)
    except error:
        return False
    return True


async def fleet_writer_status(config_path: Path) -> dict[str, Any]:
    """Return the configured fleet writer's native supervision state."""
    from .fleet.supervision import FleetWriterSupervisor

    return await FleetWriterSupervisor(config_path).status()


async def start(config_path: Path, *, standalone: bool = False) -> dict[str, Any]:
    config_path = await asyncio.to_thread(lambda: config_path.expanduser().resolve())
    Config.load(config_path)
    client = RemoteBridge(config_path)
    status = await client.status()
    from .supervision import NativeSupervisor

    supervisor = NativeSupervisor(config_path)
    upgrading = not standalone and supervisor.legacy_installed()
    if status.get("accepting") and not upgrading:
        return await _start_fleet_writer(config_path, status, standalone=standalone)
    private_directory(runtime_dir(config_path))
    if upgrading:
        await supervisor.start()
    elif status["running"]:
        pass  # Another start owns the lock; wait for its readiness handshake below.
    elif standalone:
        await asyncio.to_thread(launch_standalone, config_path)
    else:
        await supervisor.start()
    for _ in range(100):
        status = await client.status()
        if status.get("accepting"):
            return await _start_fleet_writer(config_path, status, standalone=standalone)
        await asyncio.sleep(0.1)
    raise BridgeError("Bridge did not become ready; inspect answerback logs and launcher.log")


def launch_standalone(config_path: Path) -> None:
    log_path = runtime_dir(config_path) / "launcher.log"
    with log_path.open("ab") as output:
        log_path.chmod(0o600)
        # Detached bridge owns its own session. Stop uses the socket, never this PID.
        subprocess.Popen(
            service_command(config_path),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            close_fds=True,
            cwd=str(config_path.parent),
        )


async def stop(config_path: Path) -> dict[str, Any]:
    from .fleet.supervision import FleetWriterSupervisor
    from .supervision import NativeSupervisor

    client = RemoteBridge(config_path)
    # The writer depends on the bridge, so remove its supervision before the bridge.
    await FleetWriterSupervisor(config_path).stop_if_installed()
    # Remove bridge supervision before asking it to stop so an explicit stop cannot
    # be restarted by the OS.
    supervisor = NativeSupervisor(config_path)
    await supervisor.stop_if_installed()
    status = await client.status()
    if status["running"]:
        try:
            await client.call("stop", {})
        except TransportUnavailable:
            pass  # Native supervision or a simultaneous stop may already have closed it.
    for _ in range(100):
        status = await client.status()
        if not status["running"]:
            return status
        await asyncio.sleep(0.1)
    raise BridgeError("Bridge is still draining; no provider process was stopped")


async def logs(config_path: Path, *, follow: bool, lines: int = 50) -> dict[str, Any]:
    if not 1 <= lines <= 1000:
        raise ValueError("lines must be between 1 and 1000")
    path = runtime_dir(config_path) / "bridge.log"
    entries = []
    if path.exists():
        entries = [json.loads(line) for line in path.read_text().splitlines()[-lines:]]
    if not follow:
        return {"entries": entries}
    for entry in entries:
        print(json.dumps(entry), flush=True)
    offset = path.stat().st_size if path.exists() else 0
    inode = path.stat().st_ino if path.exists() else None
    while True:
        await asyncio.sleep(0.5)
        try:
            info = path.stat()
            if info.st_ino != inode or info.st_size < offset:
                offset = 0
            inode = info.st_ino
            with path.open() as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith("\n"):
                        break
                    print(line.rstrip(), flush=True)
                    offset = stream.tell()
        except FileNotFoundError:
            continue
