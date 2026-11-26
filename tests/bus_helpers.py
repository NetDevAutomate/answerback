import asyncio
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from codex_herdr_bridge.bus.config import BusConfig


def unused_server_binary(root: Path) -> str:
    """Provide an executable path for config-only tests; never start this as a broker."""
    executable = root / "unused-nats-server"
    executable.symlink_to(Path(sys.executable).resolve())
    return str(executable)


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _mise_directories() -> tuple[list[Path], set[Path]]:
    default = _absolute("~/.local/share/mise")
    data_directories = [default]
    if value := os.environ.get("MISE_DATA_DIR"):
        configured = _absolute(value)
        data_directories.insert(0, configured)
    shim_directories = {directory / "shims" for directory in data_directories}
    if value := os.environ.get("MISE_SHIMS_DIR"):
        shim_directories.add(_absolute(value))
    return data_directories, shim_directories


def _plain_server_binary(
    candidate: Path, shim_directories: set[Path]
) -> tuple[Path | None, str | None]:
    """Return the resolved plain executable, or why an existing candidate was rejected."""
    if not os.path.lexists(candidate):
        return None, None
    if candidate.parent.absolute() in shim_directories:
        return None, "in a mise shim directory"
    try:
        resolved = candidate.resolve(strict=True)
        if resolved.name == "mise":
            return None, f"resolves to mise ({resolved})"
        if not resolved.is_file():
            return None, f"not a regular file ({resolved})"
        if not os.access(resolved, os.X_OK):
            return None, f"not executable ({resolved})"
        with resolved.open("rb") as source:
            if source.read(2) == b"#!":
                return None, "script (#!), not a plain executable"
    except OSError as error:
        return None, f"unreadable ({type(error).__name__}: {error.strerror or error})"
    return resolved, None


def _server_binary() -> tuple[str | None, list[str]]:
    """Find a plain nats-server executable without running a version manager.

    Returns the binary, or None with one line per rejected candidate: a wrapper
    script installed as nats-server is refused, and the skip says so.
    """
    data_directories, shim_directories = _mise_directories()
    rejected: list[str] = []
    candidates = [Path(directory or os.curdir) / "nats-server" for directory in os.get_exec_path()]
    for data_directory in data_directories:
        installs = data_directory / "installs" / "nats-server"
        candidates.extend(sorted(installs.glob("*/nats-server"), reverse=True))
    for candidate in candidates:
        resolved, reason = _plain_server_binary(candidate, shim_directories)
        if resolved is not None:
            return str(resolved), rejected
        if reason is not None:
            rejected.append(f"{candidate}: {reason}")
    return None, rejected


class BrokerFixture:
    def __init__(self, root: Path) -> None:
        pytest.importorskip(
            "nats", reason="Tests require nats-py, the answerback[bus] extra; see docs/AGENT-BUS.md"
        )
        if os.environ.get("ANSWERBACK_TEST_OFFLINE") == "1":
            pytest.skip("ANSWERBACK_TEST_OFFLINE=1 disables the private nats-server fixture")
        binary, rejected = _server_binary()
        if binary is None:
            reason = "Tests require nats-server; see docs/AGENT-BUS.md"
            if rejected:
                reason += "; rejected candidates: " + "; ".join(rejected)
            pytest.skip(reason)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.config = BusConfig.create(root / "config.json", server_binary=binary, port=port)
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            self.config.server_binary,
            "-c",
            str(self.config.broker_config),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError("Test NATS server exited before readiness")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.config.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.02)
        raise TimeoutError("Test NATS server did not start")

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
