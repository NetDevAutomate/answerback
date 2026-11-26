"""Native user supervision, scoped to a deterministic bridge configuration."""

import asyncio
import hashlib
import json
import os
import plistlib
import re
import shlex
import sys
from pathlib import Path
from typing import NoReturn

from .config import atomic_write
from .lifecycle import service_command
from .process import BridgeError, CommandError, run
from .transport import runtime_dir

# Without an [Install] section `systemctl --user enable` only links a unit, and the
# unit never starts at the next login; default.target is the user session's target.
SYSTEMD_INSTALL = "\n[Install]\nWantedBy=default.target\n"


def systemd_working_directory(directory: Path) -> str:
    """Return the value that names `directory` in WorkingDirectory= of a systemd user unit.

    The value is the path with every '%' doubled and '/.' appended, without quotes around it;
    for the root folder it is '/.'. Raises BridgeError for a path that is not absolute, for a
    path that holds a line feed or a carriage return, and for a path with a component that is
    equal to '..', checked in this order. Lengths are not checked: in the measurement
    (T-146-fix2-dots.json, rows 16 to 18) systemd 259.9 accepted a last name of 255 bytes and
    refused both last names of 256 bytes, and the file system there refused to make a folder
    with either name of 256 bytes.
    """
    # Measured with systemd 259.9 (T-146): it leaves quotes around this value in place and
    # then refuses the unit ("path is not absolute"), expands '%' specifiers in it, drops
    # blanks and tabs at its end, joins the next line to a value that ends with a backslash,
    # and ends the value at a line feed and at a carriage return. With '/.' at the end the
    # directory it parsed was the folder, byte for byte, in each of the 96 rows of the
    # measurement for which a value was written; the other 6 rows hold a line feed or a
    # carriage return and are refused below. It refused a value with a '..' component ("path
    # is not normalized"), except where the component followed the root directly: there it
    # read '/..' as '/' (T-146-fix2-dots.json). Every '..' component is refused below.
    text = str(directory)
    if not text.startswith("/"):
        raise BridgeError(
            f"A systemd unit cannot name this working directory: the path is not absolute: {text!r}"
        )
    if "\n" in text or "\r" in text:
        raise BridgeError(
            "A systemd unit cannot name this working directory: the path holds a line feed or "
            f"a carriage return: {text!r}"
        )
    if ".." in text.split("/"):
        raise BridgeError(
            "A systemd unit cannot name this working directory: the path holds a '..' component: "
            f"{text!r}"
        )
    return "/." if text == "/" else text.replace("%", "%%") + "/."


class NativeSupervisor:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.root = runtime_dir(config_path)
        digest = hashlib.sha256(str(self.config_path).encode()).hexdigest()[:12]
        self.label = f"local.answerback-bridge.{digest}"
        self.legacy_label: str | None = f"local.herdr-bridge.{digest}"
        self.marker = self.root / "supervisor.json"

    def _owned_labels(self) -> tuple[str, ...]:
        if self.legacy_label is None:
            return (self.label,)
        return self.label, self.legacy_label

    def _matches_module_command(self, arguments: list[str], command: tuple[str, ...]) -> bool:
        """Recognize this config's module launch with an exact command suffix."""
        if not arguments:
            return False
        # Definitions written by this release carry -P (process.module_launch_argv);
        # those written by earlier releases do not, and must still verify as ours.
        rest = arguments[1:]
        if rest[:1] == ["-P"]:
            rest = rest[1:]
        return (
            len(rest) == 4 + len(command)
            and re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(arguments[0]).name) is not None
            and rest[0] == "-m"
            and rest[1] in ("answerback_bridge", "codex_herdr_bridge")
            and rest[2] == "--config"
            and Path(rest[3]).expanduser().resolve() == self.config_path
            and rest[4:] == list(command)
        )

    def _matches_command(self, arguments: list[str]) -> bool:
        return self._matches_module_command(arguments, ("serve",))

    def _runtime_definition_path(self, label: str, manager: str) -> Path:
        # Where every release before this one wrote its definition.
        return self.root / (label + (".plist" if manager == "launchd" else ".service"))

    def _installed_definition_path(self, manager: str) -> Path:
        # launchd reloads ~/Library/LaunchAgents at login; a plist under the runtime
        # directory is never reloaded after a reboot. systemd units stay in the runtime
        # directory and are enabled (linked) by absolute path.
        if manager == "launchd":
            return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"
        return self._runtime_definition_path(self.label, manager)

    def _read_marker(self) -> dict[str, object] | None:
        if self.marker.is_symlink():
            raise BridgeError("Supervisor marker ownership cannot be verified through a symlink")
        if not self.marker.exists():
            return None
        marker = json.loads(self.marker.read_text())
        if not isinstance(marker, dict):
            raise BridgeError("Supervisor marker must be an object")
        return marker

    def _definition_path(self, label: str, manager: str) -> Path:
        # A marker written by this release records where its definition lives; a marker
        # without that path was written by a release that kept it in the runtime directory.
        marker = self._read_marker()
        if marker is not None and marker.get("label") == label and marker.get("definition"):
            return Path(str(marker["definition"]))
        return self._runtime_definition_path(label, manager)

    @staticmethod
    def _private_tree(path: Path) -> None:
        # Path.mkdir(parents=True) applies the mode to the leaf only; create every
        # missing ancestor 0700 and leave directories the owner already has untouched.
        missing = []
        for directory in (path, *path.parents):
            if directory.exists():
                break
            missing.append(directory)
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)

    def _verify_definition(self, label: str, manager: str) -> None:
        path = self._definition_path(label, manager)
        if path.is_symlink():
            raise BridgeError(
                "Supervisor definition ownership cannot be verified through a symlink"
            )
        if not path.exists():
            return  # Historical marker is sufficient when no source definition remains.
        try:
            if manager == "launchd":
                document = plistlib.loads(path.read_bytes())
                arguments = document["ProgramArguments"]
                if document.get("Label") != label:
                    raise ValueError
                if "Program" in document and (not arguments or document["Program"] != arguments[0]):
                    raise ValueError
            else:
                lines = [
                    line[len("ExecStart=") :]
                    for line in path.read_text().splitlines()
                    if line.startswith("ExecStart=")
                ]
                if len(lines) != 1:
                    raise ValueError
                arguments = shlex.split(lines[0].replace("%%", "%").replace("$$", "$"))
            if not self._matches_command(arguments):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
            raise BridgeError("Supervisor definition ownership cannot be verified") from None

    def legacy_installed(self) -> bool:
        if self.marker.is_symlink():
            raise BridgeError("Supervisor marker ownership cannot be verified through a symlink")
        if self.marker.exists():
            marker = json.loads(self.marker.read_text())
            if marker.get("label") not in self._owned_labels():
                raise BridgeError("Supervisor marker belongs to another configuration")
            return marker.get("label") == self.legacy_label
        if self.legacy_label is None:
            return False
        manager = "launchd" if sys.platform == "darwin" else "systemd"
        path = self._definition_path(self.legacy_label, manager)
        if path.exists() or path.is_symlink():
            self._verify_definition(self.legacy_label, manager)
            return True
        return False

    def launchd_definition(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": service_command(self.config_path),
                "WorkingDirectory": str(self.config_path.parent),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 10,
                "ExitTimeOut": 10,
                "Umask": 0o077,
                "EnvironmentVariables": self.environment(),
                "StandardOutPath": str(self.root / "launcher.log"),
                "StandardErrorPath": str(self.root / "launcher.log"),
            }
        )

    @staticmethod
    def environment() -> dict[str, str]:
        # Only routing/configuration context; never persist tokens or API keys in a unit.
        keys = ("PATH", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "HERDR_CONFIG_PATH")
        return {key: os.environ[key] for key in keys if key in os.environ}

    def systemd_definition(self) -> str:
        def quote(value: str) -> str:
            return json.dumps(value).replace("%", "%%").replace("$", "$$")

        command = " ".join(quote(arg) for arg in service_command(self.config_path))
        working_directory = systemd_working_directory(self.config_path.parent)
        # systemd expands specifiers in both fields; environment values do not expand '$'.
        environment = "\n".join(
            "Environment=" + json.dumps(f"{key}={value}").replace("%", "%%")
            for key, value in self.environment().items()
        )
        return (
            "[Unit]\nDescription=AnswerBack collaboration bridge\n\n[Service]\nType=simple\n"
            f"ExecStart={command}\nWorkingDirectory={working_directory}\n"
            f"{environment}\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=10\n"
            "KillMode=control-group\nUMask=0077\n" + SYSTEMD_INSTALL
        )

    def _definition(self) -> bytes:
        """Return the bytes that `start` writes as the definition on this operating system.

        Raises what the definition methods raise for a configuration that no definition can
        name, as it came: BridgeError from `systemd_working_directory`; ValueError from
        plistlib for a text with a control character below U+0020 other than a tab, a line
        feed and a carriage return; UnicodeEncodeError, which is a ValueError, from either
        for a text with a lone surrogate (a file name that is not UTF-8). Raises BridgeError
        on an operating system that is neither macOS nor Linux.
        """
        if sys.platform == "darwin":
            return self.launchd_definition()
        if sys.platform.startswith("linux"):
            return self.systemd_definition().encode()
        raise BridgeError("Native supervision requires macOS or Linux; see serve")

    def _make_root(self) -> None:
        """Make what `start` writes into; called once the definition is in hand.

        Nothing here: the bridge's runtime directory is made by its caller, and
        `atomic_write` makes a missing one.
        """

    def reinstall_needed(self) -> bool:
        """Return whether this label's recorded definition is stale or in the wrong place.

        True only for a marker of this configuration's current label, on its own
        operating system, whose definition is not the exact text `start` would write at
        the path the service manager reloads at login. True also when that text cannot
        be made (see `_definition`), as for a definition file that cannot be read: no
        stored definition is then the one `start` would write. Never raises for that;
        the refusal comes from `start`, with nothing changed. The caller still runs
        `start`, which verifies the stored definition before calling the service manager.
        """
        marker = self._read_marker()
        if marker is None or marker.get("label") != self.label:
            return False
        if sys.platform == "darwin":
            manager = "launchd"
        elif sys.platform.startswith("linux"):
            manager = "systemd"
        else:
            return False
        if marker.get("manager") != manager:
            return False
        path = self._installed_definition_path(manager)
        if self._definition_path(self.label, manager) != path or path.is_symlink():
            return True
        try:
            expected = self._definition()
        except (BridgeError, ValueError):
            # Only the refusal to make the definition; `start` raises it again.
            return True
        try:
            return path.read_bytes() != expected
        except OSError:
            return True

    async def start(self) -> None:
        # Made before the first change. A configuration that no definition can name is
        # refused here: no record is written, nothing is asked of the service manager,
        # the earlier service stays as it is and no file is made.
        definition = self._definition()
        self._make_root()
        if self.legacy_installed() and not self.marker.exists():
            manager = "launchd" if sys.platform == "darwin" else "systemd"
            atomic_write(self.marker, json.dumps({"manager": manager, "label": self.legacy_label}))
        # The legacy service this start replaces: its label, manager and definition
        # file, and the bytes of the record at the moment the switch begins.
        legacy: tuple[str, str, Path, bytes] | None = None
        if self.marker.exists():
            record = self.marker.read_bytes()
            marker = json.loads(record)
            if self.legacy_label is not None and marker.get("label") == self.legacy_label:
                # Stopped and unregistered before the new service exists, so the two
                # never run together. Its definition and its marker stay until the new
                # service is started: they are the way back when that start fails, and
                # what a later start finds after a crash in between.
                manager = marker["manager"]
                await self._unregister(self.legacy_label, manager)
                legacy = (
                    self.legacy_label,
                    manager,
                    self._definition_path(self.legacy_label, manager),
                    record,
                )
            elif marker.get("label") != self.label:
                raise BridgeError("Supervisor marker belongs to another configuration")
            else:
                self._verify_definition(self.label, marker["manager"])
        try:
            await self._install(definition)
        except Exception as error:
            if legacy is None:
                raise
            await self._way_back(error, *legacy)
        if legacy is not None:
            # The new marker is written; only now do the legacy files go.
            label, manager, definition, _ = legacy
            definition.unlink(missing_ok=True)
            self._runtime_definition_path(label, manager).unlink(missing_ok=True)

    async def _install(self, definition: bytes) -> None:
        """Write `definition`, the bytes `_definition` made, and start the service."""
        if sys.platform == "darwin":
            path = self._installed_definition_path("launchd")
            target = f"gui/{os.getuid()}/{self.label}"
            previous = self._definition_path(self.label, "launchd")
            registered = await self.launchd_registered(target)
            if previous != path and (previous.exists() or previous.is_symlink()):
                # Migration from a release that kept the plist in the runtime directory:
                # launchd must load the definition from its new path, so the job loaded
                # from the old one is booted out before the old file goes.
                if registered:
                    await self._bootout(target)
                    registered = False
                previous.unlink()
            elif registered and (not path.exists() or path.read_bytes() != definition):
                # launchd does not reload a registered job when its plist is rewritten.
                # Remove the old job first so changed restart/environment policy takes
                # effect now, rather than only after the next login.
                await self._bootout(target)
                registered = False
            self._private_tree(path.parent)
            atomic_write(path, definition.decode())
            if registered:
                await run(["launchctl", "kickstart", target])
            else:
                await run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
            atomic_write(
                self.marker,
                json.dumps({"manager": "launchd", "label": self.label, "definition": str(path)}),
            )
        elif sys.platform.startswith("linux"):
            path = self._installed_definition_path("systemd")
            atomic_write(path, definition.decode())
            # enable by absolute path links the unit AND starts it at the next login;
            # a bare link never did. Starting before login is the owner's optional
            # `loginctl enable-linger`, which this code never runs.
            await run(["systemctl", "--user", "enable", str(path)])
            await run(["systemctl", "--user", "daemon-reload"])
            await run(["systemctl", "--user", "start", path.name])
            atomic_write(
                self.marker,
                json.dumps({"manager": "systemd", "label": self.label, "definition": str(path)}),
            )
        else:
            raise BridgeError("Native supervision requires macOS or Linux; see serve")

    async def _way_back(
        self, failure: Exception, label: str, manager: str, definition: Path, record: bytes
    ) -> NoReturn:
        """Undo a failed switch to the new label and report it; never returns."""
        try:
            await self._restore(label, manager, definition, record)
        except Exception as second:
            raise BridgeError(
                f"{failure}; the previous service {label} is not running, because "
                f"starting it again failed too: {second}. "
                f"{self._kept(definition, record)}; run start again"
            ) from failure
        raise BridgeError(f"{failure}; the previous service {label} runs again") from failure

    async def _restore(self, label: str, manager: str, definition: Path, record: bytes) -> None:
        # Order matters: whatever exists of the new service is removed from the service
        # manager first, so the legacy service never runs next to it. Then the record:
        # the legacy service is started only under the record it had.
        if manager == "launchd":
            new = self._installed_definition_path("launchd")
            target = f"gui/{os.getuid()}/{self.label}"
            if await self.launchd_registered(target):
                await self._bootout(target)
            new.unlink(missing_ok=True)
            self._restore_record(record)
            self._require_previous(definition)
            await run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(definition)])
        else:
            new = self._installed_definition_path("systemd")
            # Every time, whatever this attempt did: an earlier attempt may have left
            # the new unit enabled or running, and a failed `enable` may have made
            # its links. A unit that cannot be taken out raises here, before the
            # legacy unit is started.
            await self._take_out(new.name)
            new.unlink(missing_ok=True)
            self._restore_record(record)
            self._require_previous(definition)
            await run(["systemctl", "--user", "enable", str(definition)])
            await run(["systemctl", "--user", "daemon-reload"])
            await run(["systemctl", "--user", "start", definition.name])

    def _kept(self, definition: Path, record: bytes) -> str:
        # Says only what is there: a historical record may have no definition, and
        # the record itself may be what could not be put back.
        try:
            has_record = not self.marker.is_symlink() and self.marker.read_bytes() == record
        except OSError:
            has_record = False
        if definition.exists():
            if has_record:
                return "Its definition and the supervision record are kept"
            return "Its definition is kept, the supervision record could not be put back"
        if has_record:
            return "The supervision record is kept"
        return "Neither its definition nor the supervision record is kept"

    def _restore_record(self, record: bytes) -> None:
        # The write of the new record is the last step of a start, and it can fail
        # after it replaced the file. A record that names the new label next to a
        # running legacy service would make the next start install the new service
        # beside it, so the legacy service is not started before the record is back.
        try:
            if not self.marker.is_symlink() and self.marker.read_bytes() == record:
                return
        except OSError:
            pass
        atomic_write(self.marker, record.decode())

    @staticmethod
    def _require_previous(definition: Path) -> None:
        # A historical marker may outlive its definition; then there is nothing to load.
        if not definition.exists():
            raise BridgeError("no definition of the previous service remains")

    async def stop_if_installed(self) -> None:
        if self.marker.is_symlink():
            raise BridgeError("Supervisor marker ownership cannot be verified through a symlink")
        if not self.marker.exists():
            return
        marker = json.loads(self.marker.read_text())
        label = marker.get("label")
        if label not in self._owned_labels():
            raise BridgeError("Supervisor marker belongs to another configuration")
        await self._unregister(label, marker["manager"])
        definition = self._definition_path(label, marker["manager"])
        self.marker.unlink()
        definition.unlink(missing_ok=True)
        # A runtime-directory copy left by an earlier release must not outlive the job
        # either; launchd would otherwise still see a file it never reloads.
        self._runtime_definition_path(label, marker["manager"]).unlink(missing_ok=True)

    async def _unregister(self, label: str, manager: str) -> None:
        """Verify an owned definition, then stop its service and take it from the manager."""
        self._verify_definition(label, manager)
        if manager == "launchd" and sys.platform == "darwin":
            target = f"gui/{os.getuid()}/{label}"
            if await self.launchd_registered(target):
                await self._bootout(target)
        elif manager == "systemd" and sys.platform.startswith("linux"):
            await self._take_out(f"{label}.service")
        else:
            raise BridgeError("Supervisor marker does not match this operating system")

    async def _take_out(self, unit: str) -> None:
        """Stop and disable a systemd user unit; a unit systemd has not loaded passes.

        systemd answers `stop` with exit 5 ("not loaded") for a unit it does not know
        or whose file it refused, and `disable` with exit 1 ("does not exist") for a
        unit without a file. Both are the state this method is called to reach, and
        both are what an interrupted switch leaves behind. So after a failure, and
        only then, `show` is asked: a failed `stop` passes when the unit is neither
        loaded nor running, a failed `disable` when systemd finds no such unit and
        nothing of it runs (`LoadState=not-found` and `ActiveState=inactive`: the load
        state describes the unit's file, not its processes). In every other case the
        failure is raised as it came.
        """
        try:
            await run(["systemctl", "--user", "stop", unit])
        except CommandError:
            state = await self._unit_state(unit)
            if state is None or state[0] == "loaded" or state[1] != "inactive":
                raise
        try:
            await run(["systemctl", "--user", "disable", unit])
        except CommandError:
            state = await self._unit_state(unit)
            if state is None or state[0] != "not-found" or state[1] != "inactive":
                raise

    @staticmethod
    async def _unit_state(unit: str) -> tuple[str, str] | None:
        """Return (LoadState, ActiveState) of a unit, or None when systemd gave neither."""
        try:
            output = await run(
                ["systemctl", "--user", "show", "-p", "LoadState", "-p", "ActiveState", unit]
            )
        except Exception:
            # No answer, whatever kept `show` from giving one: `run` raises an OSError,
            # which is no BridgeError, when the program cannot be started. A
            # cancellation is not an Exception and passes through.
            return None
        fields = dict(line.split("=", 1) for line in output.stdout.splitlines() if "=" in line)
        load, active = fields.get("LoadState"), fields.get("ActiveState")
        if not load or not active:
            return None
        return load, active

    async def _bootout(self, target: str) -> None:
        try:
            await run(["launchctl", "bootout", target])
        except CommandError as error:
            if error.returncode != 113:
                raise
        # bootout acknowledges removal before launchd forgets a SIGTERMed job.
        # Wait for the registration, not only the bridge process, to disappear.
        try:
            async with asyncio.timeout(10):
                # launchd exposes no asyncio event for registration removal.
                while await self.launchd_registered(target):  # noqa: ASYNC110
                    await asyncio.sleep(0.1)
        except TimeoutError as error:
            raise BridgeError(
                "macOS has not removed the bridge service; supervision record retained"
            ) from error

    async def launchd_registered(self, target: str) -> bool:
        try:
            await run(["launchctl", "print", target], max_seconds=5)
        except CommandError as error:
            if error.returncode == 113:
                return False
            raise
        return True
