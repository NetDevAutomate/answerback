"""Native supervision for the fleet document writer."""

from __future__ import annotations

import json
import os
import plistlib
import re
import sys
from pathlib import Path
from typing import Any

from ..process import BridgeError, CommandError, module_launch_argv, run
from ..supervision import SYSTEMD_INSTALL, NativeSupervisor, systemd_working_directory
from .settings import default_path


class FleetWriterSupervisor(NativeSupervisor):
    """Own one fleet writer for the same configuration as one bridge service."""

    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path)
        digest = self.label.removeprefix("local.answerback-bridge.")
        self.label = f"local.answerback-fleet.{digest}"
        self.legacy_label = None
        self.marker = self.root / "fleet-supervisor.json"
        self.settings_path = default_path(self.config_path)

    def command(self) -> list[str]:
        """Return the writer command stored in the native service definition."""
        return module_launch_argv(
            self.config_path,
            "fleet",
            "serve",
            "--settings",
            str(self.settings_path),
        )

    def _matches_command(self, arguments: list[str]) -> bool:
        return self._matches_module_command(
            arguments,
            ("fleet", "serve", "--settings", str(self.settings_path)),
        )

    def launchd_definition(self) -> bytes:
        log_path = self.root / "fleet-writer.log"
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": self.command(),
                "WorkingDirectory": str(self.config_path.parent),
                "RunAtLoad": True,
                # A deliberate clean exit is the writer's stop signal. Restart only a
                # crash, rather than turning that exit into a restart loop.
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 10,
                "ExitTimeOut": 10,
                "Umask": 0o077,
                "EnvironmentVariables": self.environment(),
                "StandardOutPath": str(log_path),
                "StandardErrorPath": str(log_path),
            }
        )

    def systemd_definition(self) -> str:
        def quote(value: str) -> str:
            return json.dumps(value).replace("%", "%%").replace("$", "$$")

        command = " ".join(quote(argument) for argument in self.command())
        environment = "\n".join(
            "Environment=" + json.dumps(f"{key}={value}").replace("%", "%%")
            for key, value in self.environment().items()
        )
        return (
            "[Unit]\nDescription=Answerback fleet writer\n\n[Service]\nType=simple\n"
            f"ExecStart={command}\n"
            f"WorkingDirectory={systemd_working_directory(self.config_path.parent)}\n"
            f"{environment}\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=10\n"
            "KillMode=control-group\nUMask=0077\n" + SYSTEMD_INSTALL
        )

    async def status(self) -> dict[str, Any]:
        """Read bounded service-manager state without returning manager diagnostics."""
        result: dict[str, Any] = {
            "installed": False,
            "running": False,
            "pid": None,
            "settings_path": str(self.settings_path),
            "last_exit": None,
            "failure_reason": None,
        }
        try:
            marker = self._read_marker()
            if marker is None:
                return result
            result["installed"] = True
            manager = marker.get("manager")
            if manager not in {"launchd", "systemd"} or marker.get("label") != self.label:
                result["failure_reason"] = "fleet writer supervisor ownership could not be verified"
                return result
            self._verify_definition(self.label, str(manager))
            if manager == "launchd" and sys.platform == "darwin":
                await self._launchd_status(result)
            elif manager == "systemd" and sys.platform.startswith("linux"):
                await self._systemd_status(result)
            else:
                result["failure_reason"] = "fleet writer supervisor does not match this system"
        except (BridgeError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            result["failure_reason"] = "fleet writer supervisor status unavailable"
        return result

    async def _launchd_status(self, result: dict[str, Any]) -> None:
        target = f"gui/{os.getuid()}/{self.label}"
        try:
            output = await run(["launchctl", "print", target], max_seconds=5)
        except CommandError as error:
            result["failure_reason"] = (
                "fleet writer is not loaded"
                if error.returncode == 113
                else "fleet writer launchd status unavailable"
            )
            return
        fields: dict[str, str] = {}
        for line in output.stdout.splitlines():
            if " = " in line:
                key, value = line.strip().split(" = ", 1)
                fields.setdefault(key, value)
        state = fields.get("job state", fields.get("state"))
        result["running"] = state == "running"
        result["pid"] = _nonnegative_integer(fields.get("pid"), positive=True)
        result["last_exit"] = _signed_integer(fields.get("last exit code"))
        if not result["running"] and result["last_exit"] not in (None, 0):
            result["failure_reason"] = "fleet writer exited unsuccessfully; restart pending"

    async def _systemd_status(self, result: dict[str, Any]) -> None:
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
                    "MainPID",
                    "-p",
                    "ExecMainStatus",
                    f"{self.label}.service",
                ],
                max_seconds=5,
            )
        except CommandError:
            result["failure_reason"] = "fleet writer systemd status unavailable"
            return
        fields = dict(line.split("=", 1) for line in output.stdout.splitlines() if "=" in line)
        result["running"] = fields.get("ActiveState") == "active"
        result["pid"] = _nonnegative_integer(fields.get("MainPID"), positive=True)
        result["last_exit"] = _signed_integer(fields.get("ExecMainStatus"))
        if not result["running"] and result["last_exit"] not in (None, 0):
            result["failure_reason"] = "fleet writer exited unsuccessfully; restart pending"


def _signed_integer(value: str | None) -> int | None:
    if value is None or re.fullmatch(r"-?\d+", value) is None:
        return None
    return int(value)


def _nonnegative_integer(value: str | None, *, positive: bool = False) -> int | None:
    if value is None or not value.isdigit():
        return None
    number = int(value)
    if positive and number == 0:
        return None
    return number
