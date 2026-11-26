"""Private, bounded, one-request-per-connection local RPC."""

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from .actors.model import ActorError
from .errors import PublicBridgeError, admission_refusal_from_info
from .process import BridgeError

FRAME_LIMIT = 4 * 1024 * 1024
RPC_VERSION = 1
REQUEST_SECONDS = 31
SETUP_SECONDS = 100
READ_METHODS = frozenset(
    {
        "status",
        "fleet_snapshot",
        "peer_search",
        "peers",
        "desktop_list",
        "desktop_sessions",
        "desktop_read",
        "herdr_list_agents",
        "herdr_read_agent",
        "hc_read",
        "rex_read",
        "rex_receive",
        "crew_read",
        "kiro_sessions",
        "workspace_list",
        "task_list",
        "task_get",
        "task_wait",
        "task_for_peer",
        "task_permissions",
        "client_list",
        "client_poll",
        "client_notification_status",
        "provider_list",
        "bus_result",
        "bus_events",
        "bus_status",
        "events_read",
        "events_runs",
        "debug_status",
        "pressure_status",
    }
)


def request_seconds(method: str) -> float:
    if method == "fleet_snapshot":
        return 2
    # Crew setup includes version, owner login and two bounded gateway reads.
    return SETUP_SECONDS if method == "crew_configure" else REQUEST_SECONDS


def timeout_message(method: str) -> str:
    if method == "actor_receive":
        return "Actor read timed out; retry the read. No message was acknowledged or replied to."
    if method in {"actor_send", "actor_ack", "actor_reply"}:
        return "Actor request timed out; retry with the same identity, request ID and content."
    if method in READ_METHODS:
        return f"Read operation {method} timed out; retry the read. No work was submitted."
    if method == "crew_configure":
        return (
            "Crew setup timed out; settings may have been saved. Check status and the Crew "
            "gateway, then rerun crew-configure. Setup does not submit agent work."
        )
    return "Request timed out; delivery may be uncertain. Inspect the task receipt."


class TransportUnavailable(BridgeError):
    """No usable reply was received; a mutation might already have taken effect."""

    def __init__(self, message: str, *, config_path: Path | None = None) -> None:
        # `config_path` travels beside the message, never inside it: the
        # message is matched by exact string in the public catalogue
        # (errors.py), so a caller-specific path cannot live in str(self).
        super().__init__(message)
        self.config_path = config_path


class Operations(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


def runtime_dir(config_path: Path) -> Path:
    return config_path.expanduser().resolve().parent / "runtime"


def socket_path(config_path: Path) -> Path:
    return runtime_dir(config_path) / "bridge.sock"


def check_config_owner(config_path: Path) -> None:
    owner = runtime_dir(config_path) / "config-owner.json"
    if owner.exists() and json.loads(owner.read_text()) != {
        "config_path": str(config_path.expanduser().resolve())
    }:
        raise BridgeError(
            "This configuration directory belongs to another bridge. Use a separate "
            "configuration directory to preserve session routing and task receipts."
        )


def make_private_directories(path: Path, *, exist_ok: bool) -> None:
    """Create ``path`` and every missing folder above it with mode 0700, whatever the umask.

    ``Path.mkdir(parents=True)`` gives the mode to the last folder only; under umask 002 a
    folder above it is group-writable and the configuration path check refuses it.
    """
    try:
        path.mkdir(mode=0o700, exist_ok=exist_ok)
    except FileNotFoundError:
        if path.parent == path:
            raise
        # Another creator may make a folder in between: accepted, and left as it made it.
        make_private_directories(path.parent, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=exist_ok)


def private_directory(path: Path) -> None:
    make_private_directories(path, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BridgeError("Bridge runtime must be a directory owned by the current user")
    path.chmod(0o700)


def encode(value: dict[str, Any]) -> bytes:
    data = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    if len(data) > FRAME_LIMIT:
        raise BridgeError("RPC message exceeds the 4 MiB limit")
    return data


def reply_error(info: Any, method: str) -> Exception:
    """Missing/unknown metadata is private; the mandatory raw error is never authority."""
    if isinstance(info, dict):
        kind = info.get("kind")
        if kind == "actor" and set(info) == {"kind", "code"}:
            try:
                return ActorError(info["code"])
            except (TypeError, ValueError):
                pass
        if kind == "public" and set(info) == {"kind", "code"}:
            code = info["code"]
            if isinstance(code, str):
                try:
                    return PublicBridgeError(code)
                except ValueError:
                    pass
        elif kind == "admission_refused":
            refusal = admission_refusal_from_info(info)
            if refusal is not None:
                return refusal
        elif kind == "timeout" and set(info) == {"kind"}:
            return TransportUnavailable(timeout_message(method))
    return BridgeError("Bridge request failed; inspect service logs and the task receipt.")


class RemoteBridge:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        check_config_owner(self.config_path)
        self.path = socket_path(config_path)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(request_seconds(method) + 1):
                reader, writer = await asyncio.open_unix_connection(
                    str(self.path), limit=FRAME_LIMIT
                )
                try:
                    writer.write(
                        encode(
                            {
                                "version": RPC_VERSION,
                                "method": method,
                                "params": params,
                                "config_path": str(self.config_path),
                            }
                        )
                    )
                    await writer.drain()
                    raw = await reader.readline()
                    if not raw:
                        raise TransportUnavailable(
                            "Bridge disconnected; delivery may be uncertain. Inspect the receipt."
                        )
                    reply = json.loads(raw)
                    if reply.get("version") != RPC_VERSION:
                        raise PublicBridgeError("protocol_incompatible")
                    if not reply.get("ok"):
                        raise reply_error(reply.get("error_info"), method)
                    return reply["result"]
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise TransportUnavailable(
                "Bridge is stopped. Run answerback start first.", config_path=self.config_path
            ) from error
        except ConnectionError as error:
            raise TransportUnavailable(
                "Bridge disconnected; delivery may be uncertain. Inspect the receipt."
            ) from error
        except TimeoutError as error:
            raise TransportUnavailable(timeout_message(method)) from error

    async def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.unreachable_status()
        try:
            async with asyncio.timeout(2):
                return await self.call("status", {})
        except TransportUnavailable:
            return self.unreachable_status()
        except TimeoutError:
            return self.unreachable_status()

    def unreachable_status(self) -> dict[str, Any]:
        import fcntl

        owned = False
        try:
            with (self.path.parent / "owner.lock").open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    owned = True
        except FileNotFoundError:
            pass
        return {
            "running": owned,
            "accepting": False,
            "state": "unresponsive_or_starting" if owned else "stopped",
            "socket": str(self.path),
        }
