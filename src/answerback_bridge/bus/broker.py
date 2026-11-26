"""Supervise only this configuration's authenticated loopback NATS broker."""

import asyncio
import json
import os
import plistlib
import re
from functools import partial
from pathlib import Path
from typing import Any

import nats
import nats.errors
import psutil
from nats.aio.client import Client
from nats.aio.transport import TcpTransport

from ..process import BridgeError, CommandError, run
from ..supervision import SYSTEMD_INSTALL, NativeSupervisor, systemd_working_directory
from ..transport import private_directory
from .config import BusConfig, launcher_shim


class BrokerIdentityError(nats.errors.Error):
    """A peer refusal carrying only a fixed, credential-free diagnostic."""


def _same_executable(observed: str, configured: str) -> bool:
    observed_path = Path(observed)
    configured_path = Path(configured)
    if observed_path.resolve() == configured_path.resolve():
        return True
    return observed_path.samefile(configured_path)


def _owned_connection(config: BusConfig, client: Client) -> bool:
    """Bind the connected TCP peer to this user's configured broker process."""
    # nats-py 2.16 keeps the connected writer here. Fail closed if its transport
    # changes; inspecting a different probe socket would leave a replacement race.
    transport = client._transport
    if not isinstance(transport, TcpTransport) or transport._io_writer is None:
        return False
    local = transport._io_writer.get_extra_info("sockname")
    peer = transport._io_writer.get_extra_info("peername")
    if peer != ("127.0.0.1", config.port) or not local or local[0] != "127.0.0.1":
        return False
    uid = os.getuid()
    evidence_unavailable = False
    try:
        for process in psutil.process_iter():
            try:
                if process.uids().effective != uid:
                    continue
                if not _same_executable(process.exe(), config.server_binary):
                    continue
                if process.cmdline()[1:] != ["-c", str(config.broker_config)]:
                    continue
                for connection in process.net_connections(kind="tcp4"):
                    if (
                        connection.status == psutil.CONN_ESTABLISHED
                        and connection.laddr == peer
                        and connection.raddr == local
                    ):
                        return process.is_running() and process.uids().effective == uid
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError):
                evidence_unavailable = True
    except (psutil.Error, OSError):
        evidence_unavailable = True
    if evidence_unavailable:
        raise BrokerIdentityError("Broker process evidence could not be read on this machine")
    return False


def _verified_token(config: BusConfig, client: Client) -> str:
    """Release the token only after INFO and OS ownership agree on this socket."""
    if client._server_info.get("server_name") != config.namespace or not _owned_connection(
        config, client
    ):
        raise BrokerIdentityError("Broker peer identity could not be verified")
    return config.token()


async def connect_broker(config: BusConfig, *, connect_timeout: float = 1) -> Client:
    """Connect without disclosing credentials to an unverified loopback listener."""

    async def ignore_error(_: Exception) -> None:
        # Public callers report bounded error types, never transport diagnostics.
        pass

    client = Client()
    try:
        await client.connect(
            config.url,
            # The pinned client evaluates this callback after reading INFO and
            # immediately before writing CONNECT, on the same connected socket.
            token=partial(_verified_token, config, client),
            # nats-py annotates int but asyncio supports fractional seconds.
            connect_timeout=connect_timeout,  # ty: ignore[invalid-argument-type] -- accepts float
            allow_reconnect=False,
            max_reconnect_attempts=0,
            error_cb=ignore_error,
        )
        return client
    except BaseException as error:
        await client.close()
        # nats-py assumes decoded INFO is a mapping before invoking the token
        # callback. A JSON array/null is an unverified peer, not a usable broker.
        if isinstance(error, AttributeError) and not isinstance(client._server_info, dict):
            raise BrokerIdentityError("Broker peer identity could not be verified") from None
        raise


class BrokerSupervisor(NativeSupervisor):
    def __init__(self, config: BusConfig) -> None:
        super().__init__(config.path)
        self.bus = config
        self.root = config.root / "supervisor"
        self.label = f"local.agent-bus.nats.{config.namespace}"
        self.legacy_label = None  # The broker's service label has never changed.
        self.marker = self.root / "supervisor.json"

    def _make_root(self) -> None:
        # By `start`, after the broker's definition is made: a start that is refused
        # for its definition leaves no folder behind.
        private_directory(self.root)

    def _matches_command(self, arguments: list[str]) -> bool:
        if len(arguments) != 3 or arguments[1:] != ["-c", str(self.bus.broker_config)]:
            return False
        return arguments[0] == self.bus.server_binary or launcher_shim(arguments[0])

    def launchd_definition(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": [self.bus.server_binary, "-c", str(self.bus.broker_config)],
                "WorkingDirectory": str(self.bus.root),
                "RunAtLoad": True,
                # A configured broker is continuous infrastructure. `stop` removes the
                # launchd job before deleting its definition, so unconditional restart
                # does not turn an explicit stop into a respawn.
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "ExitTimeOut": 10,
                "Umask": 0o077,
                "StandardOutPath": str(self.root / "broker.log"),
                "StandardErrorPath": str(self.root / "broker.log"),
            }
        )

    def systemd_definition(self) -> str:
        def quote(value: str) -> str:
            return json.dumps(value).replace("%", "%%").replace("$", "$$")

        command = " ".join(
            quote(value) for value in (self.bus.server_binary, "-c", str(self.bus.broker_config))
        )
        return (
            "[Unit]\nDescription=Local agent message bus\n\n[Service]\nType=simple\n"
            f"ExecStart={command}\n"
            f"WorkingDirectory={systemd_working_directory(self.bus.root)}\n"
            "Restart=always\nRestartSec=10\nTimeoutStopSec=10\n"
            "KillMode=control-group\nUMask=0077\n" + SYSTEMD_INSTALL
        )

    def _restarts_clean_exit(self, manager: str, definition: Path) -> bool:
        """Read only the restart policy from this verified broker definition."""
        try:
            if manager == "launchd":
                keep_alive = plistlib.loads(definition.read_bytes()).get("KeepAlive")
                return keep_alive is True
            return "Restart=always" in definition.read_text().splitlines()
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            return False

    async def failure_reason(self) -> str:
        """Describe why this configured supervisor currently has no reachable broker.

        Return fixed, credential-free text. Service-manager output is parsed only for
        named state fields; paths, stderr and arbitrary diagnostics never leave here.
        """
        try:
            marker = self._read_marker()
            if marker is None:
                return "broker not running (supervisor not installed)"
            manager = marker.get("manager")
            label = marker.get("label")
            if not isinstance(manager, str) or manager not in {"launchd", "systemd"}:
                return "broker not running (supervisor ownership could not be verified)"
            if label != self.label:
                return "broker not running (supervisor ownership could not be verified)"
            self._verify_definition(self.label, manager)
            definition = self._definition_path(self.label, manager)
            restarts_cleanly = self._restarts_clean_exit(manager, definition)
            if manager == "launchd":
                target = f"gui/{os.getuid()}/{self.label}"
                try:
                    output = await run(["launchctl", "print", target], max_seconds=5)
                except CommandError as error:
                    if error.returncode == 113:
                        return "broker not running (launchd: not loaded)"
                    return "broker not running (launchd status unavailable)"
                fields = {}
                for line in output.stdout.splitlines():
                    if " = " in line:
                        key, value = line.strip().split(" = ", 1)
                        # launchctl nests coalition blocks whose state must not replace
                        # the job's earlier top-level state.
                        fields.setdefault(key, value)
                state = fields.get("job state", fields.get("state", "inactive"))
                code = fields.get("last exit code")
                if state == "running":
                    return "broker unreachable (launchd: running)"
                if code is not None and re.fullmatch(r"-?\d+", code) is None:
                    code = None
                if code == "0" and not restarts_cleanly:
                    return "broker not running (launchd: exited 0, not restarted)"
                if code is not None:
                    suffix = "restart pending" if restarts_cleanly else "not running"
                    return f"broker not running (launchd: exited {code}, {suffix})"
                return f"broker not running (launchd: {state})"

            unit = f"{self.label}.service"
            try:
                output = await run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        "-p",
                        "LoadState",
                        "-p",
                        "ActiveState",
                        "-p",
                        "SubState",
                        "-p",
                        "Result",
                        "-p",
                        "ExecMainStatus",
                        unit,
                    ],
                    max_seconds=5,
                )
            except CommandError:
                return "broker not running (systemd status unavailable)"
            fields = dict(line.split("=", 1) for line in output.stdout.splitlines() if "=" in line)
            if fields.get("ActiveState") == "active":
                return "broker unreachable (systemd: running)"
            code = fields.get("ExecMainStatus")
            if fields.get("Result") == "success" and code == "0" and not restarts_cleanly:
                return "broker not running (systemd: exited 0, not restarted)"
            state = fields.get("SubState") or fields.get("ActiveState") or "inactive"
            if code is not None:
                suffix = "restart pending" if restarts_cleanly else state
                return f"broker not running (systemd: exited {code}, {suffix})"
            return f"broker not running (systemd: {state})"
        except (BridgeError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return "broker not running (supervisor status unavailable)"


class Broker:
    def __init__(self, config: BusConfig) -> None:
        self.config = config
        self.supervisor = BrokerSupervisor(config)
        self.identity_error: str | None = None

    async def reachable(self) -> bool:
        self.identity_error = None
        try:
            async with asyncio.timeout(2):
                client = await connect_broker(self.config, connect_timeout=0.5)
                await client.close()
            return True
        except BrokerIdentityError as error:
            self.identity_error = str(error)
            return False
        except (nats.errors.Error, OSError, TimeoutError):
            return False

    async def failure_reason(self) -> str:
        """Return the current fixed diagnostic after a failed reachability probe."""
        if self.identity_error is not None:
            return self.identity_error
        return await self.supervisor.failure_reason()

    async def start(self) -> dict[str, Any]:
        # A reachable broker answered this configuration's token, so it is ours; one
        # whose definition an earlier release left where the OS never reloads it is
        # reinstalled once (start verifies the old definition first).
        reinstall = False
        if await self.reachable():
            reinstall = await asyncio.to_thread(self.supervisor.reinstall_needed)
            if not reinstall:
                return {"broker_running": True, "managed": self.supervisor.marker.exists()}
        await asyncio.to_thread(self.config.require_binary)
        if not reinstall:  # When reinstalling, our own authenticated broker holds the port.
            await self._refuse_occupied_port()
        # The supervisor makes its folder itself, once it has the broker's definition.
        await self.supervisor.start()
        for _ in range(30):
            if await self.reachable():
                return {"broker_running": True, "managed": True}
            await asyncio.sleep(0.1)
        raise BridgeError("Broker did not become ready; inspect its private supervisor log")

    async def _refuse_occupied_port(self) -> None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.config.port), 0.5
            )
        except (OSError, TimeoutError):
            return
        writer.close()
        await writer.wait_closed()
        raise BridgeError(
            "Broker port is occupied by an unverified service; refusing to replace it"
        )

    async def stop(self) -> dict[str, Any]:
        if not self.supervisor.marker.exists():
            if await self.reachable():
                raise BridgeError("Configured broker is unmanaged; it was not stopped")
            return {"broker_running": False}
        await self.supervisor.stop_if_installed()
        for _ in range(30):
            if not await self.reachable():
                return {"broker_running": False, "coordinator_kept_running": True}
            await asyncio.sleep(0.1)
        raise BridgeError("Broker still responds; shutdown is unverified")
